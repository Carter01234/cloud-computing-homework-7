#!/usr/bin/env python3
"""HW5 web server: serves files from Cloud Storage and records every request in Cloud SQL.

Each request is handled in four separately timed steps:
    1. extract_request_info  - parse the request and its headers
    2. get_file_from_bucket  - read the requested file from Cloud Storage
    3. send_response         - send the response to the client
    4. write_to_database     - record the request in Cloud SQL

Every request goes into `requests` (so banned-country requests can be counted);
any request whose status is not 200 also goes into `failed_requests`.

Schema (3NF, see setup_schema.py): requests -> ips -> countries.
A request stores only its client IP; the IP's country lives in `ips`
(client_ip -> country) and the ban flag in `countries` (country -> is_banned).

Requirements:
    pip install "cloud-sql-python-connector[pymysql]" "pymysql==1.2.0" sqlalchemy \
        google-cloud-secret-manager google-cloud-storage google-cloud-logging google-cloud-pubsub
"""

import json
import logging
import mimetypes
import signal
import threading
import time
from dataclasses import dataclass
from functools import wraps
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

import sqlalchemy
from google.api_core.exceptions import NotFound
from google.cloud import logging as cloud_logging
from google.cloud import pubsub_v1, secretmanager, storage
from google.cloud.sql.connector import Connector

PROJECT_ID = "project-8aeecca0-4f70-4c6b-8e3"
FORBIDDEN_TOPIC = "forbidden-requests"

BUCKET_NAME = "html-files-for-class"
PREFIX = "pages"
HOST = "0.0.0.0"  # listen on all interfaces so the VM is reachable from outside
PORT = 8080

# Cloud SQL
REGION = "us-east1"
INSTANCE = "my-class-db"
INSTANCE_CONNECTION_NAME = f"{PROJECT_ID}:{REGION}:{INSTANCE}"
DB_USER = "root"
DB_NAME = "hw5"
DB_PASS_SECRET = "db-root-password"

# Timing output
TIMINGS_FILE = "timings.json"
TIMING_SUMMARY_EVERY = 10_000  # print a timing summary every N requests

FORBIDDEN_COUNTRIES = {
    "north korea",
    "iran",
    "cuba",
    "myanmar",
    "iraq",
    "libya",
    "sudan",
    "zimbabwe",
    "syria",
}

# setting up logging to work with google cloud
client = cloud_logging.Client()
client.setup_logging()

storage_client = storage.Client()
bucket = storage_client.bucket(BUCKET_NAME)

publisher = pubsub_v1.PublisherClient()
forbidden_topic_path = publisher.topic_path(PROJECT_ID, FORBIDDEN_TOPIC)


# --------------------------------------------------------------------------
# Database setup
# --------------------------------------------------------------------------

def get_secret(secret_id: str, version: str = "latest") -> str:
    """Fetch a secret's value from Google Secret Manager."""
    sm_client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/{version}"
    response = sm_client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


DB_PASS = get_secret(DB_PASS_SECRET)
connector = Connector()


def _get_db_connection():
    return connector.connect(
        INSTANCE_CONNECTION_NAME,
        "pymysql",
        user=DB_USER,
        password=DB_PASS,
        db=DB_NAME,
    )


# A connection pool shared by all request threads. Opening a new Cloud SQL
# connection per request would be far slower than reusing pooled ones.
engine = sqlalchemy.create_engine(
    "mysql+pymysql://",
    creator=_get_db_connection,
    pool_size=5,
    max_overflow=5,
    pool_pre_ping=True,   # replace connections the server has dropped
    pool_recycle=1800,
)

INSERT_COUNTRY = sqlalchemy.text(
    "INSERT IGNORE INTO countries (country, is_banned) VALUES (:country, :is_banned)"
)
# An IP always comes from the same country, so the first country seen for an IP
# is kept; IGNORE skips IPs that are already recorded.
INSERT_IP = sqlalchemy.text(
    "INSERT IGNORE INTO ips (client_ip, country) VALUES (:client_ip, :country)"
)
INSERT_REQUEST = sqlalchemy.text(
    """
    INSERT INTO requests
        (client_ip, gender, age, income, time_of_day, requested_file)
    VALUES
        (:client_ip, :gender, :age, :income, :time_of_day, :requested_file)
    """
)
INSERT_FAILED = sqlalchemy.text(
    "INSERT INTO failed_requests (requested_file, error_code) VALUES (:requested_file, :error_code)"
)

# Countries and IPs already written, so we skip those inserts.
_known_countries: set[str] = set()
_known_ips: set[str] = set()


# --------------------------------------------------------------------------
# Timing instrumentation
# --------------------------------------------------------------------------

class Timings:
    """Thread-safe running totals of how long each operation takes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._stats: dict[str, dict] = {}
        self.requests = 0

    def record(self, op: str, elapsed_ns: int):
        with self._lock:
            s = self._stats.setdefault(
                op, {"count": 0, "total_ns": 0, "min_ns": None, "max_ns": 0}
            )
            s["count"] += 1
            s["total_ns"] += elapsed_ns
            s["min_ns"] = elapsed_ns if s["min_ns"] is None else min(s["min_ns"], elapsed_ns)
            s["max_ns"] = max(s["max_ns"], elapsed_ns)

    def request_done(self):
        with self._lock:
            self.requests += 1
            n = self.requests
        if n % TIMING_SUMMARY_EVERY == 0:
            print(self.summary(), flush=True)

    def as_dict(self) -> dict:
        with self._lock:
            out = {"requests": self.requests, "operations": {}}
            for op, s in self._stats.items():
                out["operations"][op] = {
                    "count": s["count"],
                    "total_s": s["total_ns"] / 1e9,
                    "mean_ms": s["total_ns"] / s["count"] / 1e6,
                    "min_ms": s["min_ns"] / 1e6,
                    "max_ms": s["max_ns"] / 1e6,
                }
            return out

    def summary(self) -> str:
        d = self.as_dict()
        lines = [f"--- Timing summary after {d['requests']} requests ---",
                 f"{'operation':<24}{'count':>9}{'mean ms':>11}{'min ms':>10}{'max ms':>11}{'total s':>11}"]
        for op, s in d["operations"].items():
            lines.append(
                f"{op:<24}{s['count']:>9}{s['mean_ms']:>11.3f}{s['min_ms']:>10.3f}"
                f"{s['max_ms']:>11.3f}{s['total_s']:>11.2f}"
            )
        return "\n".join(lines)

    def save(self, path: str):
        with open(path, "w") as f:
            json.dump(self.as_dict(), f, indent=2)


timings = Timings()


def timed(op_name: str):
    """Decorator: time each call with the high-resolution perf_counter_ns clock."""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            start = time.perf_counter_ns()
            try:
                return func(*args, **kwargs)
            finally:
                timings.record(op_name, time.perf_counter_ns() - start)
        return wrapper
    return decorator


# --------------------------------------------------------------------------
# The four instrumented steps
# --------------------------------------------------------------------------

@dataclass
class RequestInfo:
    filename: str
    country: str
    client_ip: str
    gender: str | None
    age: str | None
    income: str | None
    time_of_day: str | None
    is_banned: bool


@timed("extract_request_info")
def extract_request_info(handler: "RequestHandler", filename: str) -> RequestInfo:
    headers = handler.headers
    # curl requests without these headers still get recorded, with placeholders.
    country = (headers.get("X-country") or "Unknown").strip()
    return RequestInfo(
        filename=filename,
        country=country,
        client_ip=headers.get("X-client-IP") or handler.client_address[0],
        gender=headers.get("X-gender"),
        age=headers.get("X-age"),
        income=headers.get("X-income"),
        time_of_day=headers.get("X-time"),
        is_banned=is_forbidden_country(country),
    )


@timed("get_file_from_bucket")
def get_file_from_bucket(filename: str) -> None | bytes:
    blob = bucket.blob(f"{PREFIX}/{filename}")
    try:
        return blob.download_as_bytes()
    except NotFound:
        print(f"The filename {filename} does not exist in GCS")

        logging.warning(
            f"File not found: {filename}",
            extra={"json_fields": {"status": 404, "filename": filename}},
        )

        return None


@timed("send_response")
def send_response(handler: "RequestHandler", status: int, body, content_type: str):
    handler._send(status, body, content_type)


@timed("write_to_database")
def write_to_database(info: RequestInfo, status: int):
    try:
        with engine.begin() as conn:  # one transaction, committed on exit
            # Parent rows first, so the foreign keys are satisfied:
            # countries <- ips <- requests.
            if info.country not in _known_countries:
                conn.execute(INSERT_COUNTRY, {"country": info.country, "is_banned": info.is_banned})
            if info.client_ip not in _known_ips:
                conn.execute(INSERT_IP, {"client_ip": info.client_ip, "country": info.country})
            conn.execute(
                INSERT_REQUEST,
                {
                    "client_ip": info.client_ip,
                    "gender": info.gender,
                    "age": info.age,
                    "income": info.income,
                    "time_of_day": info.time_of_day,
                    "requested_file": info.filename,
                },
            )
            if status != 200:
                conn.execute(
                    INSERT_FAILED,
                    {"requested_file": info.filename, "error_code": status},
                )
        _known_countries.add(info.country)
        _known_ips.add(info.client_ip)
    except Exception:
        # The response has already been sent; don't let a DB problem crash the handler.
        logging.exception(f"Failed to write request for {info.filename} to the database")


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class RequestHandler(BaseHTTPRequestHandler):
    """Handles incoming HTTP requests. Add a do_<METHOD> method per verb you support."""

    def _send(self, status, body, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, filename: str):
        """Shared logic for GET and POST."""
        info = extract_request_info(self, filename)

        if info.is_banned:
            event = {
                "status": 403,
                "country": info.country,
                "filename": filename,
                "method": self.command,
                "client_ip": info.client_ip,
            }

            logging.critical(
                f"Access denied: request from forbidden country '{info.country}' for {filename}",
                extra={"json_fields": event},
            )

            publisher.publish(forbidden_topic_path, json.dumps(event).encode("utf-8"))

            status = 403
            send_response(self, status, "Access denied\n", "text/plain; charset=utf-8")

        else:
            contents = get_file_from_bucket(filename)
            if contents is None:
                status = 404
                send_response(self, status, f"File not found: {filename}\n", "text/html; charset=utf-8")
            else:
                status = 200
                content_type = mimetypes.guess_type(filename)[0] or "text/plain"
                send_response(self, status, contents, content_type)

        write_to_database(info, status)
        timings.request_done()

    def do_GET(self):
        filename = unquote(urlsplit(self.path).path).lstrip("/")
        self._handle(filename)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length))
        self._handle(data.get("filename"))

    def __getattr__(self, name):
        # Only called when normal attribute lookup fails, so do_GET and do_POST
        # are found as usual. Any other do_<METHOD> lands here.
        if name.startswith("do_"):
            return self._method_not_implemented
        raise AttributeError(name)

    def _method_not_implemented(self):
        logging.error(
            f"Unsupported HTTP method: {self.command} {self.path}",
            extra={"json_fields": {"status": 501, "method": self.command, "path": self.path}},
        )
        filename = unquote(urlsplit(self.path).path).lstrip("/")
        info = extract_request_info(self, filename)
        send_response(self, 501, f"Method {self.command} not implemented\n", "text/plain; charset=utf-8")
        write_to_database(info, 501)
        timings.request_done()


def is_forbidden_country(country: str | None) -> bool:
    return country is not None and country.strip().lower() in FORBIDDEN_COUNTRIES


def _handle_sigterm(signum, frame):
    raise KeyboardInterrupt


def main():
    # Treat `kill` (SIGTERM) like Ctrl+C so the timing results still get saved.
    signal.signal(signal.SIGTERM, _handle_sigterm)

    server = ThreadingHTTPServer((HOST, PORT), RequestHandler)
    print(f"Serving on http://{HOST}:{PORT} (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
    finally:
        server.server_close()
        print(timings.summary())
        timings.save(TIMINGS_FILE)
        print(f"Timing results saved to {TIMINGS_FILE}")
        engine.dispose()
        connector.close()


if __name__ == "__main__":
    main()