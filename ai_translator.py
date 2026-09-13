#!/usr/bin/env python3
"""Subtitle translation gate between Sonarr/Radarr imports and Tdarr transcoding.

Pipeline per imported file:
  import webhook -> wait for stable file -> extract English subtitles
  -> hand the file to Tdarr (hardlink into Tdarr's watch folder)
  -> translate subtitles to Swedish while Tdarr transcodes
  -> verify Tdarr output (AV1, no embedded subtitles) -> swap it back into the
     ingest folder next to the .sv sidecar -> rescan in *arr -> move to the
     cold library root -> verify.

Subtitles are extracted before Tdarr ever sees the file, so the embedded
English track cannot be stripped out from under us.
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
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pysubs2
import requests
import uvicorn
from fastapi import Body, Depends, FastAPI, Header, HTTPException

log = logging.getLogger("ai-translator")

BASE_DIR = Path(os.environ.get("AI_TRANSLATOR_HOME", "/opt/ai-translator"))
SECRETS_FILE = Path(os.environ.get("AI_TRANSLATOR_SECRETS", BASE_DIR / "secrets.json"))
CONFIG_FILE = Path(os.environ.get("AI_TRANSLATOR_CONFIG", BASE_DIR / "config.json"))

DEFAULTS: dict[str, Any] = {
    "db_file": str(BASE_DIR / "state.db"),
    "work_dir": str(BASE_DIR / "work"),
    "tdarr_staging_root": "/tdarr-ingest",
    "media_roots": ["/arr-ingest", "/library"],
    "path_map": {},
    "tv_target_root": "/library/tv",
    "movie_target_root": "/library/movies",
    "anime_tv_target_root": "/library/anime/tv",
    "anime_marker": "/anime/",
    "listen_host": "0.0.0.0",
    "listen_port": 5000,
    "poll_interval_seconds": 10,
    "stability_checks": 2,
    "transcode_timeout_hours": 12,
    "move_timeout_minutes": 180,
    "command_timeout_minutes": 30,
    "move_without_subtitles": True,
    "arr_request_timeout": 30,
    "ffmpeg_timeout": 900,
    "max_pipeline_attempts": 5,
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
TEXT_SUBTITLE_CODECS = {"ass": ".ass", "ssa": ".ass", "subrip": ".srt", "text": ".srt", "mov_text": ".srt"}
IMAGE_SUBTITLE_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
ENGLISH_TAGS = {"eng", "en", "en-us", "en-gb", "english"}
ACCEPTED_EVENT_TYPES = {"Download", "Import", "EpisodeFileImported", "MovieFileImported"}

# Tdarr writes its work-in-progress under these names inside the library folder.
TDARR_TEMP_MARKERS = ("tdarrcachefile", "-tdarr-", ".partial", ".tmp")

STATE_NEW = "new"
STATE_STAGED = "staged"
STATE_TRANSCODED = "transcoded"
STATE_RESCAN_PENDING = "rescan_pending"
STATE_MOVE_PENDING = "move_pending"
STATE_DONE = "done"
STATE_FAILED = "failed"
ACTIVE_STATES = (STATE_NEW, STATE_STAGED, STATE_TRANSCODED, STATE_RESCAN_PENDING, STATE_MOVE_PENDING)

TR_PENDING, TR_PROCESSING, TR_DONE, TR_FAILED = "pending", "processing", "done", "failed"

shutdown = threading.Event()


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
WORK_DIR = Path(CONFIG["work_dir"])
STAGING_ROOT = Path(CONFIG["tdarr_staging_root"])
MEDIA_ROOTS = [Path(root) for root in CONFIG["media_roots"]]
GEMINI_URL = (
    f"https://generativelanguage.googleapis.com/v1beta/models/"
    f"{CONFIG['translation']['model']}:generateContent"
)


# --------------------------------------------------------------------------- database

COLUMNS: dict[str, str] = {
    "id": "INTEGER PRIMARY KEY AUTOINCREMENT",
    "source_path": "TEXT NOT NULL UNIQUE",
    "current_path": "TEXT NOT NULL",
    "media_type": "TEXT NOT NULL CHECK(media_type IN ('series','movie'))",
    "item_id": "INTEGER NOT NULL",
    "target_root": "TEXT NOT NULL",
    "state": "TEXT NOT NULL DEFAULT 'new'",
    "translation_state": "TEXT NOT NULL DEFAULT 'pending'",
    "translation_attempts": "INTEGER NOT NULL DEFAULT 0",
    "pipeline_attempts": "INTEGER NOT NULL DEFAULT 0",
    "subtitle_ext": "TEXT",
    "en_subtitle_path": "TEXT",
    "sv_subtitle_path": "TEXT",
    "untranslated_lines": "INTEGER NOT NULL DEFAULT 0",
    "staging_dir": "TEXT",
    "staged_at": "REAL",
    "source_duration": "REAL",
    "last_size": "INTEGER",
    "stable_checks": "INTEGER NOT NULL DEFAULT 0",
    "arr_command_id": "INTEGER",
    "move_deadline": "REAL",
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
        if existing and "source_path" not in existing:
            log.warning("found a pre-rewrite jobs table, renaming it to jobs_legacy")
            conn.execute("DROP TABLE IF EXISTS jobs_legacy")
            conn.execute("ALTER TABLE jobs RENAME TO jobs_legacy")
            existing = set()
        conn.execute(f"CREATE TABLE IF NOT EXISTS jobs ({columns_sql})")
        for name, ddl in COLUMNS.items():
            if existing and name not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {ddl}")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_translation ON jobs(translation_state)")


def fetch_jobs(query: str, params: tuple = ()) -> list[sqlite3.Row]:
    with db() as conn:
        return conn.execute(query, params).fetchall()


def get_job(job_id: int) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()


def update_job(job_id: int, **fields: Any) -> None:
    unknown = set(fields) - UPDATABLE_COLUMNS
    if unknown:
        raise ValueError(f"unknown job columns: {sorted(unknown)}")
    fields["updated_at"] = time.time()
    assignments = ", ".join(f"{name}=?" for name in fields)
    with db() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id=?", (*fields.values(), job_id))


def fail_job(job_id: int, detail: str) -> None:
    log.error("job %s failed: %s", job_id, detail)
    update_job(job_id, state=STATE_FAILED, error_detail=detail[:1000])


def job_key(source_path: str) -> str:
    return hashlib.sha1(source_path.encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- paths


def map_path(raw: str) -> str:
    for source, destination in CONFIG["path_map"].items():
        if raw == source or raw.startswith(source.rstrip("/") + "/"):
            return destination.rstrip("/") + raw[len(source.rstrip("/")):]
    return raw


def within_media_roots(path: Path) -> bool:
    resolved = Path(os.path.normpath(str(path)))
    return any(resolved == root or root in resolved.parents for root in MEDIA_ROOTS)


def sidecar_path(media: Path, ext: str) -> Path:
    return media.with_name(f"{media.stem}.sv{ext}")


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
        if codec in IMAGE_SUBTITLE_CODECS:
            continue
        if codec not in TEXT_SUBTITLE_CODECS:
            continue
        forced = bool(stream.get("disposition", {}).get("forced")) or "forced" in title
        codec_rank = {"ass": 0, "ssa": 0, "subrip": 1, "text": 1, "mov_text": 2}[codec]
        candidates.append(((forced, codec_rank, stream.get("index", 0)), stream))
    if not candidates:
        return None
    return min(candidates, key=lambda item: item[0])[1]


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


# --------------------------------------------------------------------------- *arr client


class ArrClient:
    def __init__(self, media_type: str) -> None:
        prefix = "SONARR" if media_type == "series" else "RADARR"
        self.base_url = SECRETS[f"{prefix}_URL"].rstrip("/")
        self.headers = {"X-Api-Key": SECRETS[f"{prefix}_API_KEY"]}
        self.media_type = media_type
        self.item_endpoint = "series" if media_type == "series" else "movie"
        self.file_endpoint = "episodefile" if media_type == "series" else "moviefile"
        self.id_field = "seriesId" if media_type == "series" else "movieId"

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

    def get_item(self, item_id: int) -> dict[str, Any]:
        return self._request("GET", f"{self.item_endpoint}/{item_id}")

    def get_file(self, file_id: int) -> dict[str, Any]:
        return self._request("GET", f"{self.file_endpoint}/{file_id}")

    def list_files(self, item_id: int) -> list[dict[str, Any]]:
        files = self._request("GET", f"{self.file_endpoint}?{self.id_field}={item_id}")
        return files if isinstance(files, list) else [files]

    def rescan(self, item_id: int) -> int:
        name = "RescanSeries" if self.media_type == "series" else "RescanMovie"
        return int(self._request("POST", "command", json={"name": name, self.id_field: item_id})["id"])

    def command_status(self, command_id: int) -> dict[str, Any]:
        return self._request("GET", f"command/{command_id}")

    def move_to_root(self, item: dict[str, Any], target_root: str) -> None:
        folder = os.path.basename(str(item.get("path", "")).rstrip("/"))
        if not folder:
            raise PipelineError("could not determine the item folder name")
        item["rootFolderPath"] = target_root
        item["path"] = os.path.join(target_root, folder)
        self._request("PUT", f"{self.item_endpoint}/{item['id']}?moveFiles=true", json=item)


# --------------------------------------------------------------------------- pipeline steps


def wait_for_stable_file(job: sqlite3.Row) -> bool:
    path = Path(job["current_path"])
    if not path.exists():
        raise FatalJobError(f"source file disappeared: {path}")
    size = path.stat().st_size
    if job["last_size"] == size:
        stable = job["stable_checks"] + 1
        update_job(job["id"], stable_checks=stable)
        return stable >= CONFIG["stability_checks"]
    update_job(job["id"], last_size=size, stable_checks=0)
    return False


def stage_for_tdarr(job: sqlite3.Row) -> Path:
    source = Path(job["current_path"])
    staging_dir = STAGING_ROOT / job_key(job["source_path"])
    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)
    try:
        os.link(source, staging_dir / source.name)
    except OSError as exc:
        shutil.rmtree(staging_dir, ignore_errors=True)
        if exc.errno == errno.EXDEV:
            raise FatalJobError(
                f"{STAGING_ROOT} and {source.parent} are on different filesystems; "
                "the Tdarr staging folder must live on the same filesystem as the ingest folder"
            ) from exc
        raise PipelineError(f"could not stage file for Tdarr: {exc}") from exc
    return staging_dir


def handle_new(job: sqlite3.Row) -> None:
    if not wait_for_stable_file(job):
        return
    media = Path(job["current_path"])
    info = ffprobe(media)
    work_dir = WORK_DIR / job_key(job["source_path"])
    work_dir.mkdir(parents=True, exist_ok=True)

    stream = pick_english_subtitle(info)
    if stream is None:
        image_only = any(
            s.get("codec_name") in IMAGE_SUBTITLE_CODECS for s in streams_of_type(info, "subtitle")
        )
        detail = (
            "only image-based (PGS/VobSub) English subtitles found; OCR is not supported"
            if image_only
            else "no English subtitle track found"
        )
        log.warning("job %s: %s", job["id"], detail)
        update_job(job["id"], translation_state=TR_FAILED, error_detail=detail)
    else:
        ext = TEXT_SUBTITLE_CODECS[stream["codec_name"]]
        en_subtitle = work_dir / f"{media.stem}.en{ext}"
        extract_subtitle(media, stream, en_subtitle)
        update_job(job["id"], subtitle_ext=ext, en_subtitle_path=str(en_subtitle), translation_state=TR_PENDING)
        log.info("job %s: extracted English subtitles to %s", job["id"], en_subtitle)

    duration = probe_duration(info)
    if is_av1(info) and not streams_of_type(info, "subtitle"):
        log.info("job %s: already AV1 with no embedded subtitles, skipping Tdarr", job["id"])
        update_job(job["id"], state=STATE_TRANSCODED, source_duration=duration)
        return

    staging_dir = stage_for_tdarr(job)
    update_job(
        job["id"],
        state=STATE_STAGED,
        staging_dir=str(staging_dir),
        staged_at=time.time(),
        source_duration=duration,
        last_size=None,
        stable_checks=0,
    )
    log.info("job %s: handed to Tdarr at %s", job["id"], staging_dir)


def find_transcode_output(staging_dir: Path) -> Path | None:
    if not staging_dir.is_dir():
        return None
    for candidate in sorted(staging_dir.iterdir()):
        if not candidate.is_file() or candidate.suffix.lower() not in MEDIA_EXTENSIONS:
            continue
        if any(marker in candidate.name.lower() for marker in TDARR_TEMP_MARKERS):
            continue
        return candidate
    return None


def handle_staged(job: sqlite3.Row) -> None:
    staging_dir = Path(job["staging_dir"])
    elapsed_hours = (time.time() - (job["staged_at"] or time.time())) / 3600
    candidate = find_transcode_output(staging_dir)
    if candidate is None:
        if elapsed_hours > CONFIG["transcode_timeout_hours"]:
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
        if elapsed_hours > CONFIG["transcode_timeout_hours"]:
            raise FatalJobError("Tdarr did not produce an AV1 file within the timeout")
        return
    remaining_subs = streams_of_type(info, "subtitle")
    if remaining_subs:
        if elapsed_hours > CONFIG["transcode_timeout_hours"]:
            raise FatalJobError(
                f"transcoded file still has {len(remaining_subs)} embedded subtitle stream(s); "
                "configure Tdarr to strip subtitles"
            )
        return
    duration = probe_duration(info)
    expected = job["source_duration"] or 0.0
    if expected and duration and abs(duration - expected) / expected > 0.02:
        raise FatalJobError(f"transcoded duration {duration:.0f}s does not match source {expected:.0f}s")

    log.info("job %s: Tdarr output verified (%s)", job["id"], candidate.name)
    update_job(job["id"], state=STATE_TRANSCODED, last_size=None, stable_checks=0)


def translation_settled(job: sqlite3.Row) -> bool:
    if job["translation_state"] == TR_DONE:
        return True
    return job["translation_state"] == TR_FAILED and CONFIG["move_without_subtitles"]


def swap_in_transcoded_file(job: sqlite3.Row) -> Path:
    """Replace the ingest file with Tdarr's output and park the .sv sidecar beside it."""
    original = Path(job["current_path"])
    staging_dir = Path(job["staging_dir"]) if job["staging_dir"] else None
    transcoded = find_transcode_output(staging_dir) if staging_dir else None

    final = original
    if transcoded is not None:
        final = original.with_suffix(transcoded.suffix)
        os.replace(transcoded, final)
        if final != original and original.exists():
            original.unlink()
    if staging_dir is not None:
        shutil.rmtree(staging_dir, ignore_errors=True)

    place_sidecar_next_to(job, final)
    shutil.rmtree(WORK_DIR / job_key(job["source_path"]), ignore_errors=True)
    update_job(job["id"], current_path=str(final), staging_dir=None, last_size=final.stat().st_size)
    return final


def handle_transcoded(job: sqlite3.Row) -> None:
    if not translation_settled(job):
        return
    final = swap_in_transcoded_file(job)
    log.info("job %s: final media file is %s", job["id"], final)
    client = ArrClient(job["media_type"])
    command_id = client.rescan(job["item_id"])
    update_job(job["id"], state=STATE_RESCAN_PENDING, arr_command_id=command_id, staged_at=time.time())


def handle_rescan_pending(job: sqlite3.Row) -> None:
    client = ArrClient(job["media_type"])
    status = client.command_status(job["arr_command_id"])
    state = status.get("status")
    if state in ("queued", "started"):
        if time.time() - (job["staged_at"] or 0) > CONFIG["command_timeout_minutes"] * 60:
            raise FatalJobError("*arr rescan did not finish within the timeout")
        return
    if state != "completed":
        raise FatalJobError(f"*arr rescan ended with status '{state}'")

    item = client.get_item(job["item_id"])
    if os.path.normpath(str(item.get("rootFolderPath", ""))) == os.path.normpath(job["target_root"]):
        log.info("job %s: item already in %s, no move needed", job["id"], job["target_root"])
        finish_job(job, client, item)
        return
    client.move_to_root(item, job["target_root"])
    update_job(
        job["id"],
        state=STATE_MOVE_PENDING,
        arr_command_id=None,
        move_deadline=time.time() + CONFIG["move_timeout_minutes"] * 60,
    )
    log.info("job %s: move to %s requested", job["id"], job["target_root"])


def locate_moved_file(client: ArrClient, job: sqlite3.Row, item: dict[str, Any]) -> Path | None:
    """Find our file in *arr's records after a rescan/move, tolerating renames."""
    expected_size = job["last_size"]
    item_path = os.path.normpath(str(item.get("path", "")))
    best: Path | None = None
    for record in client.list_files(job["item_id"]):
        record_path = record.get("path")
        if not record_path:
            continue
        candidate = Path(record_path)
        if item_path and not str(candidate).startswith(item_path + os.sep):
            continue
        if expected_size is not None and record.get("size") == expected_size:
            return candidate
        if candidate.stem == Path(job["current_path"]).stem:
            best = candidate
    return best


def place_sidecar_next_to(job: sqlite3.Row, media: Path) -> None:
    current = Path(job["sv_subtitle_path"]) if job["sv_subtitle_path"] else None
    if current is None:
        return
    destination = sidecar_path(media, job["subtitle_ext"] or ".srt")
    if current == destination and destination.exists():
        return
    if destination.exists():
        update_job(job["id"], sv_subtitle_path=str(destination))
        return
    if not current.exists():
        log.warning("job %s: Swedish subtitle is missing from %s", job["id"], current)
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(current), str(destination))
    update_job(job["id"], sv_subtitle_path=str(destination))
    log.info("job %s: placed Swedish subtitle at %s", job["id"], destination)


def finish_job(job: sqlite3.Row, client: ArrClient, item: dict[str, Any]) -> None:
    media = locate_moved_file(client, job, item)
    if media is None or not media.exists():
        raise PipelineError("*arr has not registered the media file at its destination yet")
    place_sidecar_next_to(job, media)
    detail = None
    if job["translation_state"] == TR_FAILED:
        detail = f"moved without Swedish subtitles: {job['error_detail']}"
    elif job["untranslated_lines"]:
        detail = f"{job['untranslated_lines']} line(s) left untranslated"
    update_job(job["id"], state=STATE_DONE, current_path=str(media), error_detail=detail)
    log.info("job %s: complete at %s", job["id"], media)


def handle_move_pending(job: sqlite3.Row) -> None:
    client = ArrClient(job["media_type"])
    item = client.get_item(job["item_id"])
    root = os.path.normpath(str(item.get("rootFolderPath", "")))
    if root == os.path.normpath(job["target_root"]):
        media = locate_moved_file(client, job, item)
        if media is not None and media.exists():
            finish_job(job, client, item)
            return
    if time.time() > (job["move_deadline"] or 0):
        # Nothing is deleted here: both copies, if any, are left for manual inspection.
        raise FatalJobError(f"*arr did not move the item into {job['target_root']} within the timeout")


HANDLERS = {
    STATE_NEW: handle_new,
    STATE_STAGED: handle_staged,
    STATE_TRANSCODED: handle_transcoded,
    STATE_RESCAN_PENDING: handle_rescan_pending,
    STATE_MOVE_PENDING: handle_move_pending,
}


def pipeline_tick() -> None:
    placeholders = ",".join("?" for _ in ACTIVE_STATES)
    jobs = fetch_jobs(f"SELECT * FROM jobs WHERE state IN ({placeholders}) ORDER BY created_at", ACTIVE_STATES)
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


def pipeline_loop() -> None:
    while not shutdown.is_set():
        try:
            pipeline_tick()
        except Exception:  # noqa: BLE001 - the loop must survive
            log.exception("pipeline tick crashed")
        shutdown.wait(CONFIG["poll_interval_seconds"])


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
    sv_subtitle = en_subtitle.with_name(
        en_subtitle.stem.removesuffix(".en") + ".sv" + en_subtitle.suffix
    )
    log.info("job %s: translating %s", job["id"], en_subtitle.name)
    try:
        untranslated = translate_subtitle_file(en_subtitle, sv_subtitle)
    except FatalJobError as exc:
        update_job(job["id"], translation_state=TR_FAILED, error_detail=str(exc)[:1000])
        log.error("job %s: translation failed permanently: %s", job["id"], exc)
        return
    except Exception as exc:  # noqa: BLE001 - retried up to max_attempts
        attempts = job["translation_attempts"] + 1
        final = attempts >= CONFIG["translation"]["max_attempts"]
        update_job(
            job["id"],
            translation_state=TR_FAILED if final else TR_PENDING,
            translation_attempts=attempts,
            error_detail=str(exc)[:1000],
        )
        log.warning("job %s: translation attempt %s failed: %s", job["id"], attempts, exc)
        return
    en_subtitle.unlink(missing_ok=True)
    update_job(
        job["id"],
        translation_state=TR_DONE,
        sv_subtitle_path=str(sv_subtitle),
        untranslated_lines=untranslated,
        en_subtitle_path=None,
    )
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

app = FastAPI(title="AI Translator")


def require_secret(x_api_key: str = Header(default="")) -> None:
    if not hmac.compare_digest(x_api_key, SECRETS["WEBHOOK_SECRET"]):
        raise HTTPException(status_code=401, detail="Unauthorized")


def resolve_path(payload: dict[str, Any], media_type: str) -> str:
    file_payload = payload.get("episodeFile") or payload.get("movieFile") or {}
    raw = file_payload.get("path")
    if not raw or not str(raw).startswith("/"):
        file_id = file_payload.get("id")
        if not file_id:
            raise HTTPException(status_code=400, detail="Payload contains no file path or file id")
        try:
            raw = ArrClient(media_type).get_file(int(file_id)).get("path")
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"Could not resolve path from *arr: {exc}") from exc
    if not raw:
        raise HTTPException(status_code=400, detail="Could not determine the imported file path")
    return map_path(str(raw))


def target_root_for(payload: dict[str, Any], media_type: str) -> str:
    if media_type == "movie":
        return CONFIG["movie_target_root"]
    series_path = str(payload.get("series", {}).get("path", "")).lower()
    if CONFIG["anime_marker"] in series_path:
        return CONFIG["anime_tv_target_root"]
    return CONFIG["tv_target_root"]


@app.get("/health")
def health() -> dict[str, Any]:
    counts = fetch_jobs("SELECT state, COUNT(*) AS total FROM jobs GROUP BY state")
    return {"status": "ok", "jobs": {row["state"]: row["total"] for row in counts}}


@app.get("/api/jobs", dependencies=[Depends(require_secret)])
def list_jobs(limit: int = 50) -> list[dict[str, Any]]:
    rows = fetch_jobs("SELECT * FROM jobs ORDER BY updated_at DESC LIMIT ?", (max(1, min(limit, 500)),))
    return [dict(row) for row in rows]


@app.post("/api/import", status_code=202, dependencies=[Depends(require_secret)])
def import_webhook(payload: dict[str, Any] = Body(default_factory=dict)) -> dict[str, str]:
    event_type = payload.get("eventType")
    if event_type == "Test":
        return {"status": "Test successful"}
    if event_type not in ACCEPTED_EVENT_TYPES:
        return {"status": f"ignored event {event_type}"}

    media_type = "series" if "series" in payload else "movie"
    item = payload.get("series") or payload.get("movie") or {}
    item_id = item.get("id")
    if item_id is None:
        raise HTTPException(status_code=400, detail="Payload contains no series/movie id")

    path = resolve_path(payload, media_type)
    if not within_media_roots(Path(path)):
        raise HTTPException(status_code=400, detail=f"Path {path} is outside the configured media roots")

    now = time.time()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO jobs (source_path, current_path, media_type, item_id, target_root, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_path) DO UPDATE SET
                current_path=excluded.current_path,
                media_type=excluded.media_type,
                item_id=excluded.item_id,
                target_root=excluded.target_root,
                state='new',
                translation_state='pending',
                translation_attempts=0,
                pipeline_attempts=0,
                subtitle_ext=NULL,
                en_subtitle_path=NULL,
                sv_subtitle_path=NULL,
                untranslated_lines=0,
                staging_dir=NULL,
                staged_at=NULL,
                source_duration=NULL,
                last_size=NULL,
                stable_checks=0,
                arr_command_id=NULL,
                move_deadline=NULL,
                error_detail=NULL,
                updated_at=excluded.updated_at
            """,
            (path, path, media_type, int(item_id), target_root_for(payload, media_type), now, now),
        )
    log.info("accepted %s import: %s", media_type, path)
    return {"status": "accepted"}


# --------------------------------------------------------------------------- startup


def preflight() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            raise SystemExit(f"{tool} is not installed or not on PATH")
    for directory in (WORK_DIR, STAGING_ROOT):
        directory.mkdir(parents=True, exist_ok=True)
        if not os.access(directory, os.W_OK):
            raise SystemExit(f"{directory} is not writable")
    reachable = []
    for root in MEDIA_ROOTS:
        if root.exists():
            reachable.append(root)
        else:
            log.warning("configured media root %s does not exist here; check path_map / bind mounts", root)
    if not reachable:
        raise SystemExit("none of the configured media_roots exist in this container")
    if STAGING_ROOT.stat().st_dev != reachable[0].stat().st_dev:
        log.warning(
            "%s and %s are on different filesystems; hardlink staging to Tdarr will fail",
            STAGING_ROOT,
            reachable[0],
        )


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    init_db()
    preflight()
    threading.Thread(target=pipeline_loop, name="pipeline", daemon=True).start()
    threading.Thread(target=translation_loop, name="translation", daemon=True).start()
    try:
        uvicorn.run(app, host=CONFIG["listen_host"], port=CONFIG["listen_port"], log_level="warning")
    finally:
        shutdown.set()


if __name__ == "__main__":
    main()
