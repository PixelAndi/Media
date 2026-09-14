# Media gatekeeper

Sits between qBittorrent and the Sonarr/Radarr import. Nothing reaches the library pool until
it is final: AV1, no embedded subtitles, Swedish sidecar beside it.

The full design — storage layout, rollout phases, the old-library migration and the pool-swap
procedure — is in [`docs/architecture.md`](docs/architecture.md). The live setup it was written
against is recorded in [`docs/homelab-setup.md`](docs/homelab-setup.md).

```
qBittorrent finishes ──webhook──▶ gatekeeper
                                     1. hardlink media into /data/work/<job>/   (seed untouched)
                                     2. extract the English subtitle track
                                     3. hand the video to Tdarr via /data/transcode/<job>/<n>/
                                     4. translate EN → SV while Tdarr encodes
                                     5. verify: AV1, zero subtitle streams, duration matches
                                     6. move it back beside its .sv sidecar
                                     7. DownloadedEpisodesScan / DownloadedMoviesScan,
                                        importMode=Move  ── the only write to the library pool
                                     8. clean up the work folder
```

Tdarr cannot see a file until step 3, so the embedded English track can never be stripped before
it is extracted. The race is designed out rather than timed around.

## Install

Run it as a TrueNAS app alongside the *arr stack, with `/data` mounted and `ffmpeg` on `PATH`.
It must see the same `/data` tree as qBittorrent, Sonarr, Radarr and Tdarr, and that whole tree
must be **one ZFS dataset** — hardlinks cannot cross datasets.

```bash
pip install -r requirements.txt
cp config.example.json config.json      # edit paths and categories
python3 ai_translator.py
```

`secrets.json` (mode 600) next to the script:

```json
{
  "SONARR_URL": "http://10.0.0.10:8989",
  "SONARR_API_KEY": "...",
  "RADARR_URL": "http://10.0.0.10:7878",
  "RADARR_API_KEY": "...",
  "GEMINI_API_KEY": "...",
  "WEBHOOK_SECRET": "a long random string",
  "QBIT_USERNAME": "optional — only for the reconcile loop",
  "QBIT_PASSWORD": "optional"
}
```

A systemd unit is included for non-container deployments.

## Wiring

**qBittorrent** → Options → Downloads → *Run on torrent finished*:

```sh
curl -sS -X POST -H "X-Api-Key: YOUR_SECRET" -H "Content-Type: application/json" \
  -d "{\"hash\":\"%I\",\"path\":\"%F\",\"category\":\"%L\",\"name\":\"%N\"}" \
  http://gatekeeper:5000/api/download/complete
```

**Sonarr / Radarr**
- Connect → Webhook → `http://gatekeeper:5000/api/import`, trigger **On File Import**, header
  `X-Api-Key: YOUR_SECRET`. This is confirmation only; the pipeline no longer starts here.
- **Completed Download Handling → Import: off.** The gatekeeper is the importer.
- Media Management → **Import Extra Files** with `srt,ass`, so the `.sv` sidecar travels with
  the video.
- One root folder per content type, under `/library`.

**Tdarr** — library source `/data/transcode`, transcoding in place, plugin stack producing AV1
with all subtitles removed. Add `av1` to *Codecs to skip*; leave *Skip hardlinked files* **off**.

**Bazarr** (fallback path, for releases with no embedded English track) — custom post-processing:

```sh
sh -c 'src="{{subtitles}}"; dest="${src%.*}"; dest="${dest%.en}.sv.${src##*.}"; \
       curl -sS -H "X-Api-Key: YOUR_SECRET" -F "file=@$src" \
       http://gatekeeper:5000/translate -o "$dest"'
```

Leave Bazarr's **Remove Tags** off so styling survives.

## Endpoints

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/download/complete` | qBittorrent completion hook — starts the pipeline |
| POST | `/api/import` | *arr On Import webhook — confirmation only |
| POST | `/translate` | subtitle file in, Swedish subtitle out (Bazarr's hook) |
| GET | `/api/jobs` | job list for debugging |
| GET | `/health` | job counts by state, unauthenticated |

All except `/health` require the `X-Api-Key` header.

## Behaviour worth knowing

- **A missed webhook costs nothing.** A reconcile loop polls qBittorrent for completed torrents
  the database has never seen and enqueues them. On its very first run it adopts the existing
  seed list instead of ingesting all of it, then only considers completions newer than
  `reconcile_max_age_hours`.
- **Season packs are one unit.** A torrent with several episodes is imported once, when every
  file in it is finished.
- **Nothing is deleted to recover from a failure.** If an import cannot be confirmed the job is
  marked `failed` with the reason and the files stay in the work folder for a manual import.
- **Formatting is preserved by masking.** Every `{\an8}`-style override tag and `\N` break becomes
  a `[[n]]` token before translation and is restored afterwards. A line that comes back with
  tokens mangled keeps its English text rather than losing its positioning. ASS styles — outline
  width, colours, `PlayRes` — pass through untouched.
- **SRT carries no positioning or outline data**, so ASS/SSA tracks are preferred automatically
  when a release has both.
- **Image-based subtitles (PGS/VobSub) are not supported** — there is no OCR step. Those imports
  continue without Swedish subtitles and the reason is recorded on the job.
- If translation fails permanently the media is still imported (`import_without_subtitles`,
  default `true`).
- Only one translation runs at a time, whether it came from the pipeline or from Bazarr.

## Tests

```bash
python3 tests/test_pipeline_e2e.py      # two-episode season pack, real ffmpeg, stubbed Gemini/*arr
python3 tests/test_failure_paths.py     # auth, validation, timeouts, reconcile, /translate
```

Both build real media in a temp directory and need `ffmpeg`/`ffprobe` on `PATH`. The e2e run
includes SVT-AV1 encodes standing in for Tdarr, so it takes a couple of minutes.

## Checking on it

```bash
curl -s localhost:5000/health | jq
curl -s -H "X-Api-Key: $SECRET" localhost:5000/api/jobs | jq '.[] | {source_path, state, translation_state, error_detail}'
```
