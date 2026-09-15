#!/usr/bin/env python3
"""Print the gatekeeper's job list in a readable form.

Reads the webhook secret from secrets.json, so no key is typed on the command line
and none ends up in shell history. Run it on the TrueNAS host:

    python3 /mnt/fast-pool/gatekeeper/tools/jobs.py
    python3 .../jobs.py --state failed     # only the failures
    python3 .../jobs.py --full             # every field of every job

Only the standard library, because the host python has no third-party packages.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_HOME = Path("/mnt/fast-pool/gatekeeper")


def load_secret(home: Path) -> str:
    path = home / "secrets.json"
    try:
        secret = json.loads(path.read_text()).get("WEBHOOK_SECRET")
    except FileNotFoundError:
        sys.exit(f"{path} not found. Pass --home if the gatekeeper lives elsewhere.")
    except PermissionError:
        sys.exit(f"Cannot read {path} — run this as root (sudo -i).")
    except json.JSONDecodeError as exc:
        sys.exit(f"{path} is not valid JSON: {exc}")
    if not secret:
        sys.exit(f"{path} has no WEBHOOK_SECRET.")
    return str(secret)


def fetch(url: str, secret: str) -> list[dict]:
    request = urllib.request.Request(url, headers={"X-Api-Key": secret})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:200]
        if exc.code == 401:
            sys.exit("401 — the secret in secrets.json is not what the running service expects.\n"
                     "Restart the gatekeeper if you edited secrets.json after it started.")
        sys.exit(f"{exc.code} from {url}: {body}")
    except urllib.error.URLError as exc:
        sys.exit(f"Cannot reach {url}: {exc.reason}\nIs the gatekeeper running? docker ps | grep gatekeeper")
    if not isinstance(payload, list):
        sys.exit(f"Expected a list of jobs, got: {json.dumps(payload)[:200]}")
    return payload


def describe(job: dict) -> str:
    name = job.get("download_name") or Path(job.get("source_path", "?")).name
    lines = [f"[{job.get('state', '?')}] {name}"]
    lines.append(f"    source      {job.get('source_path')}")
    if job.get("transcode_dir"):
        lines.append(f"    transcode   {job['transcode_dir']}")
    if job.get("imported_path"):
        lines.append(f"    imported    {job['imported_path']}")
    subs = job.get("translation_state", "?")
    if job.get("untranslated_lines"):
        subs += f" ({job['untranslated_lines']} lines left in English)"
    lines.append(f"    subtitles   {subs}")
    lines.append(f"    attempts    pipeline {job.get('pipeline_attempts', 0)}, "
                 f"translation {job.get('translation_attempts', 0)}")
    if job.get("error_detail"):
        lines.append(f"    ERROR       {job['error_detail']}")
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME, help="folder holding secrets.json")
    parser.add_argument("--url", default="http://127.0.0.1:5000", help="gatekeeper base URL")
    parser.add_argument("--state", help="show only jobs in this state (new, transcoding, failed, done, ...)")
    parser.add_argument("--full", action="store_true", help="dump every field as JSON")
    args = parser.parse_args()

    jobs = fetch(f"{args.url.rstrip('/')}/api/jobs?limit=500", load_secret(args.home))
    if args.state:
        jobs = [j for j in jobs if j.get("state") == args.state]

    if not jobs:
        print("No jobs." if not args.state else f"No jobs in state {args.state!r}.")
        return

    if args.full:
        print(json.dumps(jobs, indent=2))
        return

    for job in jobs:
        print(describe(job))
        print()

    counts: dict[str, int] = {}
    for job in jobs:
        counts[job.get("state", "?")] = counts.get(job.get("state", "?"), 0) + 1
    print("  ".join(f"{state}: {n}" for state, n in sorted(counts.items())))


if __name__ == "__main__":
    main()
