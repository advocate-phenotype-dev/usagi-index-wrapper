#!/usr/bin/env python3
"""
Smoke-test the running Usagi Search service.

Usage (service must already be running on localhost:8000):

    python test_service.py
    python test_service.py --url http://localhost:8000 --term "myocardial infarction"
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

DEFAULT_URL = "http://localhost:8000"
DEFAULT_TERM = "myocardial infarction"


def get(url: str) -> dict:
    with urllib.request.urlopen(url) as r:
        return json.loads(r.read())


def post(url: str, body: dict) -> dict:
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", default=DEFAULT_URL)
    p.add_argument("--term", default=DEFAULT_TERM)
    p.add_argument(
        "--domain", default=None, help="Comma-separated domain filter, e.g. Condition,Drug"
    )
    p.add_argument("--standard-only", action="store_true")
    p.add_argument("--top-n", type=int, default=5)
    p.add_argument("--use-mlt", type=lambda x: x.lower() != "false", default=True)
    args = p.parse_args()

    # ── Health check ─────────────────────────────────────────────────────
    print("── /health ─────────────────────────────────────────────────────")
    try:
        h = get(f"{args.url}/health")
    except urllib.error.URLError as e:
        sys.exit(f"Service not reachable at {args.url}: {e}")

    for k, v in h.items():
        print(f"  {k}: {v}")

    if not h.get("concept_db_available"):
        print("\nWARNING: concept DB not available — concept_name will fall back to match_term.")

    # ── Search ───────────────────────────────────────────────────────────
    print(f"\n── /search  term={repr(args.term)} ──────────────────────────────")
    body = {
        "term": args.term,
        "top_n": args.top_n,
        "standard_only": args.standard_only,
        "use_mlt": args.use_mlt,
    }
    if args.domain:
        body["domain_filter"] = [d.strip() for d in args.domain.split(",")]

    try:
        resp = post(f"{args.url}/search", body)
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code}: {e.read().decode()}")

    print(f"  total_candidates: {resp['total_candidates']}")
    print()
    for i, r in enumerate(resp["results"], 1):
        # Header line
        match_note = f'  matched: "{r["match_term"]}"' \
            if r["match_term"].lower() != r["concept_name"].lower() else ""
        print(
            f"  {r['concept_id']}  {r['concept_name']}  "
            f"[{r['vocabulary_id']}, {r['domain_id']}]  "
            f"score={r['similarity_score']:.3f}"
            f"{match_note}"
        )
        # Ancestry chain: parse breadcrumb, drop the concept itself, indent
        breadcrumb = r.get("breadcrumb", "")
        if breadcrumb:
            parts = breadcrumb.split(" > ")
            if parts and parts[-1] == r["concept_name"]:
                parts = parts[:-1]
            for depth, ancestor in enumerate(reversed(parts)):
                print(f"  {'  ' * (depth + 1)}← {ancestor}")
        print()

    # ── Additional examples ───────────────────────────────────────────────
    extra_terms = ["type 2 diabetes", "hypertension", "aspirin 100mg"]
    if args.term not in extra_terms:
        print("\n── Extra terms ─────────────────────────────────────────────────")
        for term in extra_terms:
            body["term"] = term
            body["top_n"] = 3
            r = post(f"{args.url}/search", body)
            top = r["results"][0] if r["results"] else None
            if top:
                print(
                    f"  {repr(term):<30} → {top['concept_id']}  "
                    f"{top['concept_name'][:50]}  (score={top['similarity_score']:.4f})"
                )
            else:
                print(f"  {repr(term):<30} → (no results)")


if __name__ == "__main__":
    main()
