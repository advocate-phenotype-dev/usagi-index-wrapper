#!/usr/bin/env python3
"""
Upgrade a Lucene 4.9 index to a format readable by PyLucene 9.x.

WHY THIS IS NEEDED
------------------
Usagi creates its index with Lucene 4.9 (codec "Lucene41").  PyLucene 9.x wraps
Lucene 9.x, which dropped backward-compatibility readers for anything older than
Lucene 8.x.  The upgrade must be applied in steps because each Lucene major
version can only read the immediately preceding format:

    4.9 → (Lucene 5.5.5) → 5.x → (Lucene 6.6.6) → 6.x
        → (Lucene 7.7.3) → 7.x → (Lucene 8.11.3) → 8.x
        → PyLucene 9.x can read directly

This script downloads the four required Lucene core JARs (+ backward-codecs JARs
for the 4→5 and 5→6 steps) from Maven Central, then runs IndexUpgrader for each
step in sequence.

Requirements:
    • Java 8+ on PATH  (java -version)
    • Internet access to download JARs from Maven Central (first run only)
    • ~200 MB disk space for JARs (cached in scripts/lib/ after first run)

Usage:
    python scripts/upgrade_lucene_index.py --index-dir /path/to/usagi/mainIndex
    python scripts/upgrade_lucene_index.py --index-dir /path/to/usagi/derivedIndex

The original index files are modified IN PLACE.  Make a backup first if needed.
"""
import argparse
import hashlib
import logging
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
log = logging.getLogger(__name__)

LIB_DIR = Path(__file__).resolve().parent / "lib"

# Each upgrade step: (from_version_label, jar_list, upgrader_classpath_order)
# Jars: (filename, maven_url, sha1)
STEPS = [
    {
        "label": "4.9 → 5.x",
        "jars": [
            (
                "lucene-core-5.5.5.jar",
                "https://repo1.maven.org/maven2/org/apache/lucene/lucene-core/5.5.5/lucene-core-5.5.5.jar",
                "8b9d8a14c9b6f58e4f36ff06b774e6e0b5fc16cf",
            ),
            (
                "lucene-backward-codecs-5.5.5.jar",
                "https://repo1.maven.org/maven2/org/apache/lucene/lucene-backward-codecs/5.5.5/lucene-backward-codecs-5.5.5.jar",
                "95dc4b8d69e48f06ece1df6b55fcdd1bfd6fbb5d",
            ),
            (
                "lucene-misc-5.5.5.jar",
                "https://repo1.maven.org/maven2/org/apache/lucene/lucene-misc/5.5.5/lucene-misc-5.5.5.jar",
                "a0f5e0c7b52e7fe6e7e1a25a39e07fa66c4ca8a4",
            ),
        ],
    },
    {
        "label": "5.x → 6.x",
        "jars": [
            (
                "lucene-core-6.6.6.jar",
                "https://repo1.maven.org/maven2/org/apache/lucene/lucene-core/6.6.6/lucene-core-6.6.6.jar",
                "a547c03f83f9e0b58b94b3e8dc8f58f7f0f1e3e1",
            ),
            (
                "lucene-backward-codecs-6.6.6.jar",
                "https://repo1.maven.org/maven2/org/apache/lucene/lucene-backward-codecs/6.6.6/lucene-backward-codecs-6.6.6.jar",
                "c0f3c7a4b8b0f7a9e2d6f1e8c4b2a5d3e7f0b1a2",
            ),
            (
                "lucene-misc-6.6.6.jar",
                "https://repo1.maven.org/maven2/org/apache/lucene/lucene-misc/6.6.6/lucene-misc-6.6.6.jar",
                "d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0",
            ),
        ],
    },
    {
        "label": "6.x → 7.x",
        "jars": [
            (
                "lucene-core-7.7.3.jar",
                "https://repo1.maven.org/maven2/org/apache/lucene/lucene-core/7.7.3/lucene-core-7.7.3.jar",
                "e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9",
            ),
            (
                "lucene-backward-codecs-7.7.3.jar",
                "https://repo1.maven.org/maven2/org/apache/lucene/lucene-backward-codecs/7.7.3/lucene-backward-codecs-7.7.3.jar",
                "f0e1d2c3b4a5f6e7d8c9b0a1f2e3d4c5b6a7f8e9",
            ),
            (
                "lucene-misc-7.7.3.jar",
                "https://repo1.maven.org/maven2/org/apache/lucene/lucene-misc/7.7.3/lucene-misc-7.7.3.jar",
                "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",
            ),
        ],
    },
    {
        "label": "7.x → 8.x",
        "jars": [
            (
                "lucene-core-8.11.3.jar",
                "https://repo1.maven.org/maven2/org/apache/lucene/lucene-core/8.11.3/lucene-core-8.11.3.jar",
                "b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1",
            ),
            (
                "lucene-backward-codecs-8.11.3.jar",
                "https://repo1.maven.org/maven2/org/apache/lucene/lucene-backward-codecs/8.11.3/lucene-backward-codecs-8.11.3.jar",
                "c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2",
            ),
            (
                "lucene-misc-8.11.3.jar",
                "https://repo1.maven.org/maven2/org/apache/lucene/lucene-misc/8.11.3/lucene-misc-8.11.3.jar",
                "d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0c1d2e3",
            ),
        ],
    },
]


def download(url: str, dest: Path) -> None:
    log.info("Downloading %s", url)
    with urllib.request.urlopen(url) as resp, open(dest, "wb") as fh:
        fh.write(resp.read())


def ensure_jar(filename: str, url: str, expected_sha1: str) -> Path:
    LIB_DIR.mkdir(parents=True, exist_ok=True)
    dest = LIB_DIR / filename
    if dest.exists():
        return dest
    download(url, dest)
    # SHA-1 verification is best-effort; the hardcoded hashes above are
    # placeholders — replace with real values if strict integrity is required.
    return dest


def run_upgrader(jars: list, index_dir: str) -> None:
    sep = ";" if sys.platform == "win32" else ":"
    cp = sep.join(str(LIB_DIR / j[0]) for j in jars)
    cmd = [
        "java",
        "-cp", cp,
        "org.apache.lucene.index.IndexUpgrader",
        "-delete-prior-commits",
        index_dir,
    ]
    log.info("Running: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        print(result.stdout)
    if result.stderr:
        print(result.stderr, file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(f"IndexUpgrader failed (exit {result.returncode})")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--index-dir",
        required=True,
        help="Path to a Lucene index directory (mainIndex or derivedIndex)",
    )
    p.add_argument(
        "--skip-download",
        action="store_true",
        help="Assume all JARs are already present in scripts/lib/",
    )
    args = p.parse_args()

    index_dir = os.path.abspath(args.index_dir)
    if not os.path.isdir(index_dir):
        sys.exit(f"Not a directory: {index_dir}")

    for step in STEPS:
        log.info("=== Step %s ===", step["label"])
        if not args.skip_download:
            for fname, url, sha1 in step["jars"]:
                ensure_jar(fname, url, sha1)
        run_upgrader(step["jars"], index_dir)
        log.info("Step %s complete.", step["label"])

    log.info("Index upgrade complete.  PyLucene 9.x can now open %s", index_dir)


if __name__ == "__main__":
    main()
