# Homelab setup map

Reconstructed from screenshots of the live setup. Values marked **(verify)** were read off a
photo of a screen and may be slightly wrong.

Batch 1: 16 screenshots (Tdarr, Bazarr, Jellyseerr, Prowlarr). More to come — this file is
appended as they arrive.

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

Hot pool (NVMe), as Sonarr/Radarr see it:
- `/nvme/arr-ingest/tv` — Sonarr root folder
- `/nvme/arr-ingest/anime/tv` — Sonarr anime root folder
- `/nvme/arr-ingest/movies` — Radarr root folder

Cold pool (SMR), as Bazarr sees it:
- `/Media/library/tv/`, `/Media/library/anime/tv/`
- `/Media/library/movies/`, `/Media/library/anime/movies/`

Tdarr sees its source as `/ingest` **(verify)** and its transcode cache as `/tdarr-cache`
**(verify)** — container paths, presumably bound to the same NVMe dataset.

Note the path namespaces differ per container (`/nvme/arr-ingest/...` vs `/Media/library/...`
vs `/ingest`). Anything new that touches these paths needs an explicit mapping.

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

## Open questions

- **Tdarr "Media (Duplicate)" library** — what is its source path and plugin stack? If it
  points at the same folder, files may be processed twice.
- Does qBittorrent currently have any on-completion hook configured?
- Which service actually answers on `192.168.50.160:5000` today?
- Is `/ingest` in Tdarr the same dataset as `/nvme/arr-ingest`?

## To be filled from the next batch

- qBittorrent (categories, paths, completion hook)
- Sonarr/Radarr: Media Management, Import Extra Files, Completed Download Handling
- Jellyfin libraries and subtitle settings
- Anything else sent
