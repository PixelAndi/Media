"""Failure paths: auth, validation, missing subs, un-stripped subs, import timeout,
reconcile watermark, the /translate endpoint, and schema migration."""
import io, json, os, shutil, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = Path(tempfile.mkdtemp(prefix="gatekeeper-failures-"))

DATA = BASE / "data"
for sub in ("torrents/complete", "work", "transcode"):
    (DATA / sub).mkdir(parents=True)

(BASE / "secrets.json").write_text(json.dumps({
    "SONARR_URL": "http://localhost:8989", "SONARR_API_KEY": "k1",
    "RADARR_URL": "http://localhost:7878", "RADARR_API_KEY": "k2",
    "GEMINI_API_KEY": "g", "WEBHOOK_SECRET": "s3cret"}))
(BASE / "config.json").write_text(json.dumps({
    "db_file": str(BASE / "state.db"),
    "data_root": str(DATA),
    "complete_root": str(DATA / "torrents/complete"),
    "work_root": str(DATA / "work"),
    "transcode_root": str(DATA / "transcode"),
    "stability_checks": 1,
    "min_media_bytes": 1,
    "import_timeout_minutes": 0.0001,
    "qbittorrent": {"url": "http://qbit.invalid:8080"}}))

# a jobs table from the previous design must be migrated aside, not crashed on
legacy = sqlite3.connect(BASE / "state.db")
legacy.execute("CREATE TABLE jobs (id INTEGER PRIMARY KEY, source_path TEXT, state TEXT)")
legacy.execute("INSERT INTO jobs VALUES (1, '/old/file.mkv', 'done')")
legacy.commit(); legacy.close()

os.environ["AI_TRANSLATOR_SECRETS"] = str(BASE / "secrets.json")
os.environ["AI_TRANSLATOR_CONFIG"] = str(BASE / "config.json")
sys.path.insert(0, str(REPO))
import ai_translator as t
from fastapi import HTTPException

t.init_db()
cols = {r["name"] for r in t.fetch_jobs("SELECT name FROM pragma_table_info('jobs')")}
assert "download_id" in cols and "work_dir" in cols
assert t.fetch_jobs("SELECT * FROM jobs_legacy")[0]["source_path"] == "/old/file.mkv"
print("migration: previous jobs table preserved as jobs_legacy")

# --- auth ---------------------------------------------------------------------
for bad in ("", "wrong", "s3cre"):
    try:
        t.require_secret(bad); raise SystemExit(f"secret {bad!r} was accepted")
    except HTTPException as exc:
        assert exc.status_code == 401
t.require_secret("s3cret")
print("auth: rejects empty/wrong secrets, accepts the right one")

# --- ingest validation --------------------------------------------------------
assert "ignored" in t.download_complete(
    {"hash": "a1", "path": str(DATA), "category": "unknown-cat", "name": "x"})["status"]
assert t.fetch_jobs("SELECT * FROM jobs") == []
print("ingest: unknown categories are ignored")

for payload in ({"hash": "a1"}, {"path": "/x"}, {}):
    try:
        t.download_complete(payload); raise SystemExit("incomplete payload accepted")
    except HTTPException as exc:
        assert exc.status_code == 400
print("ingest: rejects payloads without a hash and path")

try:
    t.download_complete({"hash": "a1", "path": "/etc", "category": "tv-sonarr", "name": "x"})
    raise SystemExit("path outside the data root was accepted")
except HTTPException as exc:
    assert exc.status_code == 400 and "outside" in exc.detail
print("ingest: rejects paths outside the data root")

# --- a download with no media in it -------------------------------------------
junk = DATA / "torrents/complete/Junk Release"
junk.mkdir(parents=True)
(junk / "readme.nfo").write_text("nothing to see")
(junk / "sample.mkv").write_bytes(b"\x00" * 1024)
t.download_complete({"hash": "junk1", "path": str(junk), "category": "tv-sonarr", "name": "Junk"})
row = t.fetch_jobs("SELECT * FROM jobs")[0]
assert row["state"] == "failed" and "no media files" in row["error_detail"], row["error_detail"]
print("ingest:", row["error_detail"], "(recorded so reconcile stops re-offering it)")

# --- media with no English subtitles ------------------------------------------
release = DATA / "torrents/complete/No Subs Show S01E01"
release.mkdir(parents=True)
media = release / "No Subs Show - S01E01.mkv"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
                "-i", "testsrc=size=160x90:rate=5:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", str(media)], check=True)

IMPORTS = {"count": 0, "status": "completed"}


class FakeArr(t.ArrClient):
    def __init__(self, media_type):
        self.media_type, self.scan_command = media_type, "DownloadedEpisodesScan"

    def request_import(self, path, download_id):
        IMPORTS["count"] += 1
        return 5

    def command_status(self, command_id):
        return {"status": IMPORTS["status"]}


t.ArrClient = FakeArr

t.download_complete({"hash": "nosub1", "path": str(release), "category": "tv-sonarr", "name": "NoSubs"})
job = lambda: t.fetch_jobs("SELECT * FROM jobs WHERE download_id='NOSUB1'")[0]
t.pipeline_tick(); t.pipeline_tick()
row = job()
assert row["translation_state"] == "failed", row["translation_state"]
assert "no English subtitle" in row["error_detail"], row["error_detail"]
assert row["state"] == "transcoding", "the file must still be handed to Tdarr"
assert t.claim_translation_job() is None, "a failed translation must not be re-claimed"
print("no-subs: translation marked failed, file still handed to Tdarr, no retry loop")

# --- Tdarr leaves the subtitles embedded --------------------------------------
staged = list(Path(row["transcode_dir"]).iterdir())[0]
ass = BASE / "s.ass"
ass.write_text("[Script Info]\nScriptType: v4.00+\n\n[Events]\nFormat: Layer, Start, End, Style, "
               "Name, MarginL, MarginR, MarginV, Effect, Text\n"
               "Dialogue: 0,0:00:00.10,0:00:01.00,Default,,0,0,0,,Hi\n", encoding="utf-8")
out = staged.with_name("out.mkv")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(staged), "-i", str(ass),
                "-map", "0:v", "-map", "1:s", "-c:v", "libsvtav1", "-preset", "12", "-crf", "60",
                "-c:s", "ass", str(out)], check=True, stderr=subprocess.DEVNULL)
staged.unlink(); out.rename(staged)

t.pipeline_tick()
assert job()["state"] == "transcoding", "must keep waiting while subtitles are still embedded"
t.pipeline_tick()
assert job()["state"] == "transcoding", "must not converge on a file that still has subtitles"
assert IMPORTS["count"] == 0
print("un-stripped subs: job correctly refuses to converge")

t.CONFIG["transcode_timeout_hours"] = 0
t.pipeline_tick()
row = job()
assert row["state"] == "failed" and "embedded subtitle" in row["error_detail"], row["error_detail"]
assert media.exists(), "the seeding file must never be deleted on failure"
print("un-stripped subs:", row["error_detail"])

# --- import timeout with files left behind ------------------------------------
t.CONFIG["transcode_timeout_hours"] = 12
work_dir = DATA / "work" / t.job_key("STUCK1")
work_dir.mkdir(parents=True, exist_ok=True)
stuck_media = work_dir / "Stuck - S01E01.mkv"
stuck_media.write_bytes(b"\x00" * 2048)
now = time.time()
with t.db() as conn:
    conn.execute(
        "INSERT INTO jobs (download_id, media_type, source_path, work_dir, work_path, state, "
        "translation_state, import_command_id, import_deadline, created_at, updated_at) "
        "VALUES ('STUCK1','series',?,?,?,'importing','done',5,?,?,?)",
        (str(DATA / "torrents/complete/stuck.mkv"), str(work_dir), str(stuck_media), now - 1, now, now),
    )
t.pipeline_tick()
row = t.fetch_jobs("SELECT * FROM jobs WHERE download_id='STUCK1'")[0]
assert row["state"] == "failed" and "did not import" in row["error_detail"], row["error_detail"]
assert stuck_media.exists(), "files must be left in place for manual import, never deleted"
print("import timeout:", row["error_detail"])

# --- reconcile: first run adopts, then picks up missed completions ------------
recovered = DATA / "torrents/complete/Recovered Show S01E01"
recovered.mkdir(parents=True)
(recovered / "Recovered Show - S01E01.mkv").write_bytes(b"\x00" * 4096)

TORRENTS = [{
    "hash": "feed01", "name": "Recovered Show S01E01", "category": "tv-sonarr",
    "content_path": str(recovered), "completion_on": time.time(),
}]


class FakeQbit(t.QbitClient):
    def __init__(self):
        self.authenticated = True

    def completed_torrents(self):
        return TORRENTS


client = FakeQbit()
assert t.reconcile_once(client) == 0, "the first run must adopt existing torrents, not ingest them"
assert t.fetch_jobs("SELECT * FROM jobs WHERE download_id='FEED01'") == []
print("reconcile: first run adopts the existing seed list instead of stampeding")

t.meta_set("reconcile_watermark", str(time.time() - 3600))
assert t.reconcile_once(client) == 1, "a completion the database has never seen must be enqueued"
assert len(t.fetch_jobs("SELECT * FROM jobs WHERE download_id='FEED01'")) == 1
assert t.reconcile_once(client) == 0, "an already-known download must not be enqueued twice"
print("reconcile: missed completion picked up once, not repeatedly")

TORRENTS.append({"hash": "old01", "name": "Ancient", "category": "tv-sonarr",
                 "content_path": str(recovered), "completion_on": time.time() - 90 * 3600})
assert t.reconcile_once(client) == 0, "completions older than the cutoff must be ignored"
print("reconcile: stale completions stay ignored")

# --- /translate ---------------------------------------------------------------
t.call_gemini = lambda prompt: {
    key: "ÖVERSATT " + value for key, value in json.loads(prompt.split("```json")[1].split("```")[0]).items()
}
t.CONFIG["translation"]["delay_between_batches"] = 0


class Upload:
    def __init__(self, name, body):
        self.filename, self.file = name, io.BytesIO(body)


srt = b"1\n00:00:01,000 --> 00:00:02,000\nHello there\n\n2\n00:00:03,000 --> 00:00:04,000\nGoodbye\n"
response = t.translate_upload(Upload("Show.en.srt", srt))
assert response.status_code == 200
assert "ÖVERSATT" in response.body.decode("utf-8"), response.body[:200]
print("/translate: returned a translated subtitle for Bazarr's hook")

try:
    t.translate_upload(Upload("cover.jpg", b"nope")); raise SystemExit("bad format accepted")
except HTTPException as exc:
    assert exc.status_code == 400
print("/translate: rejects formats that are not subtitles")
assert not any(p.name.startswith("translate-") for p in (DATA / ".scratch").iterdir()), "temp dirs cleaned up"

# --- *arr confirmation webhook ------------------------------------------------
assert t.import_webhook({"eventType": "Test"})["status"] == "Test successful"
assert "ignored" in t.import_webhook({"eventType": "Download"})["status"]
print("import webhook: test events and payloads without a downloadId are harmless")

# --- cross-filesystem staging -------------------------------------------------
foreign = Path("/dev/shm/gatekeeper-xdev")
shutil.rmtree(foreign, ignore_errors=True)
foreign.mkdir(parents=True, exist_ok=True)
try:
    t.link_into_work(media, foreign)
    print("cross-fs: (same device here, skipped)")
except t.FatalJobError as exc:
    assert "different filesystems" in str(exc)
    print("cross-fs:", exc)

print("\nALL FAILURE-PATH CHECKS PASSED")
