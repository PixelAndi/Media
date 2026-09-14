# Target architecture

A redesign of the media pipeline, replacing the current arrangement rather than patching it.
Written against the live setup recorded in `homelab-setup.md`.

**Status: §2 implemented.** `ai_translator.py` now runs this pipeline — the qBittorrent
completion trigger, per-torrent grouping, the Tdarr handoff and verification, the scan-based
import, the reconcile loop against qBittorrent's API, and a `/translate` endpoint for Bazarr's
hook. Covered by both suites in `tests/`. Phases 0, 1 and 3 of §6 are configuration changes on
the server and are still to be done.

Goals it is built to meet:
- Fully automated from a Jellyseerr request to a finished file in Jellyfin.
- One write to the library pool, ever — the file is final before it is written.
- Simple enough to hold in your head: one path layout, one direction of travel.
- The library pool can be swapped later without touching a single application setting.

---

## 1. The four principles

**1. Every app sees the same paths.** `/data` for work in progress, `/library` for the finished
library. No exceptions. Every path mapping, remote path mapping and namespace translation in the
current setup exists only because this rule was broken.

**2. One dataset for work in progress.** Hardlinks cannot cross ZFS datasets. Downloads, staging
and the transcode queue must be *directories inside one dataset*, not datasets of their own.

**3. Processing happens before the import, never after.** The *arr import is the single write to
the library pool, and by then the file is AV1 with its Swedish subtitle beside it.

**4. One root folder per content type.** Sonarr and Radarr each point at exactly one library
root. Nothing is ever moved between roots.

---

## 2. The pipeline

```
Jellyseerr request
      |
Sonarr / Radarr  ──grab──▶  qBittorrent ──▶ /data/torrents/complete/<category>/
                                                     |
                                        "run on torrent finished" webhook
                                                     ▼
                                              ┌─────────────┐
                                              │  gatekeeper │
                                              └─────────────┘
                                                     |
        1. hardlink media into /data/work/<job>/        (instant, no extra space,
                                                         torrent keeps seeding)
        2. extract the English subtitle track
        3. move the video to /data/transcode/<job>/   ──▶ Tdarr  (AV1, subtitles stripped)
        4. translate EN → SV, write <name>.sv.ass beside the video
        5. verify Tdarr's output: AV1, zero subtitle streams, duration matches
        6. move the finished video back to /data/work/<job>/
                                                     |
        7. tell *arr to import that folder:  DownloadedEpisodesScan / DownloadedMoviesScan
           { path: /data/work/<job>, downloadClientId: <hash>, importMode: "Move" }
                                                     ▼
                              Sonarr / Radarr move video + .sv sidecar into /library/...
                                        ◀── THE ONLY WRITE TO THE LIBRARY POOL ──▶
                                                     |
        8. *arr "On Import" webhook confirms → gatekeeper cleans up /data/work/<job>
                                                     ▼
                           Jellyfin picks it up (real time monitoring, already enabled)
```

Tdarr physically cannot see the file until step 3, so it can never strip the English track
before it has been extracted. That race is designed out, not timed around.

**Self-healing.** The gatekeeper also polls qBittorrent's API for completed torrents in the
`tv-sonarr` / `radarr` categories and compares them against its own job database. Anything
completed but unknown gets enqueued. A missed webhook, a restart mid-job or an hour of downtime
costs nothing — the queue drains itself when the service comes back. This is what makes
"fully automated" true rather than aspirational.

---

## 3. Storage layout

### Host (TrueNAS)

```
/mnt/nvme-seed/data/          ← ONE dataset. Everything inside is plain directories.
        torrents/complete/
        torrents/incomplete/
        work/                 ← gatekeeper staging
        transcode/            ← Tdarr's watch folder
        cache/                ← Tdarr transcode cache

/mnt/Media/library/           ← the library, on whichever pool currently holds it
        tv/  movies/  anime/tv/  anime/movies/
```

The single `data` dataset is the important part. Today `arr-ingest`, `tdarr-cache`,
`torrents/complete` and `torrents/incomplete` are four separate ZFS datasets, and **hardlinks
cannot span ZFS datasets** — so Sonarr and Radarr's "Use Hardlinks instead of Copy" has been
silently falling back to a full copy on every single import. Worth confirming before you plan
around it:

```sh
# link count of 1 means it is a copy, not a hardlink
find /mnt/nvme-seed/arr-ingest -type f -name '*.mkv' -printf '%n %p\n' | head
```

If that prints `1`, every imported file has been stored twice on a pool that is 91.8 % full.

### Container mounts — identical in every app

| App | Mount | Host path | Notes |
|---|---|---|---|
| qBittorrent | `/data` | `/mnt/nvme-seed/data` | save path becomes `/data/torrents/complete` |
| Sonarr | `/data` | `/mnt/nvme-seed/data` | |
| Sonarr | `/library` | `/mnt/Media/library` | |
| Radarr | `/data` | `/mnt/nvme-seed/data` | |
| Radarr | `/library` | `/mnt/Media/library` | |
| gatekeeper | `/data` | `/mnt/nvme-seed/data` | never touches `/library` |
| Tdarr | `/data` | `/mnt/nvme-seed/data` | watches `/data/transcode`, caches to `/data/cache` |
| Bazarr | `/library` | `/mnt/Media/library` | |
| Jellyfin | `/library` | `/mnt/Media/library` | read-only |

Because every app calls the download area `/data` and the library `/library`, **every path
mapping in the system can be deleted**: Sonarr's and Radarr's remote path mapping for
qBittorrent, and all four of Bazarr's path mappings. A path is a path everywhere.

---

## 4. Who does what

| Component | Responsibility | Explicitly not its job |
|---|---|---|
| Jellyseerr | the only manual step — requests | anything about files |
| Prowlarr | indexer sync | — |
| Sonarr / Radarr | grab, and perform the single library write on import | transcoding, subtitles, moving between roots |
| qBittorrent | download and seed | deciding where anything ends up |
| gatekeeper | subtitle extraction, translation, Tdarr handoff, verification, triggering import | writing to the library |
| Tdarr | AV1 encode, strip subtitles | discovering files on its own |
| Bazarr | source **English** subtitles when a release has none | Swedish (see below) |
| Jellyfin | playback | — |

### One translation engine, two entry points

The messiest part of the current setup is that Swedish subtitles can come from three places.
The fix keeps one engine and gives it two doors:

- **Normal path** — the release has an embedded English track. The gatekeeper extracts and
  translates it before import. Styling, positions and outlines survive because ASS override
  tags are masked and restored rather than re-typed.
- **Fallback path** — the release has no English track. It imports without Swedish. Bazarr then
  fetches an English subtitle for it post-import, and its existing custom post-processing hook
  POSTs that file to the gatekeeper's `/translate` endpoint, which returns the Swedish version.
  Same engine, no shared filesystem needed — which is exactly why that hook was written that way.

The gatekeeper skips translation if a `.sv` sidecar already exists, so if Bazarr finds a real
human-made Swedish subtitle, that one wins. Machine translation only fills genuine gaps.

Sidecar naming is `<video name>.sv.ass` (or `.srt`) — nothing else, so Jellyfin reliably reads it
as Swedish.

---

## 5. What this deletes

Simplification is measured in what stops existing:

- The `/nvme/arr-ingest/*` root folders in Sonarr and Radarr — gone. One root each.
- `rootFolderPath` rewriting, `MoveSeries` / `MoveMovie`, and all move-verification logic — gone.
  The import *is* the move.
- Sonarr's and Radarr's qBittorrent remote path mappings — gone.
- All four Bazarr path mappings — gone.
- The ai-translator LXC (CT 101) — gone. The gatekeeper becomes a TrueNAS app next to the others,
  which fixes the no-media-mounted problem, the start-at-boot problem and the degraded-unit
  problem in one move, and removes a whole machine from the data path.
- The Tdarr "Media (Duplicate)" library — gone, replaced by the migration in §7.
- Tdarr's folder watch on the ingest folder — replaced by a watch on `/data/transcode`, which
  only ever contains files that are ready for it.

---

## 6. Rollout

Each phase leaves the system working. Nothing here is a big-bang cutover.

### Phase 0 — immediate, no restructuring

1. Rotate the Radarr API key (it was legible in the screenshots).
2. Set CT 101 "Start at boot" → Yes, and run `systemctl --failed` to find the failed unit.
3. Clear the Tdarr transcode cache. It is holding roughly 389 GiB of abandoned work on a pool
   with 79 GiB free.
4. Turn Tdarr's **Hold Files After Scanning** on (1 hour is already configured). Until the new
   pipeline lands, this is the only thing standing between an import and a stripped subtitle
   track.
5. Add access lists or authentication in front of `request.pandi.se` and `watch.pandi.se` in
   NPMplus — both are currently publicly reachable with no restriction.

### Phase 1 — storage reshape

1. Create `nvme-seed/data` as a single dataset.
2. Create plain directories inside it: `torrents/complete`, `torrents/incomplete`, `work`,
   `transcode`, `cache`.
3. Move the torrent data in, then use qBittorrent's "Relocate torrent" / recheck so seeding
   continues from the new path.
4. Destroy the old `arr-ingest`, `tdarr-cache` and `torrents/*` datasets once empty.
5. Repoint every app's mounts to the table in §3.
6. Delete the now-redundant path mappings in Sonarr, Radarr and Bazarr.

Verify hardlinks work before continuing: create a file in `torrents/complete`, `ln` it into
`work`, confirm the link count is 2.

### Phase 2 — deploy the gatekeeper as a TrueNAS app

1. Build the service as a custom app with `/data` mounted and `ffmpeg` available.
2. Point Sonarr's and Radarr's existing `AI WORM Gatekeeper` webhooks at its new address.
   Keep the trigger on **On File Import** — in the new design that is the *completion* signal,
   not the trigger.
3. Add qBittorrent → Options → Downloads → **Run on torrent finished**:
   ```
   curl -sS -X POST -H "X-Api-Key: $SECRET" -H "Content-Type: application/json" \
     -d "{\"hash\":\"%I\",\"path\":\"%F\",\"category\":\"%L\",\"name\":\"%N\"}" \
     http://<gatekeeper>:5000/api/download/complete
   ```
4. Leave the old LXC running but idle until Phase 3 proves out, then delete it.

### Phase 3 — switch the pipeline over

1. Repoint the Tdarr library from `/ingest` to `/data/transcode`, keeping the plugin stack as
   it is. Add `av1` to "Codecs to skip". Leave "Skip hardlinked files" **off**.
2. Turn **Completed Download Handling → Import** *off* in both Sonarr and Radarr. From here the
   gatekeeper is the importer.
3. Remove the `/nvme/arr-ingest/*` root folders from both apps, leaving one root each.
4. Bazarr: turn on default language profiles for new series and movies (currently off, so new
   content gets nothing), set the profile to English plus Swedish, and leave **Remove Tags off**
   so styling survives.
5. Watch the first few imports end to end before trusting it.

### Phase 4 — the old library migration

See §7.

---

## 7. Migrating the old library (replacing the Tdarr duplicate)

There is 2.26 TiB in `Andi/Media/Library` — 1 TiB TV, 804 GiB anime, 485 GiB movies — that
Jellyfin cannot see, because its four libraries all point at the new pool, and that Sonarr and
Radarr do not track either. Using a second Tdarr library to move it was the worst available
option for an SMR destination: many small random writes, interleaved reads, transcoding in
place. SMR drives handle exactly one workload well — **large sequential writes, one at a time.**

**The approach: copy at the storage layer, transcode on NVMe, register afterwards.**

1. **Pause the pipeline.** Stop the gatekeeper and Tdarr. Concurrent writes are what turn an
   SMR migration from slow into pathological.
2. **Work in batches**, a few hundred GB at a time — a handful of series, or one letter of the
   alphabet. Never the whole 2.26 TiB in one run.
3. For each batch, if you want it in AV1: copy it to `/data/work/migrate/`, run it through
   Tdarr on the NVMe pool, and let the gatekeeper produce Swedish subtitles for it exactly as it
   does for new content. All the churn happens on flash.
4. **Write to the library once, sequentially:**
   ```sh
   rsync -a --info=progress2 --no-compress /data/work/migrate/ /mnt/Media/library/tv/
   ```
   One rsync at a time. No parallelism. If you skip the transcode step, rsync straight from
   `/mnt/Andi/Media/Library/` instead.
5. **Register the batch** with Sonarr's "Import Existing Series" / Radarr's "Import Existing
   Movies" against the single `/library` root. They match to TVDB/TMDB and rename into your
   naming scheme.
6. Let Jellyfin's real-time monitoring pick it up, or scan the library once per batch.
7. Only delete the source batch once step 5 confirms the files are tracked.

Budget real time for this. An ST8000DM004 sustains roughly 20–40 MB/s once its CMR cache is
exhausted, so 2.26 TiB is on the order of a week of wall-clock writing. Batching it means you
can stop and resume at any point without leaving the library half-consistent.

**Worth deciding before you start:** when the old library is empty, the two Toshiba CMR disks in
the `Andi` mirror free up 2.26 TiB. If a mirrored CMR library is where you want to end up, you
may be better off skipping the SMR destination altogether and migrating once, straight to the
target you actually want. The alternative — and a genuinely good use for an SMR disk — is to make
the ST8000DM004 the *backup* target for the library rather than the library itself. Sequential
bulk writes are the one thing it is good at, and the `Media` pool currently has no redundancy of
any kind.

---

## 8. Swapping the library pool later

This is the property the layout is designed to give you, and it is worth spelling out because it
depends on one deliberate choice: **the library is addressed by pool name, and the pool name
describes its role, not its hardware.**

Every app mounts `/mnt/Media/library` at `/library`. So:

```sh
# 1. build the new pool under a temporary name
zpool create Media2 mirror <disk1> <disk2>

# 2. replicate, with the apps stopped for the final incremental
zfs snapshot -r Media/library@migrate
zfs send -R Media/library@migrate | zfs recv -F Media2/library

# 3. swap the names — this is the whole trick
zpool export Media   && zpool import Media MediaOld
zpool export Media2  && zpool import Media2 Media

# 4. start the apps
```

`/mnt/Media/library` now resolves to the new mirror. Sonarr, Radarr, Bazarr and Jellyfin see
byte-identical paths and never know anything happened — no root folder edits, no re-matching, no
library re-scan, no lost watch history. The old pool stays imported as `MediaOld` until you are
satisfied, then gets destroyed or repurposed as the backup target.

If you ever *do* have to change the host path, it is one field per app in §3's table and nothing
else.

---

## 9. Open risks and decisions

- **`DownloadedEpisodesScan` behaviour must be confirmed on your versions.** The command and its
  `path` / `downloadClientId` / `importMode` properties exist in current Sonarr and Radarr and
  are marked "used by third-party apps, do not modify", but verify on the first real import that
  the queue item is resolved correctly. Fallback is the ManualImport API.
- **Radarr tracks the `develop` branch** and runs at Debug log level with a 1 MB log cap. Fine
  deliberately, worth knowing if something breaks after an update.
- **The TrueNAS VM has 16 GiB on a 32 GiB host** and reports 100 % usage. ZFS ARC plus Jellyfin,
  Tdarr, Bazarr and the *arr apps all live inside that. Raising it to 24 GiB costs nothing.
- **nvme-seed sits at 91.8 %.** ZFS performance degrades badly above ~80 %. After clearing the
  Tdarr cache and fixing hardlinks, the remaining pressure is the seeding policy: ratio 2 or
  21 days, with Torrent Queueing disabled and up to 200 active torrents.
- **`Media/library/dont_media`** is a dataset inside the library root that Tdarr mounts. Anything
  inside a library root will eventually be scanned by something; it belongs outside `/library`.
- **Decide whether SMR stays.** The new design writes to it exactly once per file, which is
  SMR-friendly. The redundancy question is separate and unanswered.
