# AI Translator — subtitle gate between *arr imports and Tdarr

Extracts English subtitles from a freshly imported file **before** Tdarr can touch it,
translates them to Swedish while Tdarr transcodes to AV1, then puts the finished media
and the `.sv` sidecar into the cold library through Sonarr/Radarr.

```
qbittorrent -> /complete
                  |  hardlink
Sonarr/Radarr -> /arr-ingest ──webhook──> ai-translator
                                             1. wait for a stable file
                                             2. extract the English subtitle track
                                             3. hardlink into the Tdarr queue folder  ──> Tdarr
                                             4. translate EN -> SV (in parallel)
                                             5. verify Tdarr output: AV1 + no subtitles
                                             6. swap it back into /arr-ingest, drop the .sv sidecar beside it
                                             7. *arr rescan -> move to /library/... -> verify
```

Tdarr never sees the file until step 3, so the embedded English track can no longer be
stripped out from under the extractor. That race was the central bug in the previous version.

## Install

```bash
sudo mkdir -p /opt/ai-translator
sudo cp ai_translator.py /opt/ai-translator/
sudo cp config.example.json /opt/ai-translator/config.json
python3 -m venv /opt/ai-translator/venv
/opt/ai-translator/venv/bin/pip install -r requirements.txt
sudo cp ai-translator.service /etc/systemd/system/
sudo systemctl enable --now ai-translator
```

`ffmpeg` and `ffprobe` must be on `PATH`. The service refuses to start without them.

`secrets.json` (mode 600) next to the script:

```json
{
  "SONARR_URL": "http://10.0.0.10:8989",
  "SONARR_API_KEY": "...",
  "RADARR_URL": "http://10.0.0.10:7878",
  "RADARR_API_KEY": "...",
  "GEMINI_API_KEY": "...",
  "WEBHOOK_SECRET": "a long random string"
}
```

Everything else lives in `config.json` — see `config.example.json`.

## Required settings in the other services

**Tdarr** — point the library at `tdarr_staging_root` (e.g. `/arr-ingest/.tdarr-queue`),
**not** at `/arr-ingest`. This is the change that closes the race.
- The staging folder must be on the same filesystem as `/arr-ingest` (staging uses hardlinks;
  the service logs a warning at startup if it is not).
- The flow must output AV1 **and remove all subtitle streams**, writing the result back into
  the same per-job folder. A file that keeps its subtitles is never accepted — the job fails
  after `transcode_timeout_hours` with an explicit message.

**Sonarr / Radarr**
- Connect → Webhook → `http://<host>:5000/api/import`, method POST, triggers: *On Import* and
  *On Upgrade* only. Add header `X-Api-Key: <WEBHOOK_SECRET>`.
- Settings → Media Management → **Import Extra Files**, with `srt,ass` in the extensions list,
  so the `.sv` sidecar travels with the media on a move. The service also verifies the sidecar
  landed next to the media afterwards and relocates it itself if *arr left it behind.

## Behaviour worth knowing

- **Nothing is ever deleted to "recover" from a failed verification.** If a move cannot be
  confirmed, the job is marked `failed` with the reason and both copies are left in place.
- **Formatting is preserved by masking.** Every `{\an8}`-style override tag and `\N` break is
  replaced by a `[[n]]` token before translation and restored afterwards. If a line comes back
  with tokens missing or duplicated, that line keeps its English text rather than losing its
  positioning. ASS styles (outline width, colours, `PlayRes`) survive untouched via pysubs2.
- **SRT carries no positioning or outline data at all.** When the source track is SRT the
  styling requirement cannot be met — there is nothing to preserve. ASS/SSA tracks are
  preferred automatically when a file has both.
- **Image-based subtitles (PGS/VobSub) are not supported** — there is no OCR step. Those jobs
  record that reason and continue without Swedish subtitles.
- If translation fails permanently the media is still finished and moved
  (`move_without_subtitles`, default `true`), with the reason recorded on the job.
- Re-importing the same path resets the job instead of creating a duplicate.

## Checking on it

```bash
curl -s localhost:5000/health | jq
curl -s -H "X-Api-Key: $WEBHOOK_SECRET" localhost:5000/api/jobs | jq '.[] | {source_path, state, translation_state, error_detail}'
journalctl -u ai-translator -f
```

## Known limitation: ongoing series

Moving a series to the cold root changes its root folder in Sonarr, so the *next* episode of
that series is imported straight into `/library/tv/...` rather than `/arr-ingest`. The pipeline
still runs for it — the webhook path is used wherever it points, and the move step is skipped
when the item is already in the target root — but that episode is transcoded in place on the
cold (SMR) disk instead of on the ingest pool. If that write pattern matters, the alternative is
to keep series permanently in the hot root and let Jellyfin read both locations.
