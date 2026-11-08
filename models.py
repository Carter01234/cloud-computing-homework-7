#!/usr/bin/env python3
"""
models.py - HW6: train two models on the request data in Cloud SQL.

Model 1: client IP  -> country   (target: 99%+ test accuracy)
Model 2: request fields -> income (target: 35%+ test accuracy)

Each model is evaluated on a held-out test set (20% of the rows). The test-set
predictions are written as separate CSV files to the Cloud Storage bucket from
HW5, together with a plain-text summary:

    gs://html-files-for-class/hw6/model1_ip_to_country_test.csv
    gs://html-files-for-class/hw6/model2_income_test.csv
    gs://html-files-for-class/hw6/results_summary.txt

How the data was generated (from the provided http-client), and why the
models look the way they do:
  * Each country owns 1-9 random network blocks. An IP's first three octets
    encode its block, and the last octet is random. So the first three octets
    determine the country exactly.
  * Income is chosen as (age group's position + one of -1, 0, +1), wrapping
    around the list, with equal probability. All other fields are drawn
    independently. So given the age group, income is one of three equally
    likely values: no model can beat about 33% on average.

Runs on the HW6 VM (see run_models.sh); also works locally with
`gcloud auth application-default login`.
"""
import io
import os
import sys
import time

import pandas as pd
from google.cloud import secretmanager, storage
from google.cloud.sql.connector import Connector
from sklearn.metrics import accuracy_score
from sklearn.model_selection import cross_val_score, train_test_split
from sklearn.naive_bayes import CategoricalNB
from sklearn.preprocessing import OrdinalEncoder
from sklearn.tree import DecisionTreeClassifier

# Hardcoded on purpose (the grader's gcloud config must not change these).
PROJECT_ID = "project-8aeecca0-4f70-4c6b-8e3"
INSTANCE_CONNECTION_NAME = f"{PROJECT_ID}:us-east1:my-class-db"
DB_USER = "root"
DB_NAME = "hw5"
DB_PASS_SECRET = "db-root-password"

BUCKET_NAME = "html-files-for-class"
OUTPUT_PREFIX = "hw6"

TEST_SIZE = 0.20
RANDOM_STATE = 42

# Order used by the client when it builds the income header.
AGES = ["0-16", "17-25", "26-35", "36-45", "46-55", "56-65", "66-75", "76+"]
INCOMES = ["0-10k", "10k-20k", "20k-40k", "40k-60k", "60k-100k",
           "100k-150k", "150k-250k", "250k+"]


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------

def get_secret(secret_id: str) -> str:
    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{PROJECT_ID}/secrets/{secret_id}/versions/latest"
    return client.access_secret_version(request={"name": name}).payload.data.decode()


def load_data() -> pd.DataFrame:
    """Read every request with its country from Cloud SQL."""
    connector = Connector()
    conn = connector.connect(
        INSTANCE_CONNECTION_NAME, "pymysql",
        user=DB_USER, password=get_secret(DB_PASS_SECRET), db=DB_NAME,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'requests' AND column_name = 'country'",
                (DB_NAME,),
            )
            if cur.fetchone()[0] == 0:
                # 3NF schema: the country is reached through the ips table.
                cur.execute("""
                    SELECT r.request_id, r.client_ip, i.country,
                           r.gender, r.age, r.income, r.time_of_day
                    FROM requests r
                    JOIN ips i USING (client_ip)
                """)
            else:
                # Not migrated yet (HW5 schema): country is still in requests.
                log("Note: database still uses the HW5 schema; run setup_schema.py to migrate.")
                cur.execute("""
                    SELECT request_id, client_ip, country,
                           gender, age, income, time_of_day
                    FROM requests
                """)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
    finally:
        conn.close()
        connector.close()
    return pd.DataFrame(rows, columns=cols)


def clean(df: pd.DataFrame) -> pd.DataFrame:
    """Keep only rows produced by the client (drops curl tests and health checks)."""
    before = len(df)
    df = df.dropna(subset=["client_ip", "country", "gender", "age", "income", "time_of_day"])
    df = df[df["country"] != "Unknown"]
    df = df[df["age"].isin(AGES) & df["income"].isin(INCOMES)]

    octets = df["client_ip"].str.split(".", expand=True)
    valid_ip = (octets.shape[1] == 4) & octets.notna().all(axis=1) \
        & octets.apply(lambda c: c.str.isdigit()).all(axis=1)
    df = df[valid_ip].copy()
    octets = octets[valid_ip].astype(int)

    # Network number = first three octets (the client's "CIDR" block).
    df["network"] = octets[0] * 65536 + octets[1] * 256 + octets[2]
    # "YYYY-MM-DD HH:00:00" -> hour of day
    df["hour"] = pd.to_numeric(df["time_of_day"].str[11:13], errors="coerce")
    df = df.dropna(subset=["hour"])
    df["hour"] = df["hour"].astype(int)

    log(f"Loaded {before} rows; {len(df)} usable after cleaning "
        f"({before - len(df)} test/health-check rows dropped).")
    return df


# --------------------------------------------------------------------------
# Model 1: IP -> country
# --------------------------------------------------------------------------

def model_ip_to_country(df: pd.DataFrame):
    """Decision tree on the IP's network number (first three octets).

    The last octet is random, so it is left out. Each country's network
    blocks form ranges of the network number, which a decision tree can
    separate exactly with threshold splits.
    """
    X = df[["network"]].to_numpy()
    y = df["country"].to_numpy()
    X_train, X_test, y_train, y_test, idx_train, idx_test = train_test_split(
        X, y, df.index, test_size=TEST_SIZE, random_state=RANDOM_STATE, stratify=y)

    model = DecisionTreeClassifier(random_state=RANDOM_STATE)
    model.fit(X_train, y_train)
    pred = model.predict(X_test)
    acc = accuracy_score(y_test, pred)

    test = df.loc[idx_test, ["request_id", "client_ip", "network"]].copy()
    test["actual_country"] = y_test
    test["predicted_country"] = pred
    test["correct"] = test["actual_country"] == test["predicted_country"]

    info = {
        "train_rows": len(X_train), "test_rows": len(X_test),
        "accuracy": acc,
        "countries": df["country"].nunique(),
        "networks": df["network"].nunique(),
        "tree_leaves": model.get_n_leaves(),
    }
    return test, info


# --------------------------------------------------------------------------
# Model 2: fields -> income
# --------------------------------------------------------------------------

FEATURE_SETS = {
    "age": ["age"],
    "age + gender": ["age", "gender"],
    "age + gender + hour": ["age", "gender", "hour"],
    "age + gender + hour + country": ["age", "gender", "hour", "country"],
}


def model_income(df: pd.DataFrame):
    """Categorical Naive Bayes on request fields, feature set chosen by cross-validation.

    All candidate inputs are categorical, so Categorical Naive Bayes fits
    naturally: it learns P(income) and P(feature | income) by counting, and
    predicts the most probable income. The feature set is chosen with 5-fold
    cross-validation on the training set only; the test set is used once.
    """
    y = df["income"].to_numpy()
    train_df, test_df = train_test_split(
        df, test_size=TEST_SIZE, random_state=RANDOM_STATE, stratify=y)

    cv_scores = {}
    for name, cols in FEATURE_SETS.items():
        enc = OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1)
        Xtr = enc.fit_transform(train_df[cols].astype(str))
        nb = CategoricalNB(min_categories=(Xtr.max(axis=0) + 1).astype(int))
        cv_scores[name] = cross_val_score(nb, Xtr, train_df["income"], cv=5).mean()
    best = max(cv_scores, key=cv_scores.get)
    cols = FEATURE_SETS[best]

    enc = OrdinalEncoder(handle_unknown="use_encoded_value", unknown_value=-1)
    Xtr = enc.fit_transform(train_df[cols].astype(str))
    Xte = enc.transform(test_df[cols].astype(str))
    Xte[Xte < 0] = 0  # unseen category (does not happen with this data)
    model = CategoricalNB(min_categories=(Xtr.max(axis=0) + 1).astype(int))
    model.fit(Xtr, train_df["income"])
    pred = model.predict(Xte)
    acc = accuracy_score(test_df["income"], pred)

    # Best achievable on this test set when predicting from age: for each age
    # group, the share of its most common income. The client draws income
    # uniformly from 3 values per age, so this sits near 1/3.
    ceiling = (test_df.groupby("age")["income"]
               .agg(lambda s: s.value_counts().iloc[0]).sum() / len(test_df))

    test = test_df[["request_id", "age", "gender", "hour", "country"]].copy()
    test["actual_income"] = test_df["income"].to_numpy()
    test["predicted_income"] = pred
    test["correct"] = test["actual_income"] == test["predicted_income"]

    # What the model learned: predicted income for each age group.
    if cols == ["age"]:
        learned = {a: model.predict(enc.transform(pd.DataFrame({"age": [a]})))[0]
                   for a in AGES}
    else:
        learned = None

    info = {
        "train_rows": len(train_df), "test_rows": len(test_df),
        "accuracy": acc, "features": best, "cv_scores": cv_scores,
        "age_ceiling": ceiling, "learned": learned,
        "majority_baseline": test_df["income"].value_counts(normalize=True).iloc[0],
    }
    return test, info


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------

def summary_text(info1: dict, info2: dict) -> str:
    lines = [
        "HW6 model results",
        "=================",
        "",
        "Model 1: client IP -> country",
        "  Model:     decision tree on the IP's network number (first three octets)",
        f"  Data:      {info1['train_rows']} train / {info1['test_rows']} test rows, "
        f"{info1['countries']} countries, {info1['networks']} networks",
        f"  Accuracy:  {info1['accuracy']:.4%} on the test set (target 99%)",
        "",
        "Model 2: request fields -> income",
        "  Model:     categorical Naive Bayes; feature set chosen by 5-fold CV",
        "  CV accuracy by feature set (training data):",
    ]
    for name, score in info2["cv_scores"].items():
        mark = "  <- chosen" if name == info2["features"] else ""
        lines.append(f"    {name:<32} {score:.2%}{mark}")
    lines += [
        f"  Data:      {info2['train_rows']} train / {info2['test_rows']} test rows",
        f"  Accuracy:  {info2['accuracy']:.2%} on the test set (target 35%)",
        f"  Baseline:  {info2['majority_baseline']:.2%} (always predict the most common income)",
        f"  Ceiling:   {info2['age_ceiling']:.2%} (best possible from age on this test set)",
    ]
    if info2["learned"]:
        lines.append("  Learned rule (age group -> predicted income):")
        for a, inc in info2["learned"].items():
            lines.append(f"    {a:<6} -> {inc}")
    lines += [
        "",
        "  Note: the client sets income = age position + (-1, 0 or +1), each",
        "  equally likely, and draws every other field independently. So about",
        "  33% is the most any model can expect; results above that come from",
        "  random fluctuations in the generated data, not from a better model.",
        "",
        f"Test-set predictions: gs://{BUCKET_NAME}/{OUTPUT_PREFIX}/",
    ]
    return "\n".join(lines) + "\n"


def upload(name: str, data: str, content_type: str):
    blob = storage.Client(project=PROJECT_ID).bucket(BUCKET_NAME).blob(f"{OUTPUT_PREFIX}/{name}")
    blob.upload_from_string(data, content_type=content_type)
    log(f"Wrote gs://{BUCKET_NAME}/{OUTPUT_PREFIX}/{name}")


def to_csv(df: pd.DataFrame) -> str:
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    return buf.getvalue()


def main():
    log("Reading requests from Cloud SQL...")
    df = clean(load_data())

    log("Training model 1 (IP -> country)...")
    test1, info1 = model_ip_to_country(df)
    log(f"Model 1 test accuracy: {info1['accuracy']:.4%}")

    log("Training model 2 (fields -> income)...")
    test2, info2 = model_income(df)
    log(f"Model 2 test accuracy: {info2['accuracy']:.2%} (features: {info2['features']})")

    summary = summary_text(info1, info2)
    print("\n" + summary, flush=True)

    upload("model1_ip_to_country_test.csv", to_csv(test1), "text/csv")
    upload("model2_income_test.csv", to_csv(test2), "text/csv")
    upload("results_summary.txt", summary, "text/plain")
    log("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
