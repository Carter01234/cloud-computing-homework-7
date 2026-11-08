"""Homework 7: link analysis and word-trigram counts with Apache Beam.

Reads every HTML page under an input pattern (by default the pages in
gs://html-files-for-class/pages/) and computes, in a single Beam pipeline:

  1. the 5 pages with the most incoming links,
  2. the 5 pages with the most outgoing links,
  3. the 5 most frequent word trigrams (three consecutive words).

The same code runs locally (FnApiRunner) or on Google Cloud Dataflow
(DataflowRunner); only the pipeline options differ. See run_local.sh and
run_dataflow.sh.

Definitions used:
  * A link is an <a href="..."> tag. Each distinct (source page, target page)
    pair counts once, so a page that links to the same target twice adds one
    outgoing link and gives the target one incoming link.
  * Words are runs of letters/digits after HTML tags are removed, lowercased.
    Trigrams are formed within a single page, never across two pages.
"""

import argparse
import json
import logging
import os
import posixpath
import re
import time

import apache_beam as beam
from apache_beam.io import fileio
from apache_beam.io.filesystems import FileSystems
from apache_beam.options.pipeline_options import PipelineOptions, SetupOptions

TOP_N = 5

LINK_RE = re.compile(r'<a\s[^>]*?href\s*=\s*["\']?([^"\'\s>]+)', re.IGNORECASE)
TAG_RE = re.compile(r"<[^>]*>")
WORD_RE = re.compile(r"[a-z0-9]+")


# ---------------------------------------------------------------- helpers
def page_name(path):
    """'gs://bucket/pages/12.html' -> '12.html'."""
    return posixpath.basename(path)


class ReadPage(beam.DoFn):
    """File metadata -> (page name, html text).

    Beam's built-in ReadMatches builds a brand-new GCS client (with its own
    connection pool and credentials) for every file it opens. Over 10,000
    pages that exhausts the laptop's file descriptors, which surfaces as DNS
    errors. Instead, each worker creates one GCS client in setup() and reuses
    it, and every file is closed as soon as it has been read.
    """

    def __init__(self, options_dict):
        self._options_dict = options_dict
        self._gcs = None

    def setup(self):
        self._gcs = None  # created lazily, only if a gs:// path shows up

    def process(self, metadata):
        path = metadata.path
        if path.startswith("gs://"):
            if self._gcs is None:
                from apache_beam.io.gcp import gcsio
                self._gcs = gcsio.GcsIO(
                    pipeline_options=PipelineOptions.from_dictionary(self._options_dict))
            with self._gcs.open(path) as f:
                data = f.read()
        else:
            with FileSystems.open(path) as f:
                data = f.read()
        yield page_name(path), data.decode("utf-8", errors="replace")


def extract_links(page):
    """(name, html) -> the distinct pages it links to, as (source, target) pairs."""
    name, html = page
    targets = {page_name(href.split("#")[0]) for href in LINK_RE.findall(html)}
    targets.discard("")
    return [(name, target) for target in sorted(targets)]


def extract_trigrams(page):
    """(name, html) -> every 'w1 w2 w3' trigram in the page's visible text."""
    _, html = page
    words = WORD_RE.findall(TAG_RE.sub(" ", html).lower())
    return [" ".join(words[i:i + 3]) for i in range(len(words) - 2)]


def rank_key(item):
    """Sort by count, ties broken by name so results are deterministic."""
    key, count = item
    return count, key


def format_top(title, top_list):
    """[(key, count), ...] -> one printable text block."""
    lines = [title]
    for rank, (key, count) in enumerate(top_list, start=1):
        lines.append(f"  {rank}. {key}: {count}")
    return "\n".join(lines)


# ---------------------------------------------------------------- pipeline
def worker_options_dict(options):
    """Options for ReadPage's GCS client, as a plain dict.

    On Dataflow the worker VMs already run as the service account
    (--service_account_email), so the laptop's --impersonate_service_account
    setting must not travel with them: the account would try to impersonate
    itself and get a 403 (iam.serviceAccounts.getAccessToken). Locally the
    setting is kept so the laptop reads the bucket as the service account.
    """
    opts = dict(options.get_all_options())
    if "Dataflow" in str(opts.get("runner") or ""):
        opts.pop("impersonate_service_account", None)
    return opts


def build_pipeline(p, options, input_pattern, output_dir):
    pages = (
        p
        | "MatchPages" >> fileio.MatchFiles(input_pattern)
        # Spread the file list across workers before reading (matters on Dataflow).
        | "Redistribute" >> beam.Reshuffle()
        | "ReadPages" >> beam.ParDo(ReadPage(worker_options_dict(options)))
    )

    links = pages | "ExtractLinks" >> beam.FlatMap(extract_links)

    # Outgoing links: each page already knows its own links, so no shuffle needed.
    outgoing = (
        links
        | "SourceAsKey" >> beam.Map(lambda edge: (edge[0], 1))
        | "SumOutgoing" >> beam.CombinePerKey(sum)
        | "TopOutgoing" >> beam.combiners.Top.Of(TOP_N, key=rank_key)
    )

    # Incoming links: group the edges by their target page and count them.
    incoming = (
        links
        | "TargetAsKey" >> beam.Map(lambda edge: (edge[1], 1))
        | "SumIncoming" >> beam.CombinePerKey(sum)
        | "TopIncoming" >> beam.combiners.Top.Of(TOP_N, key=rank_key)
    )

    trigrams = (
        pages
        | "ExtractTrigrams" >> beam.FlatMap(extract_trigrams)
        | "CountTrigrams" >> beam.combiners.Count.PerElement()
        | "TopTrigrams" >> beam.combiners.Top.Of(TOP_N, key=rank_key)
    )

    results = {
        "incoming": (incoming, f"Top {TOP_N} pages by incoming links"),
        "outgoing": (outgoing, f"Top {TOP_N} pages by outgoing links"),
        "trigrams": (trigrams, f"Top {TOP_N} word trigrams"),
    }
    for name, (pcoll, title) in results.items():
        (
            pcoll
            | f"Format_{name}" >> beam.Map(lambda top, t=title: format_top(t, top))
            | f"Write_{name}" >> beam.io.WriteToText(
                FileSystems.join(output_dir, name),
                file_name_suffix=".txt",
                num_shards=1,
                shard_name_template="",
            )
        )
    return list(results)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--input",
        default="gs://html-files-for-class/pages/*.html",
        help="File pattern of the HTML pages to analyze.",
    )
    parser.add_argument(
        "--output",
        default="output/local",
        help="Directory (local or gs://) where the result files are written.",
    )
    parser.add_argument(
        "--timings_file",
        default="timings.json",
        help="Local JSON file the measured runtime is recorded in.",
    )
    known, beam_args = parser.parse_known_args(argv)

    options = PipelineOptions(beam_args)
    options.view_as(SetupOptions).save_main_session = True
    runner = options.get_all_options().get("runner") or "DirectRunner"

    print(f"Runner: {runner}\nInput:  {known.input}\nOutput: {known.output}\n")
    start = time.perf_counter()
    p = beam.Pipeline(options=options)
    result_names = build_pipeline(p, options, known.input, known.output)
    p.run().wait_until_finish()
    elapsed = time.perf_counter() - start

    for name in result_names:
        with FileSystems.open(FileSystems.join(known.output, f"{name}.txt")) as f:
            print(f.read().decode("utf-8").strip(), end="\n\n")
    print(f"Total runtime ({runner}): {elapsed:.1f} s")

    timings = {}
    if os.path.exists(known.timings_file):
        with open(known.timings_file) as f:
            timings = json.load(f)
    timings[runner] = {
        "seconds": round(elapsed, 1),
        "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    with open(known.timings_file, "w") as f:
        json.dump(timings, f, indent=2)


if __name__ == "__main__":
    logging.getLogger().setLevel(logging.WARNING)
    main()
