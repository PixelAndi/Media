# Where we are

Living status of the overhaul. Update this at the end of each working session.

**Last updated:** 15 Sep 2026, end of session · branch `claude/magical-noether-halmjq`

---

## The one-line summary

Phases 1 and 3 are done and the gatekeeper is deployed and healthy. Phase 4 is configured but
**the live test has not actually run yet** — not because anything failed, but because no download
has finished since the gatekeeper started. That is the next thing to do.

---

## Phase status

| Phase | State | Notes |
|---|---|---|
| Prep | done | Snapshots taken, five checks run. Findings below. |
| 0 — quick wins | mostly | Confirm the Radarr API key was actually rotated. Tdarr *Hold Files* was turned on, and Phase 4 turns it back off. |
| 0.5 — free the space | **blocked** | 392 G still in `arr-ingest`. Blocked on the Mad God folder (below). |
| 1 — storage reshape | **done** | `nvme-seed/torrents/complete` → `nvme-seed/data`. Hardlinks verified: `2 links`. Seeding torrents undisturbed. |
| 2 — one path everywhere | partial | `/data` mounts added. **Deliberately not finished:** `/nvme` mount removal and `zfs destroy nvme-seed/arr-ingest` must wait until arr-ingest is drained. |
| 3 — install the gatekeeper | **done** | Running as TrueNAS app `gatekeeper`. `/health` returns ok. |
| 4 — turn the pipeline on | configured, untested | Steps 1–4 done. **Step 5, the live test, is where we stopped.** |
| 5 — old library migration | not started | 2.26 TiB in `/mnt/Andi/Media/Library`. |

---

## First live test — what it found (15 Sep)

Four jobs ran. The pipeline's own stages all worked: staging, hardlinking, subtitle extraction and
the qBittorrent hook. Everything that failed was downstream config or a design gap:

1. **Gemini model name wrong.** `gemini-2.5-flash` 404s for this API key, so every translation
   failed three times and gave up. Fix: list the key's real models and set `translation.model` in
   `config.json`. The code now says this outright instead of logging a bare 404.
2. **Tdarr processed nothing for 12 hours.** Container `ix-tdarr-tdarr-1` is up and healthy, so the
   question is whether it can see `/data/transcode` (mount) or is holding the files (Hold Files
   After Scanning, turned on in Phase 0 and never turned back off in Phase 4 step 3).
3. **Already-AV1 releases deadlocked** — fixed in code. Tdarr skips AV1 files, which skipped the
   subtitle strip too, so the job waited out the whole timeout with 22 embedded streams. The
   gatekeeper now remuxes the subtitles away itself (`-map 0 -map -0:s -c copy`, seconds, no
   re-encode) and bypasses Tdarr. Verified the hardlinked seed keeps its own inode and subtitles.
4. **A failed job used to be a dead end** — `INSERT OR IGNORE` on `source_path` meant re-firing the
   webhook did nothing. Added `POST /api/jobs/{id}/retry` and `jobs.py --retry-failed`.

Not a bug: `[Tonkatsu Fansub]` reported "no English subtitle track found" and was right — its only
subtitle track is tagged `ita`. Bazarr is the designed fallback for releases like that.

## Pick up here — an import is running overnight

**A Sonarr ManualImport of 268 files is in flight** (command 130040, queued ~end of session). It
moves the misfiled content out of `/mnt/Media/library/movies/Mad God (2021) [imdbid-tt15090124]/`
into the correct series folders. It runs inside Sonarr, so shells and sessions closing do not
affect it. Do **not** queue another import until it finishes — that would double-queue files
already in flight.

First thing next session:

```sh
find "/mnt/Media/library/movies/Mad God (2021) [imdbid-tt15090124]" -name '*.mkv' | wc -l
zfs list -o name,used,avail Media nvme-seed
```

The count should have walked down from 341 toward **20**. If it is still high, check
**Sonarr → Activity → History** — the imports are slow, several hundred GB of moves.

To re-establish the working shell (these do not survive a logout):

```sh
mis() { python3 /mnt/fast-pool/gatekeeper/manual_import.py --app sonarr "$@"; }
mir() { python3 /mnt/fast-pool/gatekeeper/manual_import.py --app radarr "$@"; }
L="/library/movies/Mad God (2021) [imdbid-tt15090124]"
```

Quiet probe: `mis --folder "$L" | grep -E '^(MATCHED|NOT IMPORT)'`

### Then the 20 stragglers

| What | Count | Fix |
|---|---|---|
| `[AnimeRG]` Ghibli films | 11 | Not in Radarr at all. Add each in Radarr → Add New, then `mir` picks them up. |
| Loose movies — Hokum, The Odyssey, SAO Ordinal Scale, Spirited Away, `mad.god…mkv` | 5 | Move to a neutral folder (`/mnt/Media/library/_sortme`) and run `mir --folder "/library/_sortme"`. **Never** point Radarr at the Mad God folder — see the trap below. |
| Re:ZERO S04E15, S04E16 | 2 | Sonarr already holds better copies. Genuinely redundant, safe to delete. |
| `Reacher S03E04 [bit][]-d3g.mkv` | 1 | Malformed name, Sonarr cannot tell if it is a sample. Rename sanely and re-run. |
| `S03ED-Kotoba ni Dekinai` | 1 | An ending-theme clip, not an episode. |

### Still outstanding from earlier

- **`arr-ingest` is down to 89.6 G** from 392 G. What remains: Sword Art Online S02 (38 G, matches
  cleanly), the `TV/` tree's Gaki (~93 files, needs `--id 48`) and FREEZE (10 files, `--id 54`,
  already verified correct), and `anime/tv` (9.9 G, 10 files, matches cleanly). Then
  `zfs destroy -r nvme-seed/arr-ingest`.
- **Tdarr still consumes nothing** from `/data/transcode`. Container healthy, `/data` mounted, queue
  readable — so it is a setting in Tdarr's own UI. Prime suspect remains *Hold Files After Scanning*,
  turned on in Phase 0 and never turned back off.
- **Gemini** works now (`gemini-flash-latest`, credits added). A 503 seen once was Google's capacity.
- **Bazarr is writing 238-byte junk subtitles.** Its post-processing curl uses `-o "$dest"` and saves
  the body whatever the status, so every failed translation left a fake `.sv.srt` — visible next to
  the 2001 file. Add `--fail` to that command and sweep up the ones already written.

## Tools now on the server (`/mnt/fast-pool/gatekeeper/`)

All read-only unless told otherwise; all stdlib-only; all read credentials from `secrets.json`.

| Tool | What it does |
|---|---|
| `doctor.py` | One command, checks every moving part — secrets, config, *arr and qBittorrent reachability, a real Gemini call, container mounts, a live hardlink probe, Tdarr throughput, pool space, failed jobs — with the fix printed beside each failure. |
| `jobs.py` | Gatekeeper job list with `error_detail`; `--retry-failed`, `--retranslate-all`. |
| `reclaim.py` | Surveys a folder: TRACKED / ORPHAN / DUPLICATE, hardlink counts, and identical files under different names. |
| `manual_import.py` | Drives Sonarr/Radarr manual import from the shell. `--find`, `--id`, `--import`, `--limit`. |

Pull them with the commit hash rather than the branch name — raw.githubusercontent caches the
branch and will serve a stale copy:
`curl -fsSL "https://raw.githubusercontent.com/PixelAndi/Media/<sha>/tools/<file>" -o /mnt/fast-pool/gatekeeper/<file>`

## Open items

**Mad God folder — surveyed 15 Sep, it is a relocation job, not a delete job.**

`tools/reclaim.py` settled what is in there. Findings:

- **Every Sonarr and Radarr entry points at `/library`. None points into `arr-ingest`**, so all
  356 G there is unreferenced — but unreferenced is not disposable. Most of it is the only copy.
- **Root cause:** Radarr's *Mad God* has its path set to `/library/dest_media`, an empty 25 K stub
  left over from the old Tdarr transfer. Folders named `Mad God (2021) [imdbid-tt15090124]` in
  both `arr-ingest/movies/` and `library/movies/` became dumping grounds for an unrelated transfer.
- **The 74 G `2001.A.Space.Odyssey` REMUX is the only copy** — Radarr's 2001 folder is empty.
  Importing that one file is a fifth of the problem and takes nvme-seed from 66 G to ~140 G free.
- Also stranded and not in the library: Star Trek SNW S04 (2160p), PLUR1BUS S01 (2160p), Star Trek
  TNG, Downtown no Gaki no Tsukai, The Creep Tapes S02, and the missing Mushoku Tensei S03E09/10/12.
- Sword Art Online S02 is split: `arr-ingest` has E01–E24, `library/movies/Mad God …/` has ~E09–E23.
  Neither is complete; the ingest copy is the fuller one.

**The fix is Manual Import with Move**, not deletion — it relocates the file *and* records it in the
database, and moving to the Media pool (6.4 T free) drains nvme-seed at the same time. Sonarr and
Radarr both reach the area at `/nvme/arr-ingest` (host `/mnt/nvme-seed`). Order: 2001 first, then
the `TV/` tree in Sonarr, then the anime paths, then SAO. One at a time — parallel copies are what
make SMR pathological. Whatever Manual Import cannot match is the real junk; delete that, then
`zfs destroy nvme-seed/arr-ingest`.

**Old note, superseded:** Roughly 345 G under `/mnt/nvme-seed/arr-ingest/movies/`. It
looked like junk; the screenshots proved it is misplaced real media, including the episodes missing
from `arr-ingest/tv`. Plan:

1. `du -sh /mnt/nvme-seed/arr-ingest/movies/*/*` to get the three subfolder sizes.
2. In qBittorrent, check the **Errored** and **Missing Files** filters — some of those folders may
   still be seeding and must not be moved out from under a live torrent.
3. Move the `TV/*` contents back to `/mnt/nvme-seed/arr-ingest/tv/`, rescan Sonarr.
4. Return any still-seeding release folders to `/complete`.

This blocks Phase 0.5, which in turn blocks finishing Phase 2.

**`zfs destroy nvme-seed/tdarr-cache` → "dataset is busy".** Something still holds it. Find the
holder (a container still mounting it, or a snapshot) before retrying.

**Fake release seeding.** `Star Trek Strange New Worlds S04E08 1080p HEVC x265-MeGusta.zipx`,
1.05 GB, category `tv-sonarr`, completed 7 Sep. `.zipx` is not a video container — this is the
standard fake-release pattern. Do not open it; delete it with its data.

**Deferred by choice:** retire CT 101; CrowdSec + GeoIP in NPMplus (config-file work, not UI
toggles — its own small project); a batch mode for translating the back catalogue (offered, never
requested).

---

## Facts about this server worth not re-deriving

- TrueNAS at **192.168.50.45**. Shell logs in as `truenas_admin`; run `sudo -i` first — every
  command in the guide needs root.
- Apps run as **568:568** (the TrueNAS apps user). Sonarr and Radarr are official apps, so the two
  custom apps (qBittorrent, gatekeeper) were moved onto 568 rather than the other way round.
- Ports: Sonarr `:30113`, Radarr `:30025`, qBittorrent `:8080`, gatekeeper `:5000`.
- **Container names.** Official TrueNAS apps are `ix-<app>-<service>-1`; the two custom apps keep
  plain names. So it is `docker exec qbittorrent …` and `docker exec gatekeeper …`, but
  `docker exec ix-tdarr-tdarr-1 …`, `ix-sonarr-sonarr-1`, `ix-radarr-radarr-1`,
  `ix-bazarr-bazarr-1`, `ix-prowlarr-prowlarr-1`, `ix-seerr-seerr-1`. Guessing `tdarr` wastes a
  round trip — `docker ps --format '{{.Names}}'` settles it.
- **The TrueNAS web shell mangles multi-line pastes** — lines overwrite each other and Python gets
  garbage. Give single-line commands, or put the script in the repo and curl it down. That is why
  `tools/jobs.py` exists.
- Gatekeeper code lives at `/mnt/fast-pool/gatekeeper` on the host, mounted as `/config`. The image
  is plain `python:3.12-slim` — Docker cannot clone a repo, the code arrives via the volume.
- Work pool is `nvme-seed/data` → `/mnt/nvme-seed/data`, mounted as `/data` everywhere. Library is
  `/mnt/Media/library` → `/library`. One dataset for work-in-progress, because hardlinks cannot
  cross datasets.
- **The gatekeeper's log prints UTC, 7 hours ahead of local.** A line stamped `19:14` happened at
  `12:14`. This has already caused one false alarm.
- qBittorrent's WebUI bypasses auth for the local subnet and answers `204` with an empty body to a
  login — that is success, not refusal. The code handles this; it cost us a wrong diagnosis once.
- The reconcile loop polls qBittorrent every 5 minutes and catches anything the completion hook
  missed, but only for torrents completing **after** its first-run watermark (set 14 Sep 19:14 UTC).
  Anything older was adopted as existing seed and will never enter the pipeline.

## Traps found the hard way

- **Radarr matches on the enclosing folder name, not the filenames.** Probing the Mad God folder
  with Radarr matched all 403 files — South Park, The Expanse, Squid Game, everything — to the
  single movie *Mad God*. Importing that would have filed them all as one film. `manual_import.py`
  now refuses when more than three files map to one movie. **Sonarr does not have this problem**; it
  reads filenames, which is why `mis --folder "$L"` sorts the same tree correctly.
- **A ZFS snapshot pins deleted blocks.** Files moved out of `arr-ingest` freed no space until
  `zfs destroy -r nvme-seed/arr-ingest@before-overhaul`. `nvme-seed/data@before-overhaul` is
  deliberately kept — that is the rollback for the storage reshape.
- **Imports are queued, not synchronous.** The tool returns as soon as Sonarr accepts the command,
  so a probe run straight afterwards still lists files that are about to move. Wait between rounds.
- **`--id` makes the tool trust you about the series.** Point it at the wrong folder and it will
  happily file one show's episodes as another. Keep each run scoped to one show's own directory.

## Standing rules

- No API keys or passwords go in this repo, even where they were legible in a screenshot.
- Develop, commit and push only to `claude/magical-noether-halmjq`.
- No pull request unless explicitly asked.
