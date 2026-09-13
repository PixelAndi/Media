"""End-to-end exercise of the pipeline with real ffmpeg media and stubbed Gemini/*arr."""
import json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = Path(tempfile.mkdtemp(prefix="ai-translator-e2e-"))
os.environ["AI_TRANSLATOR_SECRETS"] = str(BASE / "secrets.json")
os.environ["AI_TRANSLATOR_CONFIG"] = str(BASE / "config.json")

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
    "anime_tv_target_root": str(BASE / "library/anime/tv")}))

sys.path.insert(0, str(REPO))
import ai_translator as t

SHOW_DIR = BASE / "ingest" / "Test Show"
SHOW_DIR.mkdir(parents=True)
MEDIA = SHOW_DIR / "Test Show - S01E01.mkv"

ASS = BASE / "src.ass"
ASS.write_text("""[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial,60,&H00FFFFFF,&H000000FF,&H00202020,&H80000000,0,0,0,0,100,100,0,0,1,3.5,1.5,2,20,20,40,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
Dialogue: 0,0:00:00.50,0:00:02.00,Default,,0,0,0,,{\\an8}Good morning, captain.
Dialogue: 0,0:00:02.10,0:00:04.00,Default,,0,0,0,,We lost the signal\\Nan hour ago.
Dialogue: 0,0:00:04.10,0:00:06.00,Default,,0,0,0,,{\\i1}Whispering{\\i0} Don't move.
""", encoding="utf-8")

print("building source media with embedded ASS subtitles...")
subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10:duration=7",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=7", "-i", str(ASS), "-shortest",
                "-map", "0:v", "-map", "1:a", "-map", "2:s", "-c:v", "libx264", "-preset", "ultrafast",
                "-c:a", "aac", "-c:s", "ass", "-metadata:s:s:0", "language=eng", str(MEDIA)], check=True)
print(f"  source: {MEDIA.name} ({MEDIA.stat().st_size} bytes)")

# ---- stub out the two external services -------------------------------------
GEMINI_CALLS = []

def fake_gemini(prompt):
    GEMINI_CALLS.append(prompt)
    targets = json.loads(prompt.split("```json")[1].split("```")[0])
    swedish = {
        "Good morning, captain.": "God morgon, kapten.",
        "We lost the signal an hour ago.": "Vi tappade signalen för en timme sedan.",
        "Whispering Don't move.": "Viskande Rör dig inte.",
    }
    import re as _re
    out = {}
    for key, masked in targets.items():
        # translate only the text between placeholders, leaving tokens exactly where they are
        pieces = _re.split(r"(\[\[\d+\]\])", masked)
        plain = " ".join("".join(p for p in pieces if not p.startswith("[[")).split())
        translated = swedish.get(plain, "ÖVERSATT: " + plain)
        words = translated.split()
        rebuilt, used = [], 0
        text_slots = [i for i, p in enumerate(pieces) if not p.startswith("[[") and p.strip()]
        for i, piece in enumerate(pieces):
            if piece.startswith("[["):
                rebuilt.append(piece)
            elif i in text_slots:
                share = len(words) if i == text_slots[-1] else max(1, len(words) // len(text_slots))
                chunk = words[used:used + share] if i != text_slots[-1] else words[used:]
                used += len(chunk)
                rebuilt.append((" " if piece.startswith(" ") else "") + " ".join(chunk))
            else:
                rebuilt.append(piece)
        out[key] = "".join(rebuilt)
    return out

t.call_gemini = fake_gemini
t.CONFIG["translation"]["delay_between_batches"] = 0
t.CONFIG["stability_checks"] = 1

ARR_STATE = {"root": str(BASE / "ingest"), "path": str(SHOW_DIR), "rescans": 0, "moved": False}

class FakeArr(t.ArrClient):
    def __init__(self, media_type):
        self.media_type, self.item_endpoint = media_type, "series"
        self.file_endpoint, self.id_field = "episodefile", "seriesId"

    def get_item(self, item_id):
        return {"id": item_id, "path": ARR_STATE["path"], "rootFolderPath": ARR_STATE["root"]}

    def rescan(self, item_id):
        ARR_STATE["rescans"] += 1
        return 42

    def command_status(self, command_id):
        return {"id": command_id, "status": "completed"}

    def move_to_root(self, item, target_root):
        folder = os.path.basename(item["path"].rstrip("/"))
        destination = Path(target_root) / folder
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(item["path"], destination)          # *arr moves the whole folder
        ARR_STATE.update(root=target_root, path=str(destination), moved=True)

    def list_files(self, item_id):
        files = []
        for child in Path(ARR_STATE["path"]).iterdir():
            if child.suffix.lower() in t.MEDIA_EXTENSIONS:
                files.append({"path": str(child), "size": child.stat().st_size})
        return files

t.ArrClient = FakeArr

# ---- run the pipeline --------------------------------------------------------
t.init_db()
payload = {
    "eventType": "Download",
    "series": {"id": 7, "path": str(SHOW_DIR)},
    "episodeFile": {"id": 1, "path": str(MEDIA)},
}
print("webhook ->", t.import_webhook(payload))

def job():
    return t.fetch_jobs("SELECT * FROM jobs")[0]

def tick(label):
    t.pipeline_tick()
    row = job()
    print(f"  [{label}] state={row['state']} translation={row['translation_state']} err={row['error_detail']}")
    return row

print("\npipeline:")
tick("stability")
row = tick("extract+stage")
assert row["state"] == "staged", row["state"]
staged = list(Path(row["staging_dir"]).iterdir())
print("  staged for tdarr:", [p.name for p in staged])
assert MEDIA.exists(), "ingest file must survive staging"
assert MEDIA.stat().st_ino == staged[0].stat().st_ino, "staging must be a hardlink"

print("\ntranslation:")
claimed = t.claim_translation_job()
t.process_translation(claimed)
row = job()
print("  translation_state:", row["translation_state"], "untranslated:", row["untranslated_lines"])
sv = Path(row["sv_subtitle_path"])
print("  swedish file:", sv.name)
print("  ---")
print(sv.read_text(encoding="utf-8").split("[Events]")[1].strip())
print("  ---")
styles = sv.read_text(encoding="utf-8")
style_line = next(l for l in styles.splitlines() if l.startswith("Style: Default"))
print("  style:", style_line)
assert ",3.5,1.5,2," in style_line, f"outline/shadow/alignment must survive: {style_line}"
assert "&H00202020" in style_line, f"outline colour must survive: {style_line}"
assert "PlayResX: 1920" in styles, "resolution must survive"
assert "{\\an8}" in styles, "position tag must survive"
assert "\\N" in styles, "line break must survive"
assert "{\\i1}" in styles, "italics must survive"

print("\ntdarr is working... (nothing should advance)")
row = tick("waiting")
assert row["state"] == "staged"

print("\nsimulating tdarr: AV1, subtitles stripped")
staging = Path(job()["staging_dir"])
source_link = staging / MEDIA.name
out = staging / "TdarrCacheFile-tmp.mkv"
subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(source_link), "-map", "0:v", "-map", "0:a",
                "-c:v", "libsvtav1", "-preset", "12", "-crf", "50", "-c:a", "copy", "-sn", str(out)], check=True)
row = tick("cache file ignored")
assert row["state"] == "staged", "Tdarr's in-progress cache file must not be picked up"
out.rename(staging / MEDIA.name)
print("  tdarr output in place:", (staging / MEDIA.name).stat().st_size, "bytes")

tick("size seen")
row = tick("verify+finalize")
print("  rescans:", ARR_STATE["rescans"], "moved:", ARR_STATE["moved"])
row = tick("after move")
for _ in range(3):
    if job()["state"] in (t.STATE_DONE, t.STATE_FAILED):
        break
    row = tick("settle")

final = job()
print("\nfinal state:", final["state"], "| error:", final["error_detail"])
print("final media:", final["current_path"])
print("final subs :", final["sv_subtitle_path"])
assert final["state"] == t.STATE_DONE, final["error_detail"]
media_path, sub_path = Path(final["current_path"]), Path(final["sv_subtitle_path"])
assert media_path.exists() and sub_path.exists()
assert str(media_path).startswith(str(BASE / "library" / "tv")), media_path
assert sub_path.parent == media_path.parent, "sidecar must sit next to the media"
assert sub_path.stem == media_path.stem + ".sv", sub_path.name
probe = t.ffprobe(media_path)
assert t.is_av1(probe) and not t.streams_of_type(probe, "subtitle")
assert not list((BASE / "tdarr").iterdir()), "staging dir must be cleaned up"
assert not list((BASE / "work").iterdir()), "work dir must be cleaned up"
assert not any(p.name.endswith(".en.ass") for p in media_path.parent.iterdir()), "English temp sub must be gone"
print("\ncontents of final folder:", sorted(p.name for p in media_path.parent.iterdir()))
print("\nALL CHECKS PASSED")
