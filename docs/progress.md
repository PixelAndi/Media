# Where we are

Living status of the overhaul. Update this at the end of each working session.

**Last updated:** 14 Sep 2026, end of session · branch `claude/magical-noether-halmjq`

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

## Pick up here

The live test. Nothing is broken — there has simply been nothing to process.

Evidence from the last session: the newest completed torrent finished at 11:04 local; the
gatekeeper started at 12:14 local. `/api/jobs` returns `[]` because no download has finished since
it came up. Everything else was proven working — service up, API key correct, and a POST from
*inside* the qBittorrent container returned `202 Accepted`.

So:

1. Request one episode of an ongoing show in Jellyseerr.
2. Watch it in **qBittorrent's Web UI** until it hits 100%. The gatekeeper does nothing at all
   before that, so `[]` during the download is correct.
   - Stalled at 0% → dead release, pick another.
   - Absent from qBittorrent → Sonarr never grabbed it; check Sonarr → Activity → Queue, then History.
3. Then `curl -s -H "X-Api-Key: <secret>" http://192.168.50.45:5000/api/jobs; echo` — a job should
   appear within ~5 s, and walk `new → transcoding → transcoded → importing → done`.

Full detail in `setup-guide.md` §Phase 4 step 5.

---

## Open items

**Mad God folder — do not delete.** Roughly 345 G under `/mnt/nvme-seed/arr-ingest/movies/`. It
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

## Standing rules

- No API keys or passwords go in this repo, even where they were legible in a screenshot.
- Develop, commit and push only to `claude/magical-noether-halmjq`.
- No pull request unless explicitly asked.
