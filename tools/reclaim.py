#!/usr/bin/env python3
"""Survey a folder and say, per file, whether deleting it would lose anything.

Written for the arr-ingest scratch area, where content ended up that Sonarr and Radarr
may or may not still track. Answers three questions for every media file:

  * does a library database point at it?      (tracked -> move it with the *arr editor)
  * does another name share the same data?    (hardlinked -> deleting frees nothing)
  * does a same-sized copy already exist in the library?  (duplicate -> safe to drop)

Read-only. It never moves, deletes or modifies anything.

    python3 /mnt/fast-pool/gatekeeper/reclaim.py
    python3 .../reclaim.py --root /mnt/nvme-seed/arr-ingest/movies
    python3 .../reclaim.py --csv /tmp/reclaim.csv

Standard library only.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path

DEFAULT_HOME = Path("/mnt/fast-pool/gatekeeper")
DEFAULT_ROOT = Path("/mnt/nvme-seed/arr-ingest")
DEFAULT_LIBRARY = Path("/mnt/Media/library")
# Container path -> host path. The *arr APIs speak container paths; the shell speaks host ones.
DEFAULT_MAPS = ["/nvme=/mnt/nvme-seed", "/library=/mnt/Media/library", "/data=/mnt/nvme-seed/data"]
MEDIA_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".webm"}


def human(size: float) -> str:
    for unit in ("B", "K", "M", "G", "T"):
        if abs(size) < 1024 or unit == "T":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}T"


def get(base: str, key: str, endpoint: str) -> list:
    url = f"{base.rstrip('/')}/api/v3/{endpoint}"
    try:
        request = urllib.request.Request(url, headers={"X-Api-Key": key})
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        print(f"  ! {endpoint}: {exc.code} — is the API key current?", file=sys.stderr)
    except urllib.error.URLError as exc:
        print(f"  ! {endpoint}: {exc.reason}", file=sys.stderr)
    return []


def to_host(path: str, maps: list[tuple[str, str]]) -> Path:
    """Translate an *arr container path to its host path, always absolute.

    Both sides of the later comparison must be absolute, or relative_to silently never
    matches and every tracked file is misreported as an orphan.
    """
    for container, host in maps:
        if path == container or path.startswith(container.rstrip("/") + "/"):
            return (Path(host) / Path(path[len(container):].lstrip("/"))).resolve()
    return Path(path).resolve()


def library_index(library: Path) -> dict[tuple[str, int], Path]:
    """Every library file, keyed by (name, size) — enough to spot an identical copy."""
    index: dict[tuple[str, int], Path] = {}
    if not library.is_dir():
        return index
    for path in library.rglob("*"):
        if path.is_file() and path.suffix.lower() in MEDIA_EXTENSIONS:
            try:
                index[(path.name, path.stat().st_size)] = path.resolve()
            except OSError:
                continue
    return index


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="folder to survey")
    parser.add_argument("--library", type=Path, default=DEFAULT_LIBRARY,
                        help="library root, to spot files that already exist there")
    parser.add_argument("--map", action="append", default=None, metavar="CONTAINER=HOST",
                        help=f"path translation, repeatable (default: {' '.join(DEFAULT_MAPS)})")
    parser.add_argument("--csv", type=Path, help="also write a row per file here")
    parser.add_argument("--paths", action="store_true",
                        help="show how each library path translates to a host path, and stop")
    parser.add_argument("--min-size", type=int, default=50_000_000,
                        help="ignore files smaller than this (default 50MB)")
    args = parser.parse_args()

    maps = [tuple(m.split("=", 1)) for m in (args.map or DEFAULT_MAPS) if "=" in m]
    args.root = args.root.resolve()
    args.library = args.library.resolve()

    if not args.root.is_dir():
        sys.exit(f"{args.root} is not a directory")

    try:
        secrets = json.loads((args.home / "secrets.json").read_text())
    except Exception as exc:
        sys.exit(f"cannot read secrets.json: {exc}")

    print(f"Surveying {args.root}\n")

    print("Reading the library databases…")
    series = get(secrets["SONARR_URL"], secrets["SONARR_API_KEY"], "series")
    movies = get(secrets["RADARR_URL"], secrets["RADARR_API_KEY"], "movie")
    tracked: list[tuple[Path, str]] = []
    for item in series:
        if item.get("path"):
            tracked.append((to_host(item["path"], maps), f"Sonarr: {item.get('title', '?')}"))
    for item in movies:
        if item.get("path"):
            tracked.append((to_host(item["path"], maps), f"Radarr: {item.get('title', '?')}"))
    print(f"  {len(series)} series, {len(movies)} movies known\n")

    if args.paths:
        print("Container path -> host path, and whether that host path exists.\n"
              "A column of 'MISSING' means the --map translation is wrong, and every file\n"
              "under those folders would be misreported as an orphan.\n")
        seen = set()
        for base, label in sorted(tracked, key=lambda item: str(item[0])):
            if base in seen:
                continue
            seen.add(base)
            mark = "ok     " if base.is_dir() else "MISSING"
            print(f"  [{mark}] {label}\n             {base}")
        raw = sorted({item.get("path", "") for item in series + movies if item.get("path")})
        print(f"\nDistinct prefixes the databases actually use:")
        prefixes = sorted({"/".join(path.split("/")[:3]) for path in raw})
        for prefix in prefixes:
            print(f"  {prefix}")
        print("\nIf a prefix above has no --map entry, add one:  "
              "--map /that/prefix=/mnt/real/host/path")
        return

    print(f"Indexing {args.library} …")
    library = library_index(args.library)
    print(f"  {len(library)} media files in the library\n")

    def owner(path: Path) -> str | None:
        best, best_len = None, -1
        for base, label in tracked:
            try:
                path.relative_to(base)
            except ValueError:
                continue
            if len(str(base)) > best_len:
                best, best_len = label, len(str(base))
        return best

    rows, buckets = [], defaultdict(lambda: [0, 0])  # verdict -> [count, bytes]
    for path in sorted(args.root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size < args.min_size:
            continue

        who = owner(path)
        duplicate = library.get((path.name, stat.st_size))
        shared = stat.st_nlink > 1

        if duplicate:
            verdict = "DUPLICATE — an identical copy is already in the library"
        elif who:
            verdict = f"TRACKED — {who}"
        else:
            verdict = "ORPHAN — no library database points at this"
        if shared:
            verdict += " [hardlinked: deleting this name frees nothing]"

        buckets[verdict.split(" —")[0].split(" [")[0]][0] += 1
        buckets[verdict.split(" —")[0].split(" [")[0]][1] += stat.st_size
        rows.append({"path": str(path), "size_bytes": stat.st_size, "size": human(stat.st_size),
                     "links": stat.st_nlink, "tracked_by": who or "",
                     "duplicate_of": str(duplicate) if duplicate else "", "verdict": verdict})

    if not rows:
        print("No media files over the size threshold found.")
        return

    by_verdict: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_verdict[row["verdict"].split(" —")[0].split(" [")[0]].append(row)

    for kind in ("DUPLICATE", "ORPHAN", "TRACKED"):
        group = by_verdict.get(kind)
        if not group:
            continue
        count, total = buckets[kind]
        print(f"\n{'=' * 78}\n{kind} — {count} file(s), {human(total)}\n{'=' * 78}")
        for row in sorted(group, key=lambda r: -r["size_bytes"])[:40]:
            flag = "  [hardlinked]" if row["links"] > 1 else ""
            print(f"  {row['size']:>8}  {row['path']}{flag}")
            if row["duplicate_of"]:
                print(f"            already at: {row['duplicate_of']}")
            elif row["tracked_by"]:
                print(f"            {row['tracked_by']}")
        if len(group) > 40:
            print(f"  … and {len(group) - 40} more (use --csv for the full list)")

    missing = [base for base, _ in tracked if not base.is_dir()]
    if (series or movies) and not by_verdict.get("TRACKED"):
        print(f"\n{'!' * 78}\n"
              "NOTHING was matched to a library database, but the databases are not empty.\n"
              "That normally means the container-to-host path translation is wrong, in which\n"
              "case tracked files are being reported as ORPHAN. Do not delete anything on the\n"
              "strength of this run — check the translation first:\n\n"
              "    python3 reclaim.py --paths\n"
              f"{'!' * 78}")
    elif missing:
        print(f"\n  note: {len(missing)} library path(s) do not exist on this host — "
              "run with --paths to see which, since files under them read as orphans")

    print(f"\n{'=' * 78}\nTotals")
    for kind, (count, total) in sorted(buckets.items(), key=lambda kv: -kv[1][1]):
        print(f"  {kind:<10} {count:>5} file(s)  {human(total):>9}")
    print("""
What to do with each:
  DUPLICATE  the library already has this exact file — deleting the copy here is safe
  TRACKED    a library database points at it. Move it with Sonarr's Mass Editor or
             Radarr's Movie Editor (change Root Folder, say yes to moving files) so the
             database follows. Do not delete it from the shell.
  ORPHAN     nothing references it. Check it in qBittorrent before deciding — it may
             still be seeding.
  hardlinked another name shares the same data, so removing this one frees no space.
             Usually means a torrent still holds it.""")

    if args.csv:
        with args.csv.open("w", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nFull list: {args.csv}")


if __name__ == "__main__":
    main()
