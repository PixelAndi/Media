#!/usr/bin/env python3
"""Check every moving part of the pipeline at once and say what is wrong.

Run on the TrueNAS host as root:

    python3 /mnt/fast-pool/gatekeeper/doctor.py

Each check prints ok / WARN / FAIL, and every failure carries the fix. Nothing is
modified — it reads state, makes one tiny Gemini call, and writes a single temporary
file for the hardlink test.

Standard library only; the host python has no third-party packages.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_HOME = Path("/mnt/fast-pool/gatekeeper")
OK, WARN, FAIL = "ok  ", "WARN", "FAIL"

results: list[tuple[str, str, str, str]] = []


def record(status: str, name: str, detail: str = "", fix: str = "") -> None:
    results.append((status, name, detail, fix))
    line = f"[{status}] {name}"
    if detail:
        line += f" — {detail}"
    print(line, flush=True)
    if status != OK and fix:
        print(f"         fix: {fix}", flush=True)


def run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, (proc.stdout + proc.stderr).strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"


def http(method: str, url: str, headers: dict | None = None, body: bytes | None = None,
         timeout: int = 20) -> tuple[int, str]:
    request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode(errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode(errors="replace")
    except urllib.error.URLError as exc:
        return 0, str(exc.reason)
    except Exception as exc:  # a malformed URL, a refused TLS handshake
        return 0, str(exc)


# --------------------------------------------------------------------------- containers


def containers() -> dict[str, str]:
    code, out = run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"])
    if code != 0:
        record(FAIL, "docker", out[:120], "run this as root: sudo -i")
        return {}
    found = dict(line.split("\t", 1) for line in out.splitlines() if "\t" in line)
    record(OK, "docker", f"{len(found)} containers running")
    return found


def find_container(names: dict[str, str], *needles: str) -> str | None:
    for needle in needles:
        for name in names:
            if name == needle:
                return name
    for needle in needles:
        for name in names:
            if needle in name:
                return name
    return None


# --------------------------------------------------------------------------- checks


def check_settings(home: Path) -> tuple[dict, dict]:
    secrets, config = {}, {}
    try:
        secrets = json.loads((home / "secrets.json").read_text())
    except Exception as exc:
        record(FAIL, "secrets.json", str(exc)[:120], f"check {home}/secrets.json exists and is valid JSON")
        return {}, {}
    required = ["SONARR_URL", "SONARR_API_KEY", "RADARR_URL", "RADARR_API_KEY",
                "GEMINI_API_KEY", "WEBHOOK_SECRET"]
    missing = [k for k in required if not secrets.get(k)]
    if missing:
        record(FAIL, "secrets.json", f"missing {', '.join(missing)}", "add the missing keys")
    else:
        record(OK, "secrets.json", f"{len(required)} keys present")

    try:
        config = json.loads((home / "config.json").read_text())
        record(OK, "config.json", f"model={config.get('translation', {}).get('model', '?')}")
    except Exception as exc:
        record(FAIL, "config.json", str(exc)[:120], "fix the JSON syntax (a stray comma?)")
    return secrets, config


def check_gatekeeper(secret: str, url: str) -> list[dict]:
    status, body = http("GET", f"{url}/health")
    if status != 200:
        record(FAIL, "gatekeeper /health", body[:120] or f"status {status}",
               "docker logs --tail 50 gatekeeper")
        return []
    record(OK, "gatekeeper /health", body.strip()[:120])

    status, body = http("GET", f"{url}/api/jobs?limit=500", {"X-Api-Key": secret})
    if status == 401:
        record(FAIL, "gatekeeper auth", "401",
               "WEBHOOK_SECRET in secrets.json differs from the running service; restart the app")
        return []
    if status != 200:
        record(FAIL, "gatekeeper /api/jobs", f"{status} {body[:100]}")
        return []
    jobs = json.loads(body)
    record(OK, "gatekeeper auth", f"{len(jobs)} job(s) in the database")
    return jobs


def check_arr(kind: str, base: str, key: str) -> None:
    status, body = http("GET", f"{base.rstrip('/')}/api/v3/system/status", {"X-Api-Key": key})
    if status == 401:
        record(FAIL, kind, "401 — wrong API key", f"copy the current key from {kind} → Settings → General")
    elif status != 200:
        record(FAIL, kind, f"{status} {body[:100]}", f"is {base} correct and the app running?")
    else:
        try:
            version = json.loads(body).get("version", "?")
        except ValueError:
            version = "?"
        record(OK, kind, f"reachable, version {version}")


def check_qbit(config: dict, secrets: dict) -> None:
    base = str(config.get("qbittorrent", {}).get("url", "")).rstrip("/")
    if not base:
        record(WARN, "qBittorrent", "no url in config.json", "the reconcile safety net is disabled without it")
        return
    user = secrets.get("QBIT_USERNAME", "")
    password = secrets.get("QBIT_PASSWORD", "")
    headers, cookie = {"Content-Type": "application/x-www-form-urlencoded"}, ""
    if user:
        status, body = http("POST", f"{base}/api/v2/auth/login", headers,
                            f"username={user}&password={password}".encode())
        if body.strip() == "Fails.":
            record(FAIL, "qBittorrent login", "rejected", "check QBIT_USERNAME / QBIT_PASSWORD")
            return
        if status == 403:
            record(FAIL, "qBittorrent login", "403 — failed-login ban", "restart qBittorrent to clear it")
            return
    status, body = http("GET", f"{base}/api/v2/torrents/info?filter=completed",
                        {"Cookie": cookie} if cookie else {})
    if status != 200:
        record(FAIL, "qBittorrent", f"{status} {body[:100]}", f"is {base} reachable?")
        return
    try:
        torrents = json.loads(body)
    except ValueError:
        record(FAIL, "qBittorrent", "unreadable torrent list")
        return
    known = set(config.get("download_categories", {}))
    watched = [t for t in torrents if t.get("category") in known]
    record(OK, "qBittorrent", f"{len(torrents)} completed, {len(watched)} in watched categories")


def check_gemini(secrets: dict, config: dict) -> None:
    model = config.get("translation", {}).get("model", "gemini-flash-latest")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    payload = json.dumps({"contents": [{"role": "user", "parts": [{"text": "Reply with: ok"}]}]}).encode()
    status, body = http("POST", url,
                        {"x-goog-api-key": secrets.get("GEMINI_API_KEY", ""),
                         "Content-Type": "application/json"},
                        payload, timeout=45)
    if status == 200:
        record(OK, "Gemini", f"model {model} answered")
        return
    try:
        reason = json.loads(body).get("error", {}).get("message", "")[:160]
    except ValueError:
        reason = body[:160]
    if status >= 500:
        # Google's own capacity, not anything on this server.
        record(WARN, "Gemini", f"{status}: {reason}",
               "transient overload at Google's end — wait and re-run this check; "
               "the pipeline retries on its own")
        return
    if status == 429 or "quota" in reason.lower() or "credits" in reason.lower():
        fix = "out of quota or credits — top up at https://ai.studio/projects"
    elif "api key" in reason.lower() or status in (401, 403):
        fix = "GEMINI_API_KEY in secrets.json is not accepted — regenerate it at aistudio.google.com"
    else:
        fix = ("list the models this key can use with: curl -s "
               "'https://generativelanguage.googleapis.com/v1beta/models?key=YOUR_KEY' | grep '\"name\"' "
               "— then set translation.model in config.json")
    record(FAIL, "Gemini", f"{status}: {reason}", fix)


def check_paths(gatekeeper: str, config: dict) -> None:
    wanted = [config.get(k, d) for k, d in (
        ("complete_root", "/data/torrents/complete"), ("work_root", "/data/work"),
        ("transcode_root", "/data/transcode"))]
    wanted.append("/data/cache")
    for path in wanted:
        code, _ = run(["docker", "exec", gatekeeper, "test", "-d", path])
        if code == 0:
            record(OK, f"path {path}")
        else:
            record(FAIL, f"path {path}", "missing inside the container",
                   "check the /data mount: /mnt/nvme-seed/data -> /data")

    for tool in ("ffmpeg", "ffprobe"):
        code, _ = run(["docker", "exec", gatekeeper, "which", tool])
        record(OK if code == 0 else FAIL, f"{tool} in gatekeeper", "",
               "the container installs ffmpeg at startup; check docker logs gatekeeper")

    complete = config.get("complete_root", "/data/torrents/complete")
    work = config.get("work_root", "/data/work")
    probe = f"{complete}/.doctor-hardlink-probe"
    run(["docker", "exec", gatekeeper, "sh", "-c", f"rm -f '{probe}' '{work}/.doctor-hardlink-probe'"])
    code, out = run(["docker", "exec", gatekeeper, "sh", "-c",
                     f"touch '{probe}' && ln '{probe}' '{work}/.doctor-hardlink-probe' "
                     f"&& stat -c %h '{work}/.doctor-hardlink-probe'"])
    run(["docker", "exec", gatekeeper, "sh", "-c", f"rm -f '{probe}' '{work}/.doctor-hardlink-probe'"])
    if code == 0 and out.strip().endswith("2"):
        record(OK, "hardlinks", "complete and work share one filesystem")
    else:
        record(FAIL, "hardlinks", out[:120] or "could not link",
               "complete_root and work_root must be in the SAME ZFS dataset")


def check_tdarr(names: dict[str, str], transcode_root: str) -> None:
    tdarr = find_container(names, "tdarr")
    if not tdarr:
        record(FAIL, "Tdarr", "no running container", "start the Tdarr app")
        return
    record(OK, "Tdarr container", f"{tdarr} — {names[tdarr]}")

    code, out = run(["docker", "inspect", tdarr, "--format",
                     "{{range .Mounts}}{{.Source}} -> {{.Destination}}\n{{end}}"])
    if code != 0 or " -> /data" not in out:
        record(FAIL, "Tdarr /data mount", "not mounted",
               "Apps → Tdarr → Edit → Storage: add /mnt/nvme-seed/data → /data")
        return
    record(OK, "Tdarr /data mount", "present")

    code, out = run(["docker", "exec", tdarr, "ls", transcode_root])
    if code != 0:
        record(FAIL, f"Tdarr can read {transcode_root}", out[:120],
               "the mount exists but the path does not; check transcode_root in config.json")
        return
    waiting = [line for line in out.splitlines() if line.strip()]
    record(OK, f"Tdarr can read {transcode_root}", f"{len(waiting)} job folder(s) queued")


def check_stale_handoffs(gatekeeper: str, transcode_root: str, hours: float) -> None:
    code, out = run(["docker", "exec", gatekeeper, "sh", "-c",
                     f"find '{transcode_root}' -type f -mmin +{int(hours * 60)} 2>/dev/null | head -20"])
    stale = [line for line in out.splitlines() if line.strip()] if code == 0 else []
    if stale:
        record(WARN, "Tdarr throughput",
               f"{len(stale)} file(s) untouched for over {hours:g}h",
               "Tdarr sees the queue but is not consuming it. In Tdarr → Libraries → Source: "
               "Source folder must be the transcode root, 'Hold Files After Scanning' OFF, "
               "Folder Watch ON, 'Skip hardlinked files' OFF")
    else:
        record(OK, "Tdarr throughput", "nothing sitting stale in the queue")


SUFFIXES = {"K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15}


def as_bytes(value: str) -> float | None:
    """Parse a zfs size like '895G' or '1.60T'."""
    value = value.strip()
    if not value or value == "-":
        return None
    try:
        return float(value[:-1]) * SUFFIXES[value[-1].upper()] if value[-1].upper() in SUFFIXES \
            else float(value)
    except ValueError:
        return None


def check_space() -> None:
    code, out = run(["zfs", "list", "-o", "name,used,avail", "-H"])
    if code != 0:
        return
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3 or "/" in parts[0]:
            continue
        name, used, avail = parts[0], parts[1], parts[2]
        used_b, avail_b = as_bytes(used), as_bytes(avail)
        detail = f"{used} used, {avail} free"
        # Judge by proportion, not an absolute figure: a 30G boot pool that is 88% empty is
        # healthy, and a 900G work pool with 65G left is not.
        if used_b is None or avail_b is None or used_b + avail_b == 0:
            record(OK, f"pool {name}", detail)
            continue
        free_pct = avail_b / (used_b + avail_b) * 100
        detail += f" ({free_pct:.0f}% free)"
        record(WARN if free_pct < 20 else OK, f"pool {name}", detail,
               "ZFS slows badly past about 80% full" if free_pct < 20 else "")


def summarise_jobs(jobs: list[dict]) -> None:
    if not jobs:
        return
    print()
    states: dict[str, int] = {}
    for job in jobs:
        states[job.get("state", "?")] = states.get(job.get("state", "?"), 0) + 1
    print("Jobs: " + "  ".join(f"{s}: {n}" for s, n in sorted(states.items())))
    failed = [j for j in jobs if j.get("state") == "failed"]
    for job in failed[:10]:
        name = job.get("download_name") or Path(job.get("source_path", "?")).name
        print(f"  failed: {name[:70]}")
        print(f"          {job.get('error_detail')}")
    if failed:
        print("\n  Requeue them once the causes above are fixed:")
        print("    python3 /mnt/fast-pool/gatekeeper/jobs.py --retry-failed")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    parser.add_argument("--url", default="http://127.0.0.1:5000")
    parser.add_argument("--stale-hours", type=float, default=1.0,
                        help="how long a file may sit in the transcode queue before it is suspicious")
    args = parser.parse_args()

    print(f"Pipeline check — {time.strftime('%Y-%m-%d %H:%M:%S %Z')}\n")

    names = containers()
    secrets, config = check_settings(args.home)
    if not secrets:
        sys.exit(1)

    gatekeeper = find_container(names, "gatekeeper")
    if gatekeeper:
        record(OK, "gatekeeper container", names[gatekeeper])
    elif names:
        record(FAIL, "gatekeeper container", "not running", "Apps → gatekeeper → Start")
    # Ask the service directly either way — it answers even when docker ps could not be read.
    url = args.url.rstrip("/")
    jobs = check_gatekeeper(secrets["WEBHOOK_SECRET"], url)

    check_arr("Sonarr", secrets["SONARR_URL"], secrets["SONARR_API_KEY"])
    check_arr("Radarr", secrets["RADARR_URL"], secrets["RADARR_API_KEY"])
    check_qbit(config, secrets)
    check_gemini(secrets, config)

    transcode_root = config.get("transcode_root", "/data/transcode")
    if gatekeeper:
        check_paths(gatekeeper, config)
        check_stale_handoffs(gatekeeper, transcode_root, args.stale_hours)
    check_tdarr(names, transcode_root)
    check_space()

    summarise_jobs(jobs)

    bad = [r for r in results if r[0] == FAIL]
    warn = [r for r in results if r[0] == WARN]
    print(f"\n{len(results) - len(bad) - len(warn)} ok, {len(warn)} warning(s), {len(bad)} failure(s)")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
