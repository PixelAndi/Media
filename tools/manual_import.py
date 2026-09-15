#!/usr/bin/env python3
"""Drive Sonarr's and Radarr's manual import from the command line.

The Manual Import button moves around between releases; the API does not. This asks
the *arr instance what it would match in a folder, shows you, and only acts when told.

    # look, change nothing:
    python3 manual_import.py --app sonarr --folder "/nvme/arr-ingest/movies/Mad God .../TV"

    # act on what it matched, moving the files into the library:
    python3 manual_import.py --app sonarr --folder "..." --import --limit 5

Files the instance cannot match, or rejects, are never sent. Start with a small
--limit, confirm those landed, then raise it.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_HOME = Path("/mnt/fast-pool/gatekeeper")


def call(base: str, key: str, endpoint: str, method: str = "GET", payload: dict | None = None):
    url = f"{base.rstrip('/')}/api/v3/{endpoint}"
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"X-Api-Key": key}
    if data:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            body = response.read()
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        sys.exit(f"{method} {url}\n  {exc.code}: {detail}")
    except urllib.error.URLError as exc:
        sys.exit(f"cannot reach {url}: {exc.reason}")


def human(size: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(size) < 1024 or unit == "T":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


def describe(item: dict, app: str) -> tuple[str, bool]:
    """Return a one-line summary and whether this file is safe to import."""
    name = Path(item.get("path", "?")).name
    size = human(item.get("size", 0))
    rejections = [r.get("reason", "?") for r in item.get("rejections", [])]

    if app == "sonarr":
        target = (item.get("series") or {}).get("title")
        episodes = item.get("episodes") or []
        detail = f"{target} — {len(episodes)} episode(s)" if target else "NO SERIES MATCH"
        ready = bool(target and episodes)
    else:
        target = (item.get("movie") or {}).get("title")
        detail = target or "NO MOVIE MATCH"
        ready = bool(target)

    if rejections:
        ready = False
        detail += f"  [rejected: {'; '.join(rejections)[:120]}]"
    return f"  {size:>8}  {name[:70]}\n            {detail}", ready


def payload_for(item: dict, app: str) -> dict:
    entry = {
        "path": item["path"],
        "quality": item.get("quality"),
        "languages": item.get("languages"),
        "releaseGroup": item.get("releaseGroup"),
    }
    if app == "sonarr":
        entry["seriesId"] = item["series"]["id"]
        entry["episodeIds"] = [e["id"] for e in item.get("episodes", [])]
        if item.get("seasonNumber") is not None:
            entry["seasonNumber"] = item["seasonNumber"]
    else:
        entry["movieId"] = item["movie"]["id"]
    return {k: v for k, v in entry.items() if v is not None}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--app", choices=("sonarr", "radarr"), required=True)
    parser.add_argument("--folder", required=True,
                        help="folder AS THE APP SEES IT, e.g. /nvme/arr-ingest/...")
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--import", dest="do_import", action="store_true",
                        help="actually import; without this it only reports")
    parser.add_argument("--mode", default="move", choices=("move", "copy"),
                        help="move frees the source pool; copy leaves it (default: move)")
    parser.add_argument("--limit", type=int, help="import at most this many files this run")
    parser.add_argument("--find", metavar="TEXT",
                        help="list series/movies whose title contains TEXT, with their ids, and stop")
    parser.add_argument("--id", type=int, metavar="N",
                        help="force everything in the folder to this series/movie id, for releases "
                             "whose filenames carry no recognisable title")
    args = parser.parse_args()

    try:
        secrets = json.loads((args.home / "secrets.json").read_text())
    except Exception as exc:
        sys.exit(f"cannot read secrets.json: {exc}")

    prefix = "SONARR" if args.app == "sonarr" else "RADARR"
    base, key = secrets[f"{prefix}_URL"], secrets[f"{prefix}_API_KEY"]

    if args.find:
        endpoint = "series" if args.app == "sonarr" else "movie"
        needle = args.find.lower()
        hits = [item for item in call(base, key, endpoint)
                if needle in str(item.get("title", "")).lower()]
        if not hits:
            sys.exit(f"No {endpoint} title contains {args.find!r}.")
        for item in sorted(hits, key=lambda i: i["title"]):
            print(f"  id {item['id']:<5} {item['title']}")
            print(f"            {item.get('path', '')}")
        print(f"\nPass one of those with --id to force the match.")
        return

    params = {"folder": args.folder, "filterExistingFiles": "false"}
    if args.id is not None:
        # Sonarr and Radarr will match episodes or the movie within this title instead of
        # parsing the filename, which is what rescues date-named and bare-titled releases.
        params["seriesId" if args.app == "sonarr" else "movieId"] = str(args.id)
    query = urllib.parse.urlencode(params)
    print(f"Asking {args.app} what it finds in {args.folder} …\n")
    items = call(base, key, f"manualimport?{query}")
    if not isinstance(items, list) or not items:
        sys.exit("Nothing came back. Check the folder path as the CONTAINER sees it "
                 "(/nvme/... not /mnt/nvme-seed/...), and that the app has that mount.")

    ready, skipped = [], []
    for item in items:
        line, ok = describe(item, args.app)
        (ready if ok else skipped).append((line, item))

    if ready:
        print(f"MATCHED — {len(ready)} file(s)\n" + "\n".join(line for line, _ in ready))
    if skipped:
        print(f"\nNOT IMPORTABLE — {len(skipped)} file(s)\n"
              + "\n".join(line for line, _ in skipped))
        print("\n  These stay where they are. Unmatched usually means a release name the app\n"
              "  cannot parse; rejected usually means it already has that file.")

    if skipped and args.id is None and args.app == "sonarr":
        print("\n  If a whole show is unmatched because its filenames carry no title, find its\n"
              "  id with --find \"part of the name\" and re-run with --id N to force the match.")

    if not args.do_import:
        print(f"\nNothing was changed. Re-run with --import --limit 25 to move the matched files.")
        return
    if not ready:
        sys.exit("\nNothing importable — not sending anything.")

    batch = [item for _, item in ready][: args.limit] if args.limit else [i for _, i in ready]
    files = [payload_for(item, args.app) for item in batch]
    print(f"\nImporting {len(files)} file(s) with importMode={args.mode} …")
    result = call(base, key, "command", "POST",
                  {"name": "ManualImport", "files": files, "importMode": args.mode})
    print(f"  command {result.get('id')} queued ({result.get('status', '?')})")
    print("\n  Watch it in the app's Activity → History, and watch space come back with:\n"
          "    zfs list -o name,used,avail nvme-seed Media")


if __name__ == "__main__":
    main()
