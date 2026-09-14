"""End-to-end exercise of the gatekeeper with real ffmpeg media and stubbed Gemini/*arr/qBittorrent.

Covers a two-episode season pack, so the per-torrent grouping is exercised too.
"""
import json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BASE = Path(tempfile.mkdtemp(prefix="gatekeeper-e2e-"))

DATA = BASE / "data"
for sub in ("torrents/complete", "torrents/incomplete", "work", "transcode"):
    (DATA / sub).mkdir(parents=True)
(BASE / "library").mkdir()

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
    "min_media_bytes": 1}))

os.environ["AI_TRANSLATOR_SECRETS"] = str(BASE / "secrets.json")
os.environ["AI_TRANSLATOR_CONFIG"] = str(BASE / "config.json")
sys.path.insert(0, str(REPO))
import ai_translator as t

# ---- build a two-episode release with embedded English ASS ------------------

RELEASE = DATA / "torrents/complete/Test Show S01 1080p WEB-DL"
RELEASE.mkdir(parents=True)
EPISODES = [RELEASE / "Test Show - S01E01.mkv", RELEASE / "Test Show - S01E02.mkv"]

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

print("building a two-episode release with embedded ASS subtitles...")
for episode in EPISODES:
    subprocess.run(["ffmpeg", "-y", "-v", "error",
                    "-f", "lavfi", "-i", "testsrc=size=320x180:rate=10:duration=7",
                    "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo:d=7",
                    "-i", str(ASS), "-shortest",
                    "-map", "0:v", "-map", "1:a", "-map", "2:s",
                    "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-c:s", "ass",
                    "-metadata:s:s:0", "language=eng", str(episode)], check=True)
SOURCE_INODES = {e.name: e.stat().st_ino for e in EPISODES}
print("  release:", RELEASE.name, [e.name for e in EPISODES])

# ---- stub the external services ---------------------------------------------

SWEDISH = {
    "Good morning, captain.": "God morgon, kapten.",
    "We lost the signal an hour ago.": "Vi tappade signalen för en timme sedan.",
    "Whispering Don't move.": "Viskande Rör dig inte.",
}


def fake_gemini(prompt):
    targets = json.loads(prompt.split("```json")[1].split("```")[0])
    out = {}
    for key, masked in targets.items():
        pieces = re.split(r"(\[\[\d+\]\])", masked)
        plain = " ".join("".join(p for p in pieces if not p.startswith("[[")).split())
        translated = SWEDISH.get(plain, "ÖVERSATT: " + plain)
        words, used = translated.split(), 0
        slots = [i for i, p in enumerate(pieces) if not p.startswith("[[") and p.strip()]
        rebuilt = []
        for i, piece in enumerate(pieces):
            if piece.startswith("[["):
                rebuilt.append(piece)
            elif i in slots:
                share = len(words) if i == slots[-1] else max(1, len(words) // len(slots))
                chunk = words[used:] if i == slots[-1] else words[used:used + share]
                used += len(chunk)
                rebuilt.append((" " if piece.startswith(" ") else "") + " ".join(chunk))
            else:
                rebuilt.append(piece)
        out[key] = "".join(rebuilt)
    return out


t.call_gemini = fake_gemini
t.CONFIG["translation"]["delay_between_batches"] = 0

ARR = {"imports": 0, "library": BASE / "library" / "Test Show"}


class FakeArr(t.ArrClient):
    """Stands in for Sonarr: importMode=Move, so imported files leave the work folder."""

    def __init__(self, media_type):
        self.media_type = media_type
        self.scan_command = "DownloadedEpisodesScan"
        self.last = None

    def request_import(self, path, download_id):
        ARR["imports"] += 1
        ARR["library"].mkdir(parents=True, exist_ok=True)
        for child in sorted(Path(path).iterdir()):
            if child.is_file():
                shutil.move(str(child), str(ARR["library"] / child.name))
        self.last = {"path": str(path), "downloadClientId": download_id}
        ARR["last"] = self.last
        return 77

    def command_status(self, command_id):
        return {"id": command_id, "status": "completed"}


t.ArrClient = FakeArr

# ---- run the pipeline --------------------------------------------------------

t.init_db()
job = lambda: t.fetch_jobs("SELECT * FROM jobs ORDER BY id")


def tick(label):
    t.pipeline_tick()
    rows = job()
    print(f"  [{label}] " + " | ".join(f"{Path(r['source_path']).stem[-3:]}={r['state']}/{r['translation_state']}"
                                       for r in rows))
    return rows


print("\ningest:")
result = t.download_complete({
    "hash": "abc123def456",
    "path": str(RELEASE),
    "category": "tv-sonarr",
    "name": RELEASE.name,
})
print("  webhook ->", result)
assert result["files"] == 2 and result["new"] == 2
rows = job()
assert len(rows) == 2
assert {r["media_type"] for r in rows} == {"series"}
assert {r["download_id"] for r in rows} == {"ABC123DEF456"}, "hash is normalised to upper case"

print("\nstaging:")
tick("stability")
rows = tick("link + extract + hand to Tdarr")
assert all(r["state"] == "transcoding" for r in rows), [r["state"] for r in rows]

for row in rows:
    staged = list(Path(row["transcode_dir"]).iterdir())
    assert len(staged) == 1, staged
    name = Path(row["source_path"]).name
    assert staged[0].stat().st_ino == SOURCE_INODES[name], "the staged file must share the torrent's inode"
    assert Path(row["source_path"]).exists(), "the seeding file must survive staging"
    assert row["en_subtitle_path"] and Path(row["en_subtitle_path"]).exists()
    assert ".scratch" in row["en_subtitle_path"], "temp subtitles must not sit in the import folder"
print("  both episodes staged as hardlinks, English subtitles extracted")

print("\ntranslation:")
for _ in range(2):
    claimed = t.claim_translation_job()
    assert claimed is not None
    t.process_translation(claimed)
rows = job()
assert all(r["translation_state"] == "done" for r in rows), [r["translation_state"] for r in rows]
sv = Path(rows[0]["sv_subtitle_path"])
body = sv.read_text(encoding="utf-8")
print("  ---")
print(body.split("[Events]")[1].strip())
print("  ---")
style = next(l for l in body.splitlines() if l.startswith("Style: Default"))
assert ",3.5,1.5,2," in style, style
assert "&H00202020" in style, style
assert "PlayResX: 1920" in body
assert "{\\an8}" in body and "\\N" in body and "{\\i1}" in body
print("  styling, positioning and line breaks preserved")

print("\nwaiting for Tdarr:")
rows = tick("nothing ready")
assert ARR["imports"] == 0, "must not import while Tdarr still holds the files"


def simulate_tdarr(row):
    """AV1 with every subtitle stream stripped, replacing the file in place."""
    staged = list(Path(row["transcode_dir"]).iterdir())[0]
    out = staged.with_name("TdarrCacheFile-tmp.mkv")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(staged),
                    "-map", "0:v", "-map", "0:a", "-c:v", "libsvtav1", "-preset", "12",
                    "-crf", "50", "-c:a", "copy", "-sn", str(out)],
                   check=True, stderr=subprocess.DEVNULL)
    return staged, out


print("\nTdarr finishes episode 1 only:")
staged, out = simulate_tdarr(rows[0])
rows = tick("cache file ignored")
assert rows[0]["state"] == "transcoding", "an in-progress cache file must not be picked up"
staged.unlink(); out.rename(staged)
tick("size seen")
rows = tick("episode 1 verified")
assert rows[0]["state"] == "transcoded" and rows[1]["state"] == "transcoding"
assert ARR["imports"] == 0, "a partial season pack must not be imported"
print("  episode 1 ready, import correctly held back")

print("\nTdarr finishes episode 2:")
staged, out = simulate_tdarr(rows[1])
staged.unlink(); out.rename(staged)
tick("size seen")
rows = tick("episode 2 verified -> import requested")
assert ARR["imports"] == 1, ARR["imports"]
assert ARR["last"]["downloadClientId"] == "ABC123DEF456"
assert ARR["last"]["path"].endswith(t.job_key("ABC123DEF456"))

rows = tick("import confirmed")
assert all(r["state"] == "done" for r in rows), [(r["state"], r["error_detail"]) for r in rows]

# ---- what ended up in the library -------------------------------------------

delivered = sorted(p.name for p in ARR["library"].iterdir())
print("\nhanded to *arr for import:", delivered)
assert delivered == [
    "Test Show - S01E01.mkv", "Test Show - S01E01.sv.ass",
    "Test Show - S01E02.mkv", "Test Show - S01E02.sv.ass",
], delivered

for name in ("Test Show - S01E01.mkv", "Test Show - S01E02.mkv"):
    probe = t.ffprobe(ARR["library"] / name)
    assert t.is_av1(probe), f"{name} is not AV1"
    assert not t.streams_of_type(probe, "subtitle"), f"{name} still has embedded subtitles"

for episode in EPISODES:
    assert episode.exists(), "the seeding torrent must be untouched"
assert not any((DATA / "work").iterdir()), "work folder must be cleaned up"
assert not any((DATA / "transcode").iterdir()), "transcode folder must be cleaned up"
assert not any(p.name.endswith(".en.ass") for p in ARR["library"].iterdir()), "English temp sub must be gone"

print("\nALL CHECKS PASSED")
