# Homework 7 — Link analysis and word trigrams with Apache Beam / Dataflow

One Beam pipeline (`hw7_pipeline.py`) reads every page in
`gs://html-files-for-class/pages/` and reports:

1. the 5 pages with the most **incoming** links,
2. the 5 pages with the most **outgoing** links,
3. the 5 most frequent **word trigrams**.

The same code runs on the laptop (Beam's local `FnApiRunner`, one thread per CPU core) and on Cloud Dataflow
(DataflowRunner); only the pipeline options differ.

## Files

| File | Purpose |
|---|---|
| `hw7_pipeline.py` | The Beam pipeline |
| `config.sh` | Project, region, bucket and service-account names |
| `setup.sh` | Enables APIs, creates `homework7-service-account`, grants roles, builds `.venv` |
| `run_local.sh` | Runs on the laptop with `FnApiRunner` (see note in the script), prints results, saves `output-local.log` |
| `run_dataflow.sh` | Runs on Dataflow, prints results, saves `output-dataflow.log` |
| `timings.json` | Runtime of the latest run on each runner (written by the pipeline) |

## Setup (once)

Requires the gcloud CLI logged in as a project owner, and Python 3.10–3.14.

```bash
cd ~/Desktop/cloud-computing/homework7
./setup.sh
```

`setup.sh`:

- enables the Dataflow, Compute, Storage, Logging, IAM and IAM Credentials APIs;
- creates `homework7-service-account` with `roles/dataflow.admin`,
  `roles/dataflow.worker` and `roles/logging.logWriter` on the project, and
  `roles/storage.objectAdmin` on the bucket;
- gives the service account `roles/iam.serviceAccountUser` on itself (so it
  can launch Dataflow workers as itself) and gives your gcloud user
  `roles/iam.serviceAccountTokenCreator` on it, so both runs **impersonate**
  the service account — no key file is downloaded;
- creates `.venv` and installs `apache-beam[gcp]`.

## Running

```bash
./run_local.sh      # laptop
./run_dataflow.sh   # Cloud Dataflow, us-central1, up to 4 workers
```

Each run prints the three top-5 lists and the total runtime, and records it in
`timings.json`. The Dataflow job can also be watched in the console under
Dataflow → Jobs. Dataflow results are also written to
`gs://html-files-for-class/hw7-output/dataflow/`.

## How the pipeline works

```
MatchFiles(pages/*.html) → Reshuffle → ReadPage → (page, html)
   ├─ FlatMap(extract_links) → distinct (source, target) edges
   │    ├─ key by source → CombinePerKey(sum) → Top 5   (outgoing)
   │    └─ key by target → CombinePerKey(sum) → Top 5   (incoming)
   └─ FlatMap(extract_trigrams) → Count.PerElement → Top 5   (trigrams)
```

- **Reading:** `fileio.MatchFiles` lists the pages, `Reshuffle` spreads the
  list across workers, and the `ReadPage` DoFn reads each one, keeping its file
  name (needed to know a link's source). `ReadPage` creates one GCS client per
  worker and reuses it; Beam's built-in `ReadMatches` opens a new client per
  file, which ran the laptop out of file descriptors on 10,000 pages.
- **Links:** a regex finds every `<a href="...">`. Each distinct
  (source, target) pair is one edge; a page that links to the same target twice
  counts once.
- **Outgoing** is easy because each page already holds its own links: count
  edges per source. **Incoming** needs a shuffle: edges are regrouped by target
  page across all files (`CombinePerKey`) before they can be counted.
- **Trigrams:** HTML tags are stripped, text is lowercased and split into words
  (letters/digits), and every three consecutive words in a page form a
  trigram. Trigrams never cross from one page into another.
- **Top 5:** `combiners.Top.Of` ranks by count; ties are broken by name so the
  output is deterministic.

## Note on ties

The page bodies are repeated *lorem ipsum* paragraphs, so every trigram inside
that paragraph occurs exactly once per paragraph and many trigrams tie for
first place. Likewise several pages tie at 5th place for incoming and outgoing
links. The pipeline breaks ties by name (reverse alphabetical), so a different
but equally valid top 5 is possible under another tie-break rule.
