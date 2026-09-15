#!/usr/bin/env python3
"""Gatekeeper between qBittorrent and the *arr import.

Nothing reaches the library pool until it is final. Per torrent:

  qBittorrent finishes -> hardlink media into /data/work/<job>/   (seed untouched)
                       -> extract the English subtitle track
                       -> hand the video to Tdarr via /data/transcode/<job>/<n>/
                       -> translate EN -> SV while Tdarr encodes
                       -> verify Tdarr's output (AV1, no subtitles, duration matches)
                       -> move it back beside its .sv sidecar
                       -> DownloadedEpisodesScan / DownloadedMoviesScan with importMode=Move

That *arr import is the single write to the library pool.

Tdarr cannot see a file until its subtitles have been extracted, so the embedded
English track can never be stripped out from under us.

A reconcile loop polls qBittorrent for completed torrents the database does not
know about, so a missed webhook or a restart costs nothing.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pysubs2
import requests
import uvicorn
from fastapi import Body, Depends, FastAPI, File, Header, HTTPException, Response, UploadFile

log = logging.getLogger("gatekeeper")

BASE_DIR = Path(os.environ.get("AI_TRANSLATOR_HOME", "/opt/ai-translator"))
SECRETS_FILE = Path(os.environ.get("AI_TRANSLATOR_SECRETS", BASE_DIR / "secrets.json"))
CONFIG_FILE = Path(os.environ.get("AI_TRANSLATOR_CONFIG", BASE_DIR / "config.json"))

DEFAULTS: dict[str, Any] = {
    "db_file": str(BASE_DIR / "state.db"),
    "data_root": "/data",
    "complete_root": "/data/torrents/complete",
    "work_root": "/data/work",
    "transcode_root": "/data/transcode",
    "download_categories": {"tv-sonarr": "series", "radarr": "movie"},
    "min_media_bytes": 50_000_000,
    "sample_markers": ["sample", "trailer"],
    "listen_host": "0.0.0.0",
    "listen_port": 5000,
    "poll_interval_seconds": 10,
    "stability_checks": 2,
    "transcode_timeout_hours": 12,
    "import_timeout_minutes": 60,
    "command_timeout_minutes": 30,
    "import_without_subtitles": True,
    "arr_request_timeout": 30,
    "ffmpeg_timeout": 900,
    "max_pipeline_attempts": 5,
    "qbittorrent": {
        "url": "",
        "reconcile_interval_seconds": 300,
        "reconcile_max_age_hours": 24,
    },
    "translation": {
        "model": "gemini-2.5-flash",
        "batch_size": 50,
        "context_lines": 30,
        "request_timeout": 120,
        "delay_between_batches": 6.5,
        "max_batch_attempts": 3,
        "max_attempts": 3,
        "max_untranslated_ratio": 0.1,
    },
}

MEDIA_EXTENSIONS = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".ts", ".webm"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}
TEXT_SUBTITLE_CODECS = {"ass": ".ass", "ssa": ".ass", "subrip": ".srt", "text": ".srt", "mov_text": ".srt"}
IMAGE_SUBTITLE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
ENGLISH_TAGS = {"eng", "en", "en-us", "en-gb", "english"}

# Tdarr's work-in-progress files inside the folder it is watching.
TDARR_TEMP_MARKERS = ("tdarrcachefile", "-tdarr-", ".partial", ".tmp", ".workdir")

STATE_NEW = "new"
STATE_TRANSCODING = "transcoding"
STATE_TRANSCODED = "transcoded"
STATE_IMPORTING = "importing"
STATE_DONE = "done"
STATE_FAILED = "failed"
PER_FILE_STATES = (STATE_NEW, STATE_TRANSCODING)

TR_PENDING, TR_PROCESSING, TR_DONE, TR_FAILED = "pending", "processing", "done", "failed"

shutdown = threading.Event()
# One translation at a time, whether it came from the pipeline or from Bazarr.
translation_lock = threading.Lock()


class PipelineError(Exception):
    """Recoverable failure: the job is retried on a later tick."""


class FatalJobError(Exception):
    """Unrecoverable failure: the job is marked failed immediately."""


# --------------------------------------------------------------------------- config


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_settings() -> tuple[dict[str, Any], dict[str, Any]]:
    secrets = json.loads(SECRETS_FILE.read_text())
    required = ["SONARR_URL", "SONARR_API_KEY", "RADARR_URL", "RADARR_API_KEY", "GEMINI_API_KEY", "WEBHOOK_SECRET"]
    missing = [key for key in required if not secrets.get(key)]
    if missing:
        raise SystemExit(f"secrets.json is missing required keys: {', '.join(missing)}")
    config = _deep_merge(DEFAULTS, json.loads(CONFIG_FILE.read_text()) if CONFIG_FILE.exists() else {})
    return config, secrets


CONFIG, SECRETS = load_settings()
DB_FILE = CONFIG["db_file"]
DATA_ROOT = Path(CONFIG["data_root"])
COMPLETE_ROOT = Path(CONFIG["complete_root"])
WORK_ROOT = Path(CONFIG["work_root"])
TRANSCODE_ROOT = Path(CONFIG["transcode_root"])
# Temporary subtitles live outside work/ so they can never be swept up by an import.
SCRATCH_ROOT = DATA_ROOT / ".scratch"
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{CONFIG['translation']['model']}:generateContent"
)


# --------------------------------------------------------------------------- database

COLUMNS: dict[str, str] = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "download_id": "TEXT NOT NULL",
    "download_name": "TEXT",
    "category": "TEXT",
    "media_type": "TEXT NOT NULL CHECK(media_type IN ('series','movie'))",
    "source_path": "TEXT NOT NULL UNIQUE",
    "work_dir": "TEXT",
    "work_path": "TEXT",
    "transcode_dir": "TEXT",
    "state": "TEXT NOT NULL DEFAULT 'new'",
    "translation_state": "TEXT NOT NULL DEFAULT 'pending'",
    "translation_attempts": "INTEGER NOT NULL DEFAULT 0",
    "pipeline_attempts": "INTEGER NOT NULL DEFAULT 0",
    "subtitle_ext": "TEXT",
    "en_subtitle_path": "TEXT",
    "sv_subtitle_path": "TEXT",
    "untranslated_lines": "INTEGER NOT NULL DEFAULT 0",
    "source_duration": "REAL",
    "last_size": "INTEGER",
    "stable_checks": "INTEGER NOT NULL DEFAULT 0",
    "staged_at": "REAL",
    "import_command_id": "INTEGER",
    "import_deadline": "REAL",
    "imported_path": "TEXT",
    "error_detail": "TEXT",
    "created_at": "REAL NOT NULL DEFAULT 0",
    "updated_at": "REAL NOT NULL DEFAULT 0",
}
UPDATABLE_COLUMNS = set(COLUMNS) - {"id", "source_path", "created_at"}


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    Path(DB_FILE).parent.mkdir(parents=True, exist_ok=True)
    columns_sql = ", ".join(f"{name} {ddl}" for name, ddl in COLUMNS.items())
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
        if existing and "download_id" not in existing:
            log.warning("found a jobs table from an earlier design, renaming it to jobs_legacy")
            conn.execute("DROP TABLE IF EXISTS jobs_legacy")
            conn.execute("ALTER TABLE jobs RENAME TO jobs_legacy")
            existing = set()
        conn.execute(f"CREATE TABLE IF NOT EXISTS jobs ({columns_sql})")
        for name, ddl in COLUMNS.items():
            if existing and name not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_download ON jobs(download_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_translation ON jobs(translation_state)")


def fetch_jobs(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(query, params).fetchall()


def update_job(job_id: int, **fields: Any) -> None:
    unknown = set(fields) - UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(f"unknown job columns: {sorted(unknown)}")
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{name}=?" for name in fields)
    with db() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*fields.values(), job_id))


def update_download(download_id: str, **fields: Any) -> None:
    unknown = set(fields) - UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(f"unknown job columns: {sorted(unknown)}")
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{name}=?" for name in fields)
    with db() as conn:
        conn.execute(
            f"UPDATE jobs SET {assignments} WHERE download_id=? AND state NOT IN (?,?)",
            (*fields.values(), download_id, STATE_DONE, STATE_FAILED),
        )


def fail_job(job_id: int, detail: str) -> None:
    log.error("job %s failed: %s", job_id, detail)
    update_job(job_id, state=STATE_FAILED, error_detail=detail[:1000])


def fail_download(download_id: str, detail: str) -> None:
    log.error("download %s failed: %s", download_id, detail)
    update_download(download_id, state=STATE_FAILED, error_detail=detail[:1000])


def meta_get(key: str) -> str | None:
    with db() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def meta_set(key: str, value: str) -> None:
    with db() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))


def job_key(download_id: str) -> str:
    return hashlib.sha1(download_id.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- paths


def within_data_root(path: Path) -> bool:
    resolved = Path(os.path.normpath(str(path)))
    return resolved == DATA_ROOT or DATA_ROOT in resolved.parents


def sidecar_path(media: Path, ext: str) -> Path:
    return media.with_name(f"{media.stem}.sv{ext}")


def find_media_files(content: Path) -> list[Path]:
    """Media files worth processing inside a finished torrent."""
    if content.is_file():
        candidates = [content]
    else:
        candidates = sorted(p for p in content.rglob("*") if p.is_file())
    markers = [m.lower() for m in CONFIG["sample_markers"]]
    keep = []
    for path in candidates:
        if path.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        if any(marker in path.name.lower() for marker in markers):
            continue
        if path.stat().st_size < CONFIG["min_media_bytes"]:
            continue
        keep.append(path)
    return keep


# --------------------------------------------------------------------------- ffprobe / ffmpeg


def ffprobe(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        capture_output=True,
        text=True,
        timeout=CONFIG["ffmpeg_timeout"],
    )
    if result.returncode != 0:
        raise PipelineError(f"ffprobe failed for {path}: {result.stderr.strip()[:300]}")
    return json.loads(result.stdout or "{}")


def streams_of_type(info: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return [stream for stream in info.get("streams", []) if stream.get("codec_type") == kind]


def probe_duration(info: dict[str, Any]) -> float:
    try:
        return float(info.get("format", {}).get("duration", 0.0))
    except (TypeError, ValueError):
        return 0.0


def is_av1(info: dict[str, Any]) -> bool:
    return any(stream.get("codec_name") == "av1" for stream in streams_of_type(info, "video"))


def pick_english_subtitle(info: dict[str, Any]) -> dict[str, Any] | None:
    """Prefer full ASS/SSA dialogue tracks; forced tracks are a last resort."""
    candidates = []
    for stream in streams_of_type(info, "subtitle"):
        language = str(stream.get("tags", {}).get("language", "")).lower()
        title = str(stream.get("tags", {}).get("title", "")).lower()
        if language not in ENGLISH_TAGS and not (not language and "english" in title):
            continue
        codec = stream.get("codec_name", "")
        if codec not in TEXT_SUBTITLE_CODECS:
            continue
        forced = bool(stream.get("disposition", {}).get("forced")) or "forced" in title
        codec_rank = {"ass": 0, "ssa": 0, "subrip": 1, "text": 1, "mov_text": 2}[codec]
        candidates.append(((forced, codec_rank, stream.get("index", 0)), stream))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


def strip_subtitles(media: Path) -> None:
    """Remove every embedded subtitle stream in place, without re-encoding.

    Tdarr skips a file that is already AV1, which means it never strips that file's
    subtitles either — so the pipeline would wait out the whole transcode timeout for a
    condition Tdarr was never going to satisfy. A remux does it here instead: no
    re-encode, no quality loss, seconds rather than the hours a second AV1 pass costs.

    *media* is a hardlink to the seeding torrent, so replacing it swaps only this name.
    The torrent's own name in the complete folder keeps pointing at the original file.
    """
    temp = media.with_name(f".{media.stem}.nosubs{media.suffix}")
    result = subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-v", "error", "-i", str(media),
         "-map", "0", "-map", "-0:s", "-c", "copy", str(temp)],
        capture_output=True,
        text=True,
        timeout=CONFIG["ffmpeg_timeout"],
    )
    if result.returncode != 0 or not temp.exists():
        temp.unlink(missing_ok=True)
        raise PipelineError(f"could not remux the subtitles away: {result.stderr.strip()[:300]}")
    os.replace(temp, media)


def extract_subtitle(media: Path, stream: dict[str, Any], destination: Path) -> None:
    codec = stream["codec_name"]
    # mov_text carries no styling, so let ffmpeg convert it; everything else copies verbatim.
    codec_args = ["-c:s", "copy"] if codec != "mov_text" else ["-c:s", "srt"]
    result = subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-v", "error", "-i", str(media), "-map", f"0:{stream['index']}",
         *codec_args, str(destination)],
        capture_output=True,
        text=True,
        timeout=CONFIG["ffmpeg_timeout"],
    )
    if result.returncode != 0 or not destination.exists() or destination.stat().st_size == 0:
        raise FatalJobError(f"subtitle extraction failed: {result.stderr.strip()[:300]}")


# --------------------------------------------------------------------------- translation

TAG_PATTERN = re.compile(r"\{[^{}]*\}|\\[Nnh]")
PLACEHOLDER_PATTERN = re.compile(r"\[\s*\[\s*(\d+)\s*\]\s*\]")

SYSTEM_INSTRUCTION = (
    "You translate English subtitles into natural, idiomatic Swedish for film and television. "
    "Rules: (1) Tokens of the form [[0]], [[1]] are styling, positioning and line-break codes. "
    "Reproduce every token exactly as written, once each, in a position that matches the original. "
    "(2) Never merge, split, add or drop entries. (3) Keep the register and tone of the original, "
    "and keep lines short enough to read on screen. (4) Reply with a JSON object mapping each input "
    "id (as a string) to its Swedish translation, and nothing else."
)


def mask_tags(text: str) -> tuple[str, list[str]]:
    tags: list[str] = []

    def replace(match: re.Match[str]) -> str:
        tags.append(match.group(0))
        return f"[[{len(tags) - 1}]]"

    return TAG_PATTERN.sub(replace, text), tags


def unmask_tags(text: str, tags: list[str]) -> str | None:
    """Restore styling tokens, or return None if the model mangled them."""
    found = [int(index) for index in PLACEHOLDER_PATTERN.findall(text)]
    if sorted(found) != list(range(len(tags))):
        return None
    return PLACEHOLDER_PATTERN.sub(lambda match: tags[int(match.group(1))], text)


def call_gemini(prompt: str) -> dict[str, str]:
    settings = CONFIG["translation"]
    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "response_mime_type": "application/json"},
    }
    response = requests.post(
        GEMINI_URL,
        json=payload,
        headers={"x-goog-api-key": SECRETS["GEMINI_API_KEY"]},
        timeout=settings["request_timeout"],
    )
    response.raise_for_status()
    body = response.json()
    candidates = body.get("candidates") or []
    if not candidates:
        raise PipelineError(f"Gemini returned no candidates: {json.dumps(body.get('promptFeedback', {}))[:200]}")
    parts = candidates[0].get("content", {}).get("parts") or []
    if not parts:
        raise PipelineError(f"Gemini returned no content (finishReason={candidates[0].get('finishReason')})")
    decoded = json.loads(parts[0]["text"])
    if not isinstance(decoded, dict):
        raise PipelineError("Gemini response was not a JSON object")
    return {str(key): str(value) for key, value in decoded.items()}


def translate_batch(batch: list[tuple[int, str, list[str]]], context: str) -> dict[int, str]:
    settings = CONFIG["translation"]
    targets = {str(index): masked for index, masked, _ in batch}
    prompt = (
        f"## Surrounding dialogue (context only, do not translate)\n{context}\n\n"
        f"## Translate these entries\n```json\n{json.dumps(targets, ensure_ascii=False, indent=1)}\n```"
    )
    for attempt in range(settings["max_batch_attempts"]):
        try:
            reply = call_gemini(prompt)
            break
        except (requests.RequestException, ValueError, KeyError, PipelineError) as exc:
            if attempt + 1 == settings["max_batch_attempts"]:
                raise PipelineError(f"translation batch failed: {exc}") from exc
            time.sleep(2 ** attempt * 2)

    translated: dict[int, str] = {}
    for index, _, tags in batch:
        candidate = reply.get(str(index))
        if candidate is None:
            continue
        restored = unmask_tags(candidate, tags)
        if restored is not None:
            translated[index] = restored
    return translated


def build_context(plain_lines: list[tuple[int, str]], start: int, end: int) -> str:
    window = CONFIG["translation"]["context_lines"]
    lo = max(0, start - window)
    hi = min(len(plain_lines), end + window)
    return "\n".join(f"{index}: {text}" for index, text in plain_lines[lo:hi])


def translate_subtitle_file(source: Path, destination: Path) -> int:
    """Translate in place and save; returns the number of lines left in English."""
    settings = CONFIG["translation"]
    subs = pysubs2.load(str(source))
    entries = [(index, line) for index, line in enumerate(subs) if line.text.strip()]
    if not entries:
        raise FatalJobError("subtitle file contains no dialogue")

    masked = [(index, *mask_tags(line.text)) for index, line in entries]
    plain = [(index, TAG_PATTERN.sub(" ", line.text).strip()) for index, line in entries]

    untranslated = 0
    batch_size = settings["batch_size"]
    for start in range(0, len(masked), batch_size):
        batch = masked[start:start + batch_size]
        translated = translate_batch(batch, build_context(plain, start, start + len(batch)))
        for index, _, _ in batch:
            if index in translated:
                subs[index].text = translated[index]
            else:
                untranslated += 1
        if start + batch_size < len(masked):
            time.sleep(settings["delay_between_batches"])

    ratio = untranslated / len(masked)
    if ratio > settings["max_untranslated_ratio"]:
        raise PipelineError(f"{untranslated}/{len(masked)} lines could not be translated")
    subs.save(str(destination))
    return untranslated


# --------------------------------------------------------------------------- service clients


class ArrClient:
    def __init__(self, media_type: str) -> None:
        prefix = "SONARR" if media_type == "series" else "RADARR"
        self.base_url = SECRETS[f"{prefix}_URL"].rstrip("/")
        self.headers = {"X-Api-Key": SECRETS[f"{prefix}_API_KEY"]}
        self.media_type = media_type
        self.scan_command = "DownloadedEpisodesScan" if media_type == "series" else "DownloadedMoviesScan"

    def _request(self, method: str, endpoint: str, **kwargs: Any) -> Any:
        response = requests.request(
            method,
            f"{self.base_url}/api/v3/{endpoint}",
            headers=self.headers,
            timeout=CONFIG["arr_request_timeout"],
            **kwargs,
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    def request_import(self, path: Path, download_id: str) -> int:
        """Ask *arr to import a finished folder. This is the single library write."""
        body = {
            "name": self.scan_command,
            "path": str(path),
            "downloadClientId": download_id.upper(),
            "importMode": "Move",
        }
        return int(self._request("POST", "command", json=body)["id"])

    def command_status(self, command_id: int) -> dict[str, Any]:
        return self._request("GET", f"command/{command_id}")


class QbitClient:
    """Read-only client used to find completions the webhook missed."""

    def __init__(self) -> None:
        self.base_url = str(CONFIG["qbittorrent"]["url"]).rstrip("/")
        self.session = requests.Session()
        self.authenticated = False

    def _login(self) -> None:
        username = SECRETS.get("QBIT_USERNAME")
        if not username:
            self.authenticated = True  # no credentials configured; the API may not need them
            return
        response = self.session.post(
            f"{self.base_url}/api/v2/auth/login",
            data={"username": username, "password": SECRETS.get("QBIT_PASSWORD") or ""},
            headers={"Referer": self.base_url},
            timeout=CONFIG["arr_request_timeout"],
        )
        body = response.text.strip()
        if response.status_code == 403:
            # qBittorrent bans a client after a few failed logins, and the ban outlives its cause.
            raise PipelineError(
                f"qBittorrent refused the login with 403 ({body[:120]!r}). That is usually its "
                "failed-login ban; restart qBittorrent to clear it."
            )
        if body == "Fails.":
            raise PipelineError("qBittorrent rejected the username or password")
        response.raise_for_status()
        # Any other 2xx counts as usable: a WebUI that bypasses auth for this subnet answers
        # 204 with no body, which is not a refusal. The torrent list below is the real test.
        self.authenticated = True

    def completed_torrents(self) -> list[dict[str, Any]]:
        for attempt in (1, 2):
            if not self.authenticated:
                self._login()
            response = self.session.get(
                f"{self.base_url}/api/v2/torrents/info",
                params={"filter": "completed"},
                timeout=CONFIG["arr_request_timeout"],
            )
            if response.status_code == 403 and attempt == 1:
                self.authenticated = False  # session expired, log in again and retry once
                continue
            response.raise_for_status()
            return response.json()
        raise PipelineError("qBittorrent kept refusing the torrent list; check the WebUI credentials")


# --------------------------------------------------------------------------- ingest


def ingest_download(download_id: str, name: str, category: str, content_path: str) -> dict[str, Any]:
    """Create one job row per media file in a finished torrent."""
    media_type = CONFIG["download_categories"].get(category)
    if media_type not in ("series", "movie"):
        return {"status": f"ignored category {category!r}"}

    content = Path(content_path)
    if not within_data_root(content):
        raise HTTPException(status_code=400, detail=f"{content} is outside {DATA_ROOT}")
    if not content.exists():
        raise HTTPException(status_code=400, detail=f"{content} does not exist")

    download_id = download_id.upper()
    now = time.time()
    media = find_media_files(content)

    if not media:
        # Recorded as failed so the reconcile loop stops offering it back.
        rows = [(download_id, name, category, media_type, str(content), STATE_FAILED,
                 "no media files found in this download", now, now)]
    else:
        rows = [(download_id, name, category, media_type, str(path), STATE_NEW, None, now, now)
                for path in media]

    with db() as conn:
        conn.executemany(
            """
            INSERT OR IGNORE INTO jobs
                (download_id, download_name, category, media_type, source_path,
                 state, error_detail, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        created = conn.total_changes

    log.info("accepted %s download %s (%s): %s file(s), %s new",
             media_type, download_id[:8], name, len(media), created)
    return {"status": "accepted", "files": len(media), "new": created}


# --------------------------------------------------------------------------- pipeline


def wait_for_stable_size(job: sqlite3.Row, path: Path) -> bool:
    size = path.stat().st_size
    if job["last_size"] == size:
        stable = job["stable_checks"] + 1
        update_job(job["id"], stable_checks=stable)
        return stable >= CONFIG["stability_checks"]
    update_job(job["id"], last_size=size, stable_checks=0)
    return False


def link_into_work(source: Path, work_dir: Path) -> Path:
    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / source.name
    if target.exists():
        if target.stat().st_ino == source.stat().st_ino:
            return target
        target = work_dir / f"{source.parent.name}-{source.name}"
        if target.exists():
            return target
    try:
        os.link(source, target)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            raise FatalJobError(
                f"{work_dir} and {source.parent} are on different filesystems; "
                "the whole /data tree must be one ZFS dataset for hardlinks to work"
            ) from exc
        raise PipelineError(f"could not hardlink into the work folder: {exc}") from exc
    return target


def handle_new(job: sqlite3.Row) -> None:
    source = Path(job["source_path"])
    if not source.exists():
        raise FatalJobError(f"source file disappeared: {source}")
    if not wait_for_stable_size(job, source):
        return

    key = job_key(job["download_id"])
    work_dir = WORK_ROOT / key
    work_path = link_into_work(source, work_dir)
    info = ffprobe(work_path)
    duration = probe_duration(info)

    scratch = SCRATCH_ROOT / key
    scratch.mkdir(parents=True, exist_ok=True)
    stream = pick_english_subtitle(info)
    if stream is None:
        image_only = any(s.get("codec_name") in IMAGE_SUBTITLE_CODECS
                         for s in streams_of_type(info, "subtitle"))
        detail = ("only image-based (PGS/VobSub) English subtitles found; OCR is not supported"
                  if image_only else "no English subtitle track found")
        log.warning("job %s: %s", job["id"], detail)
        update_job(job["id"], translation_state=TR_FAILED, error_detail=detail)
    else:
        ext = TEXT_SUBTITLE_CODECS[stream["codec_name"]]
        en_subtitle = scratch / f"{work_path.stem}.en{ext}"
        extract_subtitle(work_path, stream, en_subtitle)
        update_job(job["id"], subtitle_ext=ext, en_subtitle_path=str(en_subtitle),
                   translation_state=TR_PENDING)
        log.info("job %s: extracted English subtitles from %s", job["id"], work_path.name)

    if is_av1(info):
        embedded = streams_of_type(info, "subtitle")
        if embedded:
            strip_subtitles(work_path)
            log.info("job %s: already AV1, remuxed away %s embedded subtitle stream(s)",
                     job["id"], len(embedded))
        log.info("job %s: already AV1, skipping Tdarr", job["id"])
        update_job(job["id"], state=STATE_TRANSCODED, work_dir=str(work_dir),
                   work_path=str(work_path), source_duration=duration,
                   last_size=work_path.stat().st_size, stable_checks=0)
        return

    transcode_dir = TRANSCODE_ROOT / key / str(job["id"])
    transcode_dir.mkdir(parents=True, exist_ok=True)
    os.replace(work_path, transcode_dir / work_path.name)
    # work_path records where the file will come back to, not where it is right now.
    update_job(job["id"], state=STATE_TRANSCODING, work_dir=str(work_dir),
               work_path=str(work_path), transcode_dir=str(transcode_dir),
               source_duration=duration, staged_at=time.time(),
               last_size=None, stable_checks=0)
    log.info("job %s: handed %s to Tdarr", job["id"], work_path.name)


def find_transcode_output(transcode_dir: Path) -> Path | None:
    if not transcode_dir.is_dir():
        return None
    for candidate in sorted(transcode_dir.iterdir()):
        if not candidate.is_file() or candidate.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        if any(marker in candidate.name.lower() for marker in TDARR_TEMP_MARKERS):
            continue
        return candidate
    return None


def handle_transcoding(job: sqlite3.Row) -> None:
    transcode_dir = Path(job["transcode_dir"])
    elapsed_hours = (time.time() - (job["staged_at"] or time.time())) / 3600
    expired = elapsed_hours > CONFIG["transcode_timeout_hours"]

    candidate = find_transcode_output(transcode_dir)
    if candidate is None:
        if expired:
            raise FatalJobError("Tdarr removed the staged file without producing an output")
        return

    size = candidate.stat().st_size
    if job["last_size"] != size:
        update_job(job["id"], last_size=size, stable_checks=0)
        return
    if job["stable_checks"] + 1 < CONFIG["stability_checks"]:
        update_job(job["id"], stable_checks=job["stable_checks"] + 1)
        return

    info = ffprobe(candidate)
    if not is_av1(info):
        if expired:
            raise FatalJobError("Tdarr did not produce an AV1 file within the timeout")
        return
    remaining = streams_of_type(info, "subtitle")
    if remaining:
        if expired:
            raise FatalJobError(
                f"transcoded file still has {len(remaining)} embedded subtitle stream(s); "
                "configure Tdarr to strip subtitles"
            )
        return
    duration = probe_duration(info)
    expected = job["source_duration"] or 0.0
    if expected and duration and abs(duration - expected) / expected > 0.02:
        raise FatalJobError(f"transcoded duration {duration:.0f}s does not match source {expected:.0f}s")

    work_dir = Path(job["work_dir"])
    work_dir.mkdir(parents=True, exist_ok=True)
    final = work_dir / (Path(job["work_path"]).stem + candidate.suffix)
    os.replace(candidate, final)
    shutil.rmtree(transcode_dir, ignore_errors=True)
    update_job(job["id"], state=STATE_TRANSCODED, work_path=str(final),
               transcode_dir=None, last_size=final.stat().st_size, stable_checks=0)
    log.info("job %s: Tdarr output verified, %s is ready to import", job["id"], final.name)


def translation_settled(job: sqlite3.Row) -> bool:
    if job["translation_state"] == TR_DONE:
        return True
    return job["translation_state"] == TR_FAILED and CONFIG["import_without_subtitles"]


def place_sidecar(job: sqlite3.Row) -> None:
    """Move the Swedish subtitle out of scratch and name it after the final video."""
    if not job["sv_subtitle_path"]:
        return
    current = Path(job["sv_subtitle_path"])
    media = Path(job["work_path"])
    destination = sidecar_path(media, job["subtitle_ext"] or ".srt")
    if current == destination:
        return
    if not current.exists():
        log.warning("job %s: Swedish subtitle is missing from %s", job["id"], current)
        return
    shutil.move(str(current), str(destination))
    update_job(job["id"], sv_subtitle_path=str(destination))


def import_ready_downloads() -> None:
    """When every file of a torrent is final, ask *arr to import the folder."""
    rows = fetch_jobs("SELECT * FROM jobs WHERE state=? ORDER BY created_at", (STATE_TRANSCODED,))
    by_download: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_download.setdefault(row["download_id"], []).append(row)

    for download_id, jobs in by_download.items():
        waiting = fetch_jobs(
            "SELECT COUNT(*) AS pending FROM jobs WHERE download_id=? AND state IN (?,?)",
            (download_id, STATE_NEW, STATE_TRANSCODING),
        )[0]["pending"]
        if waiting:
            continue
        if not all(translation_settled(job) for job in jobs):
            continue

        try:
            for job in jobs:
                place_sidecar(job)
            work_dir = Path(jobs[0]["work_dir"])
            client = ArrClient(jobs[0]["media_type"])
            command_id = client.request_import(work_dir, download_id)
        except Exception as exc:  # noqa: BLE001 - retried on the next tick
            log.warning("download %s: could not request import: %s", download_id[:8], exc)
            continue

        update_download(
            download_id,
            state=STATE_IMPORTING,
            import_command_id=command_id,
            import_deadline=time.time() + CONFIG["import_timeout_minutes"] * 60,
            staged_at=time.time(),
        )
        log.info("download %s: import requested for %s (%s file(s))",
                 download_id[:8], work_dir, len(jobs))


def cleanup_download(download_id: str) -> None:
    key = job_key(download_id)
    for path in (WORK_ROOT / key, TRANSCODE_ROOT / key, SCRATCH_ROOT / key):
        shutil.rmtree(path, ignore_errors=True)


def advance_imports() -> None:
    rows = fetch_jobs("SELECT * FROM jobs WHERE state=?", (STATE_IMPORTING,))
    by_download: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_download.setdefault(row["download_id"], []).append(row)

    for download_id, jobs in by_download.items():
        first = jobs[0]
        try:
            status = ArrClient(first["media_type"]).command_status(first["import_command_id"])
        except Exception as exc:  # noqa: BLE001 - retried on the next tick
            log.warning("download %s: could not read import command: %s", download_id[:8], exc)
            continue

        state = status.get("status")
        expired = time.time() > (first["import_deadline"] or 0)
        if state in ("queued", "started") and not expired:
            continue
        if state not in ("queued", "started", "completed"):
            fail_download(download_id, f"*arr import command ended with status '{state}'")
            continue

        remaining = [job for job in jobs if Path(job["work_path"]).exists()]
        if remaining:
            if not expired:
                continue
            names = ", ".join(Path(job["work_path"]).name for job in remaining[:3])
            fail_download(
                download_id,
                f"*arr did not import {len(remaining)} of {len(jobs)} file(s) ({names}); "
                "files are left in the work folder for manual import",
            )
            continue

        for job in jobs:
            update_job(job["id"], state=STATE_DONE,
                       error_detail=import_note(job))
        cleanup_download(download_id)
        log.info("download %s: imported and cleaned up", download_id[:8])


def import_note(job: sqlite3.Row) -> str | None:
    if job["translation_state"] == TR_FAILED:
        return f"imported without Swedish subtitles: {job['error_detail']}"
    if job["untranslated_lines"]:
        return f"{job['untranslated_lines']} line(s) left untranslated"
    return None


HANDLERS = {STATE_NEW: handle_new, STATE_TRANSCODING: handle_transcoding}


def pipeline_tick() -> None:
    placeholders = ",".join("?" for _ in PER_FILE_STATES)
    jobs = fetch_jobs(f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY created_at", PER_FILE_STATES)
    for job in jobs:
        try:
            HANDLERS[job["state"]](job)
            if job["pipeline_attempts"]:
                update_job(job["id"], pipeline_attempts=0)
        except FatalJobError as exc:
            fail_job(job["id"], str(exc))
        except Exception as exc:  # noqa: BLE001 - retried, then failed with detail
            attempts = job["pipeline_attempts"] + 1
            log.warning("job %s step '%s' failed (attempt %s): %s", job["id"], job["state"], attempts, exc)
            if attempts >= CONFIG["max_pipeline_attempts"]:
                fail_job(job["id"], f"{job['state']}: {exc}")
            else:
                update_job(job["id"], pipeline_attempts=attempts, error_detail=str(exc)[:1000])

    import_ready_downloads()
    advance_imports()


def pipeline_loop() -> None:
    while not shutdown.is_set():
        try:
            pipeline_tick()
        except Exception:  # noqa: BLE001 - the loop must survive
            log.exception("pipeline tick crashed")
        shutdown.wait(CONFIG["poll_interval_seconds"])


# --------------------------------------------------------------------------- reconcile


def reconcile_once(client: QbitClient) -> int:
    torrents = client.completed_torrents()
    watermark = float(meta_get("reconcile_watermark") or 0)
    if not watermark:
        # First run: adopt the current state instead of ingesting the whole seed list.
        meta_set("reconcile_watermark", str(time.time()))
        log.info("reconcile: first run, adopting %s existing completed torrent(s)", len(torrents))
        return 0

    cutoff = max(watermark, time.time() - CONFIG["qbittorrent"]["reconcile_max_age_hours"] * 3600)
    enqueued = 0
    for torrent in torrents:
        category = torrent.get("category", "")
        if category not in CONFIG["download_categories"]:
            continue
        if float(torrent.get("completion_on") or 0) < cutoff:
            continue
        download_id = str(torrent.get("hash", "")).upper()
        if not download_id:
            continue
        known = fetch_jobs("SELECT 1 FROM jobs WHERE download_id=? LIMIT 1", (download_id,))
        if known:
            continue
        content_path = torrent.get("content_path") or torrent.get("save_path")
        if not content_path:
            continue
        log.info("reconcile: picking up missed completion %s (%s)", download_id[:8], torrent.get("name"))
        try:
            ingest_download(download_id, torrent.get("name", ""), category, content_path)
        except HTTPException as exc:
            log.warning("reconcile: skipping %s: %s", download_id[:8], exc.detail)
            continue
        enqueued += 1
    return enqueued


def reconcile_loop() -> None:
    if not CONFIG["qbittorrent"]["url"]:
        log.warning("qbittorrent.url is not configured; the reconcile safety net is disabled")
        return
    client = QbitClient()
    interval = CONFIG["qbittorrent"]["reconcile_interval_seconds"]
    while not shutdown.is_set():
        try:
            reconcile_once(client)
        except Exception as exc:  # noqa: BLE001 - the loop must survive
            log.warning("reconcile failed: %s", exc)
        shutdown.wait(interval)


# --------------------------------------------------------------------------- translation worker


def claim_translation_job() -> sqlite3.Row | None:
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM jobs WHERE translation_state=? AND en_subtitle_path IS NOT NULL "
            "AND state NOT IN (?,?) ORDER BY created_at LIMIT 1",
            (TR_PENDING, STATE_DONE, STATE_FAILED),
        ).fetchone()
        if row is None:
            return None
        claimed = conn.execute(
            "UPDATE jobs SET translation_state=?, updated_at=? WHERE id=? AND translation_state=?",
            (TR_PROCESSING, time.time(), row["id"], TR_PENDING),
        ).rowcount
    return row if claimed == 1 else None


def process_translation(job: sqlite3.Row) -> None:
    en_subtitle = Path(job["en_subtitle_path"])
    sv_subtitle = en_subtitle.with_name(en_subtitle.stem.removesuffix(".en") + ".sv" + en_subtitle.suffix)
    log.info("job %s: translating %s", job["id"], en_subtitle.name)
    try:
        with translation_lock:
            untranslated = translate_subtitle_file(en_subtitle, sv_subtitle)
    except FatalJobError as exc:
        update_job(job["id"], translation_state=TR_FAILED, error_detail=str(exc)[:1000])
        log.error("job %s: translation failed permanently: %s", job["id"], exc)
        return
    except Exception as exc:  # noqa: BLE001 - retried up to max_attempts
        attempts = job["translation_attempts"] + 1
        final = attempts >= CONFIG["translation"]["max_attempts"]
        update_job(job["id"], translation_state=TR_FAILED if final else TR_PENDING,
                   translation_attempts=attempts, error_detail=str(exc)[:1000])
        log.warning("job %s: translation attempt %s failed: %s", job["id"], attempts, exc)
        return
    en_subtitle.unlink(missing_ok=True)
    update_job(job["id"], translation_state=TR_DONE, sv_subtitle_path=str(sv_subtitle),
               untranslated_lines=untranslated, en_subtitle_path=None)
    log.info("job %s: translation done (%s untranslated lines)", job["id"], untranslated)


def translation_loop() -> None:
    while not shutdown.is_set():
        try:
            job = claim_translation_job()
            if job is not None:
                process_translation(job)
                continue
        except Exception:  # noqa: BLE001 - the loop must survive
            log.exception("translation tick crashed")
        shutdown.wait(CONFIG["poll_interval_seconds"])


# --------------------------------------------------------------------------- web app

app = FastAPI(title="Media gatekeeper")


def require_secret(x_api_key: str = Header(default="")) -> None:
    if not hmac.compare_digest(x_api_key, SECRETS["WEBHOOK_SECRET"]):
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
def health() -> dict[str, Any]:
    counts = fetch_jobs("SELECT state, COUNT(*) AS total FROM jobs GROUP BY state")
    return {"status": "ok", "jobs": {row["state"]: row["total"] for row in counts}}


@app.get("/api/jobs", dependencies=[Depends(require_secret)])
def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    rows = fetch_jobs("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 500)),))
    return [dict(row) for row in rows]


@app.post("/api/jobs/{job_id}/retry", dependencies=[Depends(require_secret)])
def retry_job(job_id: int) -> dict[str, Any]:
    """Put a failed job back at the start of the pipeline. Nothing on disk is touched."""
    rows = fetch_jobs("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not rows:
        raise HTTPException(status_code=404, detail=f"no job with id {job_id}")
    job = rows[0]
    if job["state"] != STATE_FAILED:
        raise HTTPException(status_code=409, detail=f"job {job_id} is {job['state']}, not failed")
    update_job(
        job_id,
        state=STATE_NEW,
        error_detail=None,
        staged_at=None,
        transcode_dir=None,
        last_size=None,
        stable_checks=0,
        import_command_id=None,
        import_deadline=None,
        pipeline_attempts=0,
        translation_state=TR_PENDING,
        translation_attempts=0,
    )
    log.info("job %s: reset for another attempt", job_id)
    return {"status": "retrying", "id": job_id}


@app.post("/api/download/complete", status_code=202, dependencies=[Depends(require_secret)])
def download_complete(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """Called by qBittorrent's "run on torrent finished" hook. This starts the pipeline."""
    download_id = str(payload.get("hash") or "").strip()
    content_path = str(payload.get("path") or "").strip()
    if not download_id or not content_path:
        raise HTTPException(status_code=400, detail="hash and path are required")
    return ingest_download(
        download_id,
        str(payload.get("name") or ""),
        str(payload.get("category") or ""),
        content_path,
    )


@app.post("/api/import", status_code=202, dependencies=[Depends(require_secret)])
def import_webhook(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, str]:
    """*arr's On Import webhook. Confirmation only — the pipeline no longer starts here."""
    if payload.get("eventType") == "Test":
        return {"status": "Test successful"}
    download_id = str(payload.get("downloadId") or "").upper()
    if not download_id:
        return {"status": "ignored: no downloadId"}

    imported = (payload.get("episodeFile") or payload.get("movieFile") or {}).get("path")
    if imported:
        for job in fetch_jobs("SELECT * FROM jobs WHERE download_id=?", (download_id,)):
            if Path(job["work_path"] or "").stem == Path(imported).stem:
                update_job(job["id"], imported_path=imported)
    log.info("download %s: *arr confirmed an import (%s)", download_id[:8], imported or "path unknown")
    return {"status": "recorded"}


@app.post("/translate", dependencies=[Depends(require_secret)])
def translate_upload(file: UploadFile = File(...)) -> Response:
    """Translate an uploaded subtitle file and return it. Used by Bazarr's post-processing hook."""
    suffix = Path(file.filename or "subtitle.srt").suffix.lower()
    if suffix not in SUBTITLE_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"unsupported subtitle format {suffix!r}")

    scratch = Path(tempfile.mkdtemp(prefix="translate-", dir=str(SCRATCH_ROOT)))
    try:
        source = scratch / f"in{suffix}"
        destination = scratch / f"out{suffix}"
        source.write_bytes(file.file.read())
        try:
            with translation_lock:
                untranslated = translate_subtitle_file(source, destination)
        except FatalJobError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller
            raise HTTPException(status_code=502, detail=f"translation failed: {exc}") from exc
        log.info("translated upload %s (%s untranslated lines)", file.filename, untranslated)
        return Response(content=destination.read_bytes(), media_type="text/plain; charset=utf-8")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# --------------------------------------------------------------------------- startup


def preflight() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} is not installed or not on PATH")
    if not DATA_ROOT.exists():
        raise SystemExit(f"{DATA_ROOT} does not exist in this container — check the /data mount")
    for directory in (WORK_ROOT, TRANSCODE_ROOT, SCRATCH_ROOT):
        directory.mkdir(parents=True, exist_ok=True)
        if not os.access(directory, os.W_OK):
            raise SystemExit(f"{directory} is not writable")
    if COMPLETE_ROOT.exists() and COMPLETE_ROOT.stat().st_dev != WORK_ROOT.stat().st_dev:
        log.error(
            "%s and %s are on different filesystems — hardlinking will fail. "
            "The whole /data tree must be a single ZFS dataset.",
            COMPLETE_ROOT, WORK_ROOT,
        )
    unknown = [c for c, kind in CONFIG["download_categories"].items() if kind not in ("series", "movie")]
    if unknown:
        raise SystemExit(f"download_categories must map to 'series' or 'movie': {unknown}")


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    init_db()
    preflight()
    threading.Thread(target=pipeline_loop, name="pipeline", daemon=True).start()
    threading.Thread(target=translation_loop, name="translation", daemon=True).start()
    threading.Thread(target=reconcile_loop, name="reconcile", daemon=True).start()
    try:
        uvicorn.run(app, host=CONFIG["listen_host"], port=CONFIG["listen_port"], log_level="warning")
    finally:
        shutdown.set()


if __name__ == "__main__":
    main()
