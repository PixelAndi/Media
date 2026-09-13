# Homelab setup map

Reconstructed from screenshots of the live setup. Values marked **(verify)** were read off a
photo of a screen and may be slightly wrong.

Batch 1: 16 screenshots (Tdarr, Bazarr, Jellyseerr, Prowlarr).
Batch 2: 16 screenshots (Radarr, Sonarr).
Batch 3 pending — **full analysis is deliberately deferred until all batches are in.**

No API keys or passwords are recorded here, even where they were legible in a screenshot.

## Hosts and ports

| Service | Address | Notes |
|---|---|---|
| Sonarr | `192.168.50.45:30113` | v4.0.20.3012 |
| Radarr | `192.168.50.45:30025` | v6.4.3.10646 |
| Bazarr | `192.168.50.45:30046` | v1.6.0, `home-operations` image |
| Tdarr | `192.168.50.45:30088` | v2.87.01 |
| Jellyfin | `192.168.50.45:30013` | |
| Prowlarr | `192.168.50.45:30050` | **(verify)** |
| Jellyseerr | `request.pandi.se` | external hostname |
| AI translator | `192.168.50.160:5000` | **different host** from the *arr stack |

The *arr stack reports `Linux-6.12.99-production+truenas-x86_64-with-musl1` — it runs on
TrueNAS at `.45`, while the translator lives at `.160`.

## Storage layout

Each container sees these datasets under a different prefix. This is the single most important
thing to get right for anything new that touches files.

| Dataset | Sonarr / Radarr | Bazarr | Tdarr | qBittorrent |
|---|---|---|---|---|
| Torrent downloads | `/nvme/torrents/complete/` | — | — | `/complete/` |
| Hot ingest (NVMe) | `/nvme/arr-ingest/...` | *not mapped* | `/ingest` **(verify)** | — |
| Cold library (SMR) | `/library/...` | `/Media/library/...` | — | — |
| Transcode cache | — | — | `/tdarr-cache` **(verify)** | — |

**Sonarr root folders** (all four registered): `/library/anime/tv`, `/library/tv`,
`/nvme/arr-ingest/anime/tv`, `/nvme/arr-ingest/tv`

**Radarr root folders** (all four registered): `/library/anime/movies`, `/library/movies`,
`/nvme/arr-ingest/anime/movies`, `/nvme/arr-ingest/movies`

New content is added to the `/nvme/arr-ingest/...` roots (per Jellyseerr's server config); the
`/library/...` roots are the cold destinations.

Bazarr's configured path mappings are `/Media/library/tv/` → `/Media/library/tv/` and the
equivalents for anime/movies — i.e. the *source* side is written in Bazarr's own namespace
rather than Sonarr's (`/library/tv/`), and there is no mapping at all for
`/nvme/arr-ingest/...`. Bazarr's health page reports all three ingest roots as inaccessible.

## Tdarr — library "Media"

There is a second tab, **"Media (Duplicate)"**, whose settings were not captured. See open
questions.

**Source**
- Process Library ON · Transcodes ON · Health Checks OFF
- Scan on Start ON · Hourly Scan (find new) ON · File scanner threads 2
- **Hold Files After Scanning: OFF** (Hold Duration is set to 1 hour but inactive)
- Folder Watch ON · scan interval 30s · Use File System Events ON
- Scanners: FFprobe ON, ExifTool ON, MediaInfo ON, Closed Captions OFF, Directory Library OFF

**Transcode Cache** — `/tdarr-cache` **(verify)**. Files transcode into the cache, then the
result is moved back to the library (or output folder).

**Output Folder** — Output Folder OFF, Copy-to-output OFF, **Delete source file ON**,
Add to Skiplist OFF. The Output path is empty and shows "Invalid folder path", which is
harmless while Output Folder is off. Net effect: **Tdarr transcodes in place, replacing the
original file.**

**Filters**
- Containers scanned: `mkv,mp4,mov,m4v,mpg,mpeg,avi,flv,webm,wmv,vob,evo,iso,m2ts,ts`
- Ignore terms: empty
- **Skip hardlinked files: OFF** — required, since qBittorrent's files reach the ingest folder
  as hardlinks. If this is ever turned on, nothing gets processed.
- Resolutions/Codecs/Containers to skip at queue level: all empty

**Transcode Options** — Classic Plugin Stack ON (Flows OFF), Process Plugins In Exact Order ✓:

1. Migz Remux Container
2. Migz Clean Title Metadata
3. Migz Remove Image Formats From File
4. Remove Video Commentary Tracks
5. Migz Clean Audio Streams
6. Migz Convert Audio Streams
7. **Remove Subtitles**
8. **Boosh-Transcode Using QSV GPU & FFMPEG** (AV1 on the Arc A310)
9. lmg1 Reorder Streams
10. New File Size Check

## Sonarr (v4.0.20.3012)

**Media Management**
- Rename Episodes ✓ · Replace Illegal Characters ✓ · Colon Replacement: Smart Replace
- Standard/Daily format: `{Series TitleYear} - S{season:00}E{episode:00} - {Episode CleanTitle} [{Quality Full}][{Custom Formats}][{MediaInfo VideoD…`
- Anime format: same with `- {absolute:000} -` inserted after the episode number
- Series folder: `{Series CleanTitleWithoutYear} ({Series Year}) [tvdbid-{TvdbId}]` ·
  Season folder: `Season {season}` · Specials folder: `Specials` · Multi-episode: Prefixed Range
- Create/Delete empty series folders: both ✗
- Episode Title Required: Only for Bulk Season Releases · Minimum Free Space 100 MB
- **Use Hardlinks instead of Copy ✓** · Import Using Script ✗
- **Import Extra Files ✓ — `srt,ass`**
- Unmonitor Deleted Episodes ✗ · Propers and Repacks: Prefer and Upgrade ·
  Analyse video files ✓ · **Rescan Series Folder after Refresh: Always** · Change File Date: None
- Recycling Bin: empty (cleanup 7 days) · Set Permissions ✗ (chmod 755 / umask 0022)

**Profiles** — Any - 1080p/SD, Any Anime - 1080p/SD, HD - 1080p, HD Anime - 1080p, UHD - 4K,
UHD Anime - 4K. Delay profile: Prefer Usenet, no delay on either protocol. No release profiles.

**Quality definitions** — Preferred is 95 on every row. Max: 100 (SD/480/576), 125 (HDTV 720/1080),
130 (WEB/Bluray 720–1080), 155 (Bluray-1080p), 1000 (Raw-HD, all Remux and 2160p rows),
199.9 (Unknown, HDTV-2160p). Min: 2–4 for HD rows, 35 for Remux/2160p.

**Custom Formats** — TRaSH-style set: 2.0 Stereo, Anime - English Dub, Anime BD Tier 01–08,
Anime Dual Audio, Anime LQ Groups, Anime Raws, Anime Web Tier 01–06, Asian LQ, Asian Tier 01–03,
**ASS**, Dubs Only, Executable / Malware, HD Bluray Tier 01, MULTi, Remux Tier 01–02, Uncensored,
Upscaled, WEB Tier 01–02, x265 (HD).

**Indexers** (all via Prowlarr) — Bangumi Moe, LimeTorrents, nekoBT, Nyaa.si, SubsPlease,
The Pirate Bay, TorrentDownload. **RSS Sync Interval 15 minutes.** Minimum Age 0, Retention 0,
Maximum Size 0.

## Radarr (v6.4.3.10646, `develop` branch)

**Media Management**
- Rename Movies ✓ · Replace Illegal Characters ✓ · Colon Replacement: Smart Replace
- Standard format: `{Movie CleanTitle} ({Release Year}) [imdbid-{ImdbId}] - {edition-{Edition Tags}} [{Quality Full}][{Custom Formats}][{Media…`
- Movie folder: `{Movie CleanTitle} ({Release Year}) [imdbid-{ImdbId}]`
- Radarr shows a deprecation warning: movie-file property tokens will stop being supported in a
  future major version
- Create/Delete empty movie folders: both ✗ · Minimum Free Space 100 MB
- **Use Hardlinks instead of Copy ✓** · Import Using Script ✗
- **Import Extra Files ✓ — `srt,ass`**
- Unmonitor Deleted Movies ✗ · Propers and Repacks: Prefer and Upgrade · Analyze video files ✓ ·
  **Rescan Movie Folder after Refresh: Always** · Change File Date: None
- Recycling Bin: empty (cleanup 7 days) · Set Permissions ✗ (chmod 755 / umask 0022)

**Profiles** — same six profiles as Sonarr. Delay profile: Prefer Usenet, no delay. No release
profiles.

**Quality definitions** — Preferred 95 / Max 100 for everything up to Bluray-1080p;
Preferred 1999 / Max 2000 with unlimited size sliders for Remux-1080p, all 2160p rows, BR-DISK
and Raw-HD.

**Custom Formats** — same TRaSH-style set as Sonarr plus HD Bluray Tier 01–03, Remux Tier 01–03,
UHD Bluray Tier 01–03, LQ, WEB Tier 01–03.

**Indexers** (all via Prowlarr) — Bangumi Moe, nekoBT, Nyaa.si, The Pirate Bay, TorrentDownload.
**RSS Sync Interval 30 minutes.** Prefer Indexer Flags ✗ · Availability Delay 0 ·
Whitelisted Subtitle Tags empty · **Allow Hardcoded Subs ✗**

**Download client** — qBittorrent, enabled.
- **Completed Download Handling: Enabled** ("Automatically import completed downloads from
  download client"), check interval 1 minute
- Failed Download Handling: Redownload Failed ✓, Redownload Failed from Interactive Search ✓
- **Remote Path Mapping:** host `192.168.50.45`, remote `/complete/` → local `/nvme/torrents/complete/`

**Connect** — one webhook connection, **"AI WORM Gatekeeper"**:
- Trigger: **On File Import only** (On Grab, On File Upgrade, On Rename, On Movie Added,
  On Movie Delete, On Movie File Delete, health/update/manual-interaction triggers all unchecked)
- URL `http://192.168.50.160:5000/api/import`, method POST, no basic auth
- Header `X-Api-Key: <redacted>`
- No tag filter

**General** — bind `*`, port 30025, no URL base, no allowed-hosts restriction, instance name
"Radarr", no application URL, SSL off. Authentication: Forms (login page), required Enabled,
username `andi`. Certificate validation Enabled, no trusted networks, no proxy.
**Log Level: Debug**, log size limit 1 MB. Anonymous usage data off.
Updates: branch **develop**, automatic off, mechanism Docker. Backups every 7 days, retention 28.

## Bazarr

**Languages** — filter: English, Japanese, Swedish. Single Language OFF. Deep-analyze audio
track language ON. One profile, "Standard Subs", containing **EN and EN:FORCED only**.
Tag-based profile selection OFF for both. Default profile for newly added shows **OFF** for
both Series and Movies.

**Providers** — OpenSubtitles.com, Gestdown (Addic7ed proxy). No anti-captcha. HTTPS cert
validation left enabled (correct).

**Subtitles**
- Subtitle Folder: **AlongSide Media File** (sidecars next to the video — what Jellyfin wants)
- Encode to UTF-8 ON · chmod after download OFF
- Treat Embedded Subtitles as Downloaded: OFF
- Whisper as fallback: OFF
- Upgrade Previously Downloaded Subtitles ON (~7 days); Upgrade Manually Downloaded or
  Translated Subtitles OFF
- Adaptive searching ON (3 weeks / 1 week) · Search providers simultaneously ON ·
  Skip video hash calculation OFF
- **Sub-Zero modifications: all OFF** — importantly **Remove Tags is OFF**, so styling and
  positioning tags survive. Keep it that way.
- Audio synchronization OFF
- Built-in Translator set to **Google Translate**, score slider 0
- **Custom Post-Processing ON**, command:
  ```sh
  sh -c 'src="{{subtitles}}"; dest="${src%.*}.sv.AI-Translated.${src##*.}"; \
         curl -s -F "file=@$src" http://192.168.50.160:5000/translate -o "$dest"'
  ```

**Sonarr connection** — `192.168.50.45:30113`, timeout 60, sync on connect ON, minimum score
50, Download Only Monitored OFF, defer searching OFF, exclude season zero OFF.
Path mappings: `/Media/library/tv/` → `/Media/library/tv/`, `/Media/library/anime/tv/` →
`/Media/library/anime/tv/` (identity mappings).

**Radarr connection** — `192.168.50.45:30025`, timeout 60, sync on connect ON, minimum score
45. Path mappings: `/Media/library/movies/` and `/Media/library/anime/movies/`, both identity.

**Jellyfin** — enabled, `http://192.168.50.45:30013/`, notify **Immediate**, movie libraries
"Movies" + "Anime Movies", series libraries "Shows" + "Anime Shows", refresh metadata after
subtitle download ON for both.

**System** — Python 3.14.6, SQLite 3.53.2, config at `/config`, uptime 1d12h,
**Time Zone America/Los_Angeles**.

## Jellyseerr (`request.pandi.se`)

- Radarr (default, non-4K): `192.168.50.45:30025`, profile HD-1080p, root `/nvme/arr-ingest/movies`,
  minimum availability Released, Enable Scan ✓, Enable Automatic Search ✓
- Sonarr (default, non-4K): `192.168.50.45:30113`, series type Standard, profile HD-1080p,
  root `/nvme/arr-ingest/tv`; anime type Anime, profile "HD Anime - 1080p", anime root
  `/nvme/arr-ingest/anime/tv`; Season Folders ✓, Monitor New Seasons All, Enable Scan ✓,
  Enable Automatic Search ✓

## Prowlarr

Apps: Radarr and Sonarr, both **Full Sync**. One sync profile, "Standard" (RSS + Automatic
Search + Interactive Search). Radarr sync categories are the Movies/* set. "Sync Reject
Blocklisted Torrent Hashes While Grabbing" unchecked.

---

# Findings

**Status: batch-1 observations only. The consolidated review is on hold until batch 3 arrives.**
Batch-2 items worth revisiting are listed at the end, without recommendations.

## Confirmed working / correctly set

- Bazarr writes sidecars alongside the media file, and **Remove Tags is off**, so subtitle
  styling is not stripped before translation.
- Tdarr's "Skip hardlinked files" is off, which is required for the qBittorrent → hardlink →
  ingest flow to be processed at all.
- Jellyfin is notified immediately after subtitle changes, with metadata refresh on.
- Sonarr/Radarr/Jellyseerr agree on root folders — all point at the hot NVMe ingest.

## Needs attention

1. **Tdarr strips subtitles before transcoding** (plugin 7 runs before plugin 8), and Folder
   Watch is on with a 30-second interval plus filesystem events. This is direct confirmation
   of the race the rewritten translator was built to close: Tdarr can destroy the embedded
   English track within seconds of the file landing.

   *Interim mitigation available right now:* **Hold Files After Scanning** is OFF with a
   1-hour duration already configured. Turning it ON delays newly scanned files from entering
   the queue, which buys the extractor a window until the gated handoff lands.

2. **Bazarr's post-processing points at an endpoint that no longer exists.** The command posts
   a multipart file to `http://192.168.50.160:5000/translate` and writes the response body to
   disk. The rewritten `ai_translator.py` exposes `/api/import`, `/api/jobs` and `/health` —
   there is no `/translate`. Either that hook is left over from an older service, or the two
   subtitle paths need reconciling. Worth deciding which system owns Swedish subtitles.

3. **The `.sv.AI-Translated.<ext>` naming is risky for Jellyfin.** Jellyfin reads the tokens
   after the base name as language plus flags; an unrecognized `AI-Translated` token can cause
   the track to be labelled oddly or not matched to Swedish. `Episode.sv.srt` is the safe form.

4. **Bazarr cannot see the ingest roots.** Health reports all three of `/nvme/arr-ingest/tv`,
   `/nvme/arr-ingest/anime/tv` and `/nvme/arr-ingest/movies` as inaccessible. The only path
   mappings configured are identity mappings for `/Media/library/...`. So Bazarr can only work
   on files that already reached the cold library — nothing in ingest.

5. **The only language profile is EN + EN:FORCED, and default profiles for new shows are OFF.**
   Newly added series and movies get no language profile at all, so Bazarr will not fetch
   anything for them automatically. If Bazarr is meant to supply the English source subtitles,
   this silently does nothing for new content.

6. **No codec skip filter in Tdarr.** "Codecs to skip" is empty and the classic plugin stack is
   cyclic ("newly transcoded files will be passed down through all plugins"). Adding `av1`
   there avoids re-queueing files that are already in the target codec.

7. **Bazarr's timezone is America/Los_Angeles** while the server is in Europe. Scheduled tasks
   and history timestamps will be 9 hours off.

8. **Two translation paths exist.** Bazarr's built-in Translator is set to Google Translate
   *and* a custom post-processing hook calls the AI translator. Whichever is authoritative,
   the other is at best wasted work.

## Batch-2 items to revisit (no recommendations yet)

- Radarr's `AI WORM Gatekeeper` webhook fires on **On File Import only** — On File Upgrade is
  not selected.
- **Completed Download Handling is enabled** in Radarr with a 1-minute check interval. This is
  the setting the pre-import restructure would need to change.
- Both Sonarr and Radarr have **Import Extra Files ✓ with `srt,ass`** — the prerequisite for
  sidecars travelling with the media was already in place.
- Both use **hardlinks instead of copy**, and **Rescan after Refresh: Always**.
- Radarr sees the cold library as `/library/...` while Bazarr's mappings are written against
  `/Media/library/...`; there is no mapping for `/nvme/arr-ingest/...` in Bazarr at all.
- Delay profiles in both apps say **Prefer Usenet**, but every configured indexer is a torrent
  tracker.
- Radarr tracks the **`develop`** branch (hence v6.x) and runs at **Debug** log level with a
  1 MB log cap.
- Radarr's quality definitions allow up to 2000 MB/min on Remux/2160p rows; Sonarr's cap the
  same tiers at 1000.
- An **ASS** custom format exists in both apps.
- Neither app has any Release Profile configured.

## Open questions

- **Tdarr "Media (Duplicate)" library** — what is its source path and plugin stack? If it
  points at the same folder, files may be processed twice.
- Does **Sonarr** have an equivalent of Radarr's `AI WORM Gatekeeper` webhook? Only Radarr's
  Connect page has been captured.
- Does qBittorrent currently have any on-completion hook configured, and what are its categories?
- Which service actually answers on `192.168.50.160:5000` today, and does it serve `/translate`,
  `/api/import`, or both?
- Is `/ingest` in Tdarr the same dataset as `/nvme/arr-ingest`?
- Sonarr's Download Clients page (Completed Download Handling, remote path mappings) has not
  been captured.

## To be filled from batch 3

- qBittorrent (categories, paths, completion hook, seeding rules)
- Sonarr: Download Clients, Connect
- Jellyfin libraries and subtitle settings
- Anything else sent
