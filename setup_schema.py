"""
setup_schema.py - create (or migrate to) the 3NF schema on Cloud SQL (MySQL).

Schema (3rd normal form):
    countries(country PK, is_banned)                    country   -> is_banned
    ips(client_ip PK, country FK)                       client_ip -> country
    requests(request_id PK, client_ip FK, gender, age, income, time_of_day, requested_file)
    failed_requests(id PK, time_of_request, requested_file, error_code)

The HW5 schema stored `country` in `requests`. Since client_ip -> country, that
was a transitive dependency (request_id -> client_ip -> country), violating 3NF.

What this script does:
  * Fresh database: creates the four tables above.
  * HW5 database (requests still has a `country` column): moves the data into
    the new tables, then renames the old table to `requests_hw5` as a backup.
  * Already migrated: nothing (safe to run any number of times).

The same migration is written out as plain SQL in migrate_to_3nf.sql.

Requirements (on whatever machine runs this):
    pip install "cloud-sql-python-connector[pymysql]" "pymysql==1.2.0" google-cloud-secret-manager
    gcloud auth application-default login   # or a VM service account with
                                            # Cloud SQL Client + Secret Accessor

Usage:
    python3 setup_schema.py
The database password is read from Secret Manager (secret: db-root-password).
Setting DB_PASS in the environment overrides it, e.g. for local testing.
"""
import os

from google.cloud import secretmanager
from google.cloud.sql.connector import Connector

# Hardcode your project ID: the assignment says not to look it up dynamically.
PROJECT_ID = "project-8aeecca0-4f70-4c6b-8e3"
REGION = "us-east1"
INSTANCE = "my-class-db"
INSTANCE_CONNECTION_NAME = f"{PROJECT_ID}:{REGION}:{INSTANCE}"

DB_USER = os.environ.get("DB_USER", "root")
DB_NAME = os.environ.get("DB_NAME", "hw5")
DB_PASS_SECRET = "db-root-password"


def get_secret(secret_id: str, version: str = "latest") -> str:
    """Fetch a secret's value from Google Secret Manager."""
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/{version}"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data.decode("utf-8")


# --------------------------------------------------------------------------
# Table definitions
# --------------------------------------------------------------------------

COUNTRIES_DDL = """
    CREATE TABLE IF NOT EXISTS countries (
        country    VARCHAR(64) PRIMARY KEY,
        is_banned  BOOLEAN NOT NULL
    )
"""

# client_ip -> country: each IP always comes from the same country.
IPS_DDL = """
    CREATE TABLE IF NOT EXISTS ips (
        client_ip  VARCHAR(45) PRIMARY KEY,   -- 45 chars fits IPv6
        country    VARCHAR(64) NOT NULL,
        CONSTRAINT fk_ips_country FOREIGN KEY (country) REFERENCES countries(country)
    )
"""


def requests_ddl(table: str) -> str:
    """One row per request. No country column: it is reached through ips."""
    return f"""
    CREATE TABLE IF NOT EXISTS `{table}` (
        request_id      BIGINT AUTO_INCREMENT PRIMARY KEY,
        client_ip       VARCHAR(45) NOT NULL,
        gender          VARCHAR(16),
        age             VARCHAR(16),          -- age group string, e.g. '17-25'
        income          VARCHAR(32),          -- income group string
        time_of_day     VARCHAR(32),          -- value of the client's time header
        requested_file  VARCHAR(255) NOT NULL,
        CONSTRAINT fk_requests_client_ip FOREIGN KEY (client_ip) REFERENCES ips(client_ip)
    )
    """


# Failed requests (any status other than 200): only time, file, and code.
FAILED_DDL = """
    CREATE TABLE IF NOT EXISTS failed_requests (
        id              BIGINT AUTO_INCREMENT PRIMARY KEY,
        time_of_request TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
        requested_file  VARCHAR(255) NOT NULL,
        error_code      SMALLINT NOT NULL
    )
"""


# --------------------------------------------------------------------------
# Migration from the HW5 schema
# --------------------------------------------------------------------------

# IPs that appear with more than one country break client_ip -> country.
# In our data these are only manual curl tests (e.g. 1.2.3.4 sent as both
# France and Iran), so their rows are left out of the new tables. They stay
# available in the requests_hw5 backup.
CONFLICTING_IPS = """
    SELECT client_ip
    FROM requests
    GROUP BY client_ip
    HAVING COUNT(DISTINCT country) > 1
"""


def table_exists(cur, table: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.tables "
        "WHERE table_schema = %s AND table_name = %s",
        (DB_NAME, table),
    )
    return cur.fetchone()[0] > 0


def column_exists(cur, table: str, column: str) -> bool:
    cur.execute(
        "SELECT COUNT(*) FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = %s AND column_name = %s",
        (DB_NAME, table, column),
    )
    return cur.fetchone()[0] > 0


def migrate_from_hw5(conn, cur):
    """Move data from the HW5 `requests` table (with country) into ips + new requests."""
    print("Found the HW5 schema (requests.country); migrating to 3NF...")

    if table_exists(cur, "requests_hw5"):
        raise RuntimeError(
            "Table requests_hw5 already exists, so the backup name is taken. "
            "Rename or drop it, then run this script again."
        )

    # Start from clean staging tables in case an earlier run stopped partway.
    # Nothing references ips or requests_3nf yet, so this loses no data.
    cur.execute("DROP TABLE IF EXISTS requests_3nf")
    cur.execute("DELETE FROM ips")
    cur.execute(requests_ddl("requests_3nf"))
    conn.commit()

    cur.execute(f"SELECT COUNT(*) FROM ({CONFLICTING_IPS}) AS bad")
    n_bad_ips = cur.fetchone()[0]
    cur.execute(f"""
        SELECT COUNT(*) FROM requests
        WHERE client_ip IN (SELECT client_ip FROM ({CONFLICTING_IPS}) AS bad)
    """)
    n_skipped = cur.fetchone()[0]
    if n_bad_ips:
        print(f"  {n_bad_ips} IP(s) have more than one country; skipping their {n_skipped} row(s).")

    try:
        # 1. One row per IP with its (single) country.
        cur.execute(f"""
            INSERT INTO ips (client_ip, country)
            SELECT client_ip, MIN(country)
            FROM requests
            WHERE client_ip NOT IN (SELECT client_ip FROM ({CONFLICTING_IPS}) AS bad)
            GROUP BY client_ip
        """)
        n_ips = cur.rowcount

        # 2. Every request, without the now-redundant country column.
        cur.execute(f"""
            INSERT INTO requests_3nf
                (request_id, client_ip, gender, age, income, time_of_day, requested_file)
            SELECT request_id, client_ip, gender, age, income, time_of_day, requested_file
            FROM requests
            WHERE client_ip NOT IN (SELECT client_ip FROM ({CONFLICTING_IPS}) AS bad)
        """)
        n_moved = cur.rowcount

        # 3. Check that no rows were lost.
        cur.execute("SELECT COUNT(*) FROM requests")
        n_old = cur.fetchone()[0]
        if n_moved != n_old - n_skipped:
            raise RuntimeError(f"Row count mismatch: moved {n_moved}, expected {n_old - n_skipped}")

        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # 4. Swap the tables in one atomic step, keeping the old one as a backup.
    cur.execute("RENAME TABLE requests TO requests_hw5, requests_3nf TO requests")
    conn.commit()

    print(f"  ips: {n_ips} rows; requests: {n_moved} of {n_old} rows moved.")
    print("  Old table kept as requests_hw5 (drop it once you're satisfied).")


# --------------------------------------------------------------------------

def main():
    db_pass = os.environ.get("DB_PASS") or get_secret(DB_PASS_SECRET)
    connector = Connector()
    conn = connector.connect(
        INSTANCE_CONNECTION_NAME,
        "pymysql",
        user=DB_USER,
        password=db_pass,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}`")
            cur.execute(f"USE `{DB_NAME}`")

            for ddl in (COUNTRIES_DDL, IPS_DDL, FAILED_DDL):
                cur.execute(ddl)
            conn.commit()

            if not table_exists(cur, "requests"):
                cur.execute(requests_ddl("requests"))
                conn.commit()
                print("Created the 3NF schema.")
            elif column_exists(cur, "requests", "country"):
                migrate_from_hw5(conn, cur)
            else:
                print("Schema is already in 3NF; nothing to do.")

            cur.execute("SHOW TABLES")
            print(f"Tables in {DB_NAME}:", [row[0] for row in cur.fetchall()])
    finally:
        conn.close()
        connector.close()


if __name__ == "__main__":
    main()
