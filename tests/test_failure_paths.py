"""Failure-path checks: auth, validation, missing subs, un-stripped subs, timeouts, migration."""
import json, os, shutil, sqlite3, subprocess, sys, tempfile, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = Path(tempfile.mkdtemp(prefix="ai-translator-failures-"))
for sub in ("ingest", "library", "tdarr", "work"):
    (BASE / sub).mkdir(parents=True)
(BASE / "secrets.json").write_text(json.dumps({
    "SONARR_URL": "http://localhost:8989", "SONARR_API_KEY": "k1",
    "RADARR_URL": "http://localhost:7878", "RADARR_API_KEY": "k2",
    "GEMINI_API_KEY": "g", "WEBHOOK_SECRET": "s3cret"}))
(BASE / "config.json").write_text(json.dumps({
    "db_file": str(BASE / "state.db"), "work_dir": str(BASE / "work"),
    "tdarr_staging_root": str(BASE / "tdarr"),
    "media_roots": [str(BASE / "ingest"), str(BASE / "library")],
    "tv_target_root": str(BASE / "library/tv"), "movie_target_root": str(BASE / "library/movies"),
    "anime_tv_target_root": str(BASE / "library/anime/tv"),
    "stability_checks": 1, "transcode_timeout_hours": 0.0001, "move_timeout_minutes": 0.0001}))

# --- legacy schema must be migrated out of the way, not crash -----------------
legacy = sqlite3.connect(BASE / "state.db")
legacy.execute("CREATE TABLE jobs (path TEXT PRIMARY KEY, media_type TEXT, item_id INTEGER)")
legacy.execute("INSERT INTO jobs VALUES ('/old/file.mkv','series',1)")
legacy.commit(); legacy.close()

os.environ["AI_TRANSLATOR_SECRETS"] = str(BASE / "secrets.json")
os.environ["AI_TRANSLATOR_CONFIG"] = str(BASE / "config.json")
sys.path.insert(0, str(REPO))
import ai_translator as t
from fastapi import HTTPException

t.init_db()
cols = {r["name"] for r in t.fetch_jobs("SELECT name FROM pragma_table_info('jobs')")}
assert "source_path" in cols and "state" in cols
assert t.fetch_jobs("SELECT * FROM jobs_legacy")[0]["path"] == "/old/file.mkv"
print("migration: legacy table preserved as jobs_legacy, new schema created")

# --- webhook auth + validation ------------------------------------------------
for bad in ("", "wrong", "s3cre"):
    try:
        t.require_secret(bad); raise SystemExit(f"secret {bad!r} was accepted")
    except HTTPException as exc:
        assert exc.status_code == 401
t.require_secret("s3cret")
print("auth: rejects empty/wrong secrets, accepts the right one")

assert t.import_webhook({"eventType": "Test"})["status"] == "Test successful"
assert "ignored" in t.import_webhook({"eventType": "Grab", "series": {"id": 1}})["status"]
assert "ignored" in t.import_webhook({"eventType": "Health"})["status"]
assert t.fetch_jobs("SELECT * FROM jobs") == []
print("webhook: Test/Grab/Health events create no jobs")

try:
    t.import_webhook({"eventType": "Download", "series": {"id": 1, "path": "/etc"},
                      "episodeFile": {"id": 1, "path": "/etc/passwd"}})
    raise SystemExit("path outside media roots was accepted")
except HTTPException as exc:
    assert exc.status_code == 400 and "outside" in exc.detail
print("webhook: rejects paths outside the configured media roots")

try:
    t.import_webhook({"eventType": "Download", "series": {"path": "/x"}, "episodeFile": {"path": "/x/a.mkv"}})
    raise SystemExit("missing id accepted")
except HTTPException as exc:
    assert exc.status_code == 400
print("webhook: rejects payloads without a series/movie id")

# --- media with no English subtitles ------------------------------------------
show = BASE / "ingest" / "No Subs Show"
show.mkdir(parents=True)
media = show / "No Subs Show - S01E01.mkv"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x90:rate=5:duration=2",
                "-c:v", "libx264", "-preset", "ultrafast", str(media)], check=True)

ARR = {"root": str(BASE / "ingest"), "path": str(show)}
class FakeArr(t.ArrClient):
    def __init__(self, media_type):
        self.media_type, self.item_endpoint = media_type, "series"
        self.file_endpoint, self.id_field = "episodefile", "seriesId"
    def get_item(self, i): return {"id": i, "path": ARR["path"], "rootFolderPath": ARR["root"]}
    def rescan(self, i): return 1
    def command_status(self, c): return {"status": "completed"}
    def move_to_root(self, item, root): pass          # simulate *arr never moving it
    def list_files(self, i):
        return [{"path": str(p), "size": p.stat().st_size}
                for p in Path(ARR["path"]).iterdir() if p.suffix in t.MEDIA_EXTENSIONS]
t.ArrClient = FakeArr

t.import_webhook({"eventType": "Download", "series": {"id": 5, "path": str(show)},
                  "episodeFile": {"id": 1, "path": str(media)}})
job = lambda: t.fetch_jobs("SELECT * FROM jobs")[0]
t.pipeline_tick(); t.pipeline_tick()
row = job()
assert row["translation_state"] == t.TR_FAILED, row["translation_state"]
assert "no English subtitle" in row["error_detail"], row["error_detail"]
assert row["state"] == t.STATE_STAGED, "file must still go to Tdarr"
assert t.claim_translation_job() is None, "a failed translation must not be claimed"
print("no-subs: translation marked failed, media still handed to Tdarr, no retry loop")

# --- Tdarr leaves subtitles embedded -> must not converge, then time out -------
staging = Path(row["staging_dir"])
src = staging / media.name
out = staging / "out.mkv"
ass = BASE / "s.ass"
ass.write_text("[Script Info]\nScriptType: v4.00+\n\n[Events]\nFormat: Layer, Start, End, Style, Name, "
               "MarginL, MarginR, MarginV, Effect, Text\nDialogue: 0,0:00:00.10,0:00:01.00,Default,,0,0,0,,Hi\n",
               encoding="utf-8")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-i", str(ass), "-map", "0:v", "-map", "1:s",
                "-c:v", "libsvtav1", "-preset", "12", "-crf", "60", "-c:s", "ass", str(out)],
               check=True, stderr=subprocess.DEVNULL)
src.unlink(); out.rename(src)
t.pipeline_tick()                       # records the size
row = job()
assert row["state"] == t.STATE_STAGED, "must keep waiting while subtitles are still embedded"
t.pipeline_tick()                       # verifies, still inside the transcode window
assert job()["state"] == t.STATE_STAGED, "must not converge on a file that still has subtitles"
print("un-stripped subs: job correctly refuses to converge")
t.CONFIG["transcode_timeout_hours"] = 0  # now let the transcode window expire
t.pipeline_tick()
row = job()
assert row["state"] == t.STATE_FAILED, row["state"]
assert "embedded subtitle" in row["error_detail"], row["error_detail"]
assert media.exists(), "the ingest file must never be deleted on failure"
print("un-stripped subs:", row["error_detail"])

# --- move timeout must fail loudly and delete nothing --------------------------
t.fetch_jobs("SELECT 1")
with t.db() as conn:
    conn.execute("UPDATE jobs SET state=?, move_deadline=?, error_detail=NULL, pipeline_attempts=0",
                 (t.STATE_MOVE_PENDING, time.time() - 1))
t.pipeline_tick()
row = job()
assert row["state"] == t.STATE_FAILED and "did not move" in row["error_detail"], row["error_detail"]
assert media.exists(), "media must survive a move timeout"
print("move timeout:", row["error_detail"])

# --- re-import resets a finished/failed job ------------------------------------
t.import_webhook({"eventType": "Download", "series": {"id": 9, "path": str(show)},
                  "episodeFile": {"id": 1, "path": str(media)}})
row = job()
assert row["state"] == t.STATE_NEW and row["item_id"] == 9 and row["error_detail"] is None
assert len(t.fetch_jobs("SELECT * FROM jobs")) == 1, "re-import must update, not duplicate"
print("re-import: job reset to new with refreshed item_id")

# --- staging across filesystems must fail with a clear message ----------------
t.STAGING_ROOT = Path("/dev/shm/tdarr-xdev")
shutil.rmtree(t.STAGING_ROOT, ignore_errors=True)
t.STAGING_ROOT.mkdir(parents=True, exist_ok=True)
try:
    t.stage_for_tdarr(job())
    print("cross-fs: (same device here, skipped)")
except t.FatalJobError as exc:
    assert "different filesystems" in str(exc)
    print("cross-fs:", exc)

print("\nALL FAILURE-PATH CHECKS PASSED")
