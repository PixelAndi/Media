# Server overhaul — step by step

A runbook for rebuilding the pipeline described in `architecture.md`. Written to be followed
in order, by someone who is not a programmer. Every command is copy-paste.

**Your seeding torrents are safe.** The storage reshape is done with a ZFS rename, which changes
a label, not data. No torrent file is copied, moved on disk, or re-checked. The container path
qBittorrent knows them by (`/complete/...`) stays exactly the same.

Where to run commands: **TrueNAS web UI → System → Shell** (the `>_` icon). Everything is one
line unless shown otherwise.

Total time: about 2 hours of attention, spread over as many evenings as you like. Each phase
ends with the system working.

---

## The one rule that makes or breaks this

**Every app in the chain must run as the same user.** qBittorrent downloads a file, the
gatekeeper hardlinks it, Tdarr rewrites it, Sonarr moves it. If they run as different users,
Linux quietly refuses some of those steps — and "quietly" is the problem.

qBittorrent already runs as user `1000`, group `1000`. We will make everything match it.

---

## Before you touch anything

### 1. Back up

```sh
zfs snapshot -r nvme-seed@before-overhaul
zfs snapshot -r fast-pool@before-overhaul
```

Then: **System → General → Manage Configuration → Download File**. Keep that file somewhere
off the server.

These two snapshots are your undo button for everything in Phase 1 and 2.

### 2. Five checks — write the answers down

```sh
# a) Are hardlinks working today? "1" means every import has been storing a second full copy.
find /mnt/nvme-seed/arr-ingest -type f -name '*.mkv' -printf '%n %p\n' 2>/dev/null | head -5

# b) What is using the pool, and how much room is left?
zfs list -o name,used,avail -r nvme-seed

# c) What user do the apps run as? (the two numbers after the permissions)
ls -ln /mnt/fast-pool/sonarr/config | head -3
ls -ln /mnt/fast-pool/radarr/config | head -3
ls -ln /mnt/nvme-seed/torrents/complete | head -3

# d) Which datasets exist under nvme-seed?
zfs list -r nvme-seed

# e) Does the qBittorrent container have curl? (needed for the completion hook)
docker exec qbittorrent which curl
```

If (c) shows anything other than `1000 1000` for sonarr or radarr, note the numbers — you will
change those apps to `1000:1000` in Phase 2, and you will need to chown their config dataset when
you do.

If (e) prints nothing, say so before Phase 4 and we will use a different hook command.

### 3. Check nothing lives in the ingest root folders

In **Sonarr → Series**, and **Radarr → Movies**, look at the Path column. If any show or movie
sits under `/nvme/arr-ingest/...`, move it to `/library/...` first (Sonarr: Edit Series → Root
Folder → pick the `/library` one → tick "Move Files"). Let those finish before Phase 1.

Nothing should be left in the ingest roots when you start.

---

## Phase 0 — quick wins (20 minutes, no restructuring)

These stand alone. Do them tonight even if you go no further.

1. **Rotate the Radarr API key.** Radarr → Settings → General → API Key → the circular arrow.
   It was legible in the screenshots you sent. Afterwards update it in Prowlarr, Bazarr and
   Jellyseerr, which all store a copy.
2. **Free ~389 GiB.** Tdarr's transcode cache is full of abandoned work:
   ```sh
   du -sh /mnt/nvme-seed/tdarr-cache
   rm -rf /mnt/nvme-seed/tdarr-cache/*
   ```
3. **Protect your subtitles until the new pipeline lands.** Tdarr → Libraries → Media → Source →
   turn **Hold Files After Scanning** on (the 1 hour duration is already set). This is currently
   the only thing preventing Tdarr from stripping an English track before anything extracts it.
4. **Lock down the two public hostnames.** NPMplus → Access Lists → create one → add your own
   username/password → apply it to `request.pandi.se` and `watch.pandi.se`. Both are currently
   open to the internet with no restriction.

---

## Phase 1 — storage reshape (30 minutes, no data movement)

**What this does:** turns the four separate datasets into one, so hardlinks work. Renaming a ZFS
dataset is instant and touches no file contents.

### 1. Stop the apps that write to the pool

TrueNAS → Apps: stop **qBittorrent**, **Sonarr**, **Radarr**, **Tdarr**, **Bazarr**.

Leave Jellyfin running if you like — it only reads the library pool.

### 2. Rename the dataset

```sh
zfs rename nvme-seed/torrents/complete nvme-seed/data
zfs get -H -o value mountpoint nvme-seed/data
```

The second command must print `/mnt/nvme-seed/data`. If it prints something else:

```sh
zfs set mountpoint=/mnt/nvme-seed/data nvme-seed/data
```

Your torrent files are now at `/mnt/nvme-seed/data/<torrent name>`. Nothing was copied.

### 3. Build the new layout inside it

```sh
mkdir -p /mnt/nvme-seed/data/torrents/complete /mnt/nvme-seed/data/work /mnt/nvme-seed/data/transcode /mnt/nvme-seed/data/cache

find /mnt/nvme-seed/data -mindepth 1 -maxdepth 1 \
  ! -name torrents ! -name work ! -name transcode ! -name cache \
  -exec mv -t /mnt/nvme-seed/data/torrents/complete/ {} +
```

That `mv` is instant no matter how many torrents you have — it is a rename inside one dataset,
not a copy.

### 4. One owner for everything

```sh
chown -R 1000:1000 /mnt/nvme-seed/data
chmod 775 /mnt/nvme-seed/data /mnt/nvme-seed/data/torrents /mnt/nvme-seed/data/torrents/complete \
          /mnt/nvme-seed/data/work /mnt/nvme-seed/data/transcode /mnt/nvme-seed/data/cache
```

### 5. Prove hardlinks work

```sh
touch /mnt/nvme-seed/data/torrents/complete/_hltest
ln /mnt/nvme-seed/data/torrents/complete/_hltest /mnt/nvme-seed/data/work/_hltest
stat -c '%h links' /mnt/nvme-seed/data/work/_hltest
rm -f /mnt/nvme-seed/data/torrents/complete/_hltest /mnt/nvme-seed/data/work/_hltest
```

**It must print `2 links`.** If it prints `1`, stop and say so — nothing after this works without it.

### 6. Point qBittorrent at the new location

Apps → qBittorrent → Edit → Custom Config. Change the volumes block to:

```yaml
    volumes:
      - /mnt/fast-pool/qbittorrent/config:/config
      - /mnt/nvme-seed/data/torrents/complete:/complete
      - /mnt/nvme-seed/torrents/incomplete:/incomplete
      - /mnt/nvme-seed/data:/data
```

The `/complete` line is the important one: the host path changed, the container path did not.
Every seeding torrent still finds its files exactly where it left them.

Start qBittorrent. **Check that your critical torrents still show as Seeding, not Errored.**
If any show "Missing files", right-click → Force recheck — the data is there, it just needs to
look again.

### 7. Fix the SMB shares that pointed at the old paths

Shares → SMB: edit `complete` to `/mnt/nvme-seed/data/torrents/complete`. Delete the
`arr-ingest` share; that dataset is going away.

---

## Phase 2 — one path everywhere (30 minutes)

**What this does:** every app stops using its own private names for the same folders. This is
what lets you delete every path mapping in the system.

For each app: **Apps → the app → Edit → Storage**.

### Sonarr and Radarr (both the same)

| Remove | Add |
|---|---|
| `/complete` | `/data` → `/mnt/nvme-seed/data` |
| `/nvme` | (keep `/library` → `/mnt/Media/library`) |

Also on the Edit page, set **User ID 1000 / Group ID 1000** if it is not already. If you change
it, first run:

```sh
chown -R 1000:1000 /mnt/fast-pool/sonarr /mnt/fast-pool/radarr
```

### Bazarr

Change its one storage entry from `/Media/library` to **`/library`** → `/mnt/Media/library`.
Set user/group to 1000 as above, and:

```sh
chown -R 1000:1000 /mnt/fast-pool/bazarr
```

### Tdarr

| Remove | Add |
|---|---|
| `/ingest`, `/complete`, `/tdarr-cache`, `/Library`, `/dont_media` | `/data` → `/mnt/nvme-seed/data` |

Set the **Tdarr Transcode Storage** host path to `/mnt/nvme-seed/data/cache`.
Set user/group to 1000, and `chown -R 1000:1000 /mnt/fast-pool/Tdarr`.

### Now delete the mappings that exist only because paths differed

- **Sonarr → Settings → Download Clients → Remote Path Mappings** — delete the entry.
- **Radarr → Settings → Download Clients → Remote Path Mappings** — delete the entry.
- **Bazarr → Settings → Sonarr → Path Mappings** — delete all rows. Same under **Settings → Radarr**.
- **Sonarr → Settings → Media Management → Root Folders** — delete `/nvme/arr-ingest/tv` and
  `/nvme/arr-ingest/anime/tv`. Leave `/library/tv` and `/library/anime/tv`.
- **Radarr → same** — delete the two `/nvme/arr-ingest/...` roots.

Start all the apps. Bazarr → System → Status should now show **no path errors**.

### Clean up the empty datasets

Once everything is running and happy:

```sh
zfs destroy nvme-seed/arr-ingest
zfs destroy nvme-seed/tdarr-cache
```

Leave `nvme-seed/torrents/incomplete` alone for now — it still holds in-progress downloads.

---

## Phase 3 — install the gatekeeper (30 minutes)

### 1. Put the files on the server

```sh
mkdir -p /mnt/fast-pool/gatekeeper
cd /mnt/fast-pool/gatekeeper
curl -fsSLO https://raw.githubusercontent.com/PixelAndi/Media/claude/magical-noether-halmjq/ai_translator.py
curl -fsSLO https://raw.githubusercontent.com/PixelAndi/Media/claude/magical-noether-halmjq/requirements.txt
curl -fsSL  https://raw.githubusercontent.com/PixelAndi/Media/claude/magical-noether-halmjq/config.example.json -o config.json
ls -l
```

If the repository is private those downloads will fail. In that case download the three files on
your PC, drop them into any SMB share, and `mv` them into `/mnt/fast-pool/gatekeeper`.

### 2. Create the secrets file

```sh
nano /mnt/fast-pool/gatekeeper/secrets.json
```

Paste this, filling in your own values (`Ctrl+O`, `Enter`, `Ctrl+X` to save):

```json
{
  "SONARR_URL": "http://192.168.50.45:30113",
  "SONARR_API_KEY": "paste from Sonarr settings",
  "RADARR_URL": "http://192.168.50.45:30025",
  "RADARR_API_KEY": "paste the NEW key from Phase 0",
  "GEMINI_API_KEY": "your Gemini key",
  "WEBHOOK_SECRET": "make up a long random string",
  "QBIT_USERNAME": "your qBittorrent web user",
  "QBIT_PASSWORD": "your qBittorrent web password"
}
```

```sh
chmod 600 /mnt/fast-pool/gatekeeper/secrets.json
chown -R 1000:1000 /mnt/fast-pool/gatekeeper
```

### 3. Edit config.json

```sh
nano /mnt/fast-pool/gatekeeper/config.json
```

Change exactly two things:

- `"db_file"` → `"/config/state.db"`
- `"qbittorrent"` → `"url"` → `"http://192.168.50.45:8080"`

Leave everything else. The `/data/...` paths are already correct.

### 4. Create the app

Apps → **Discover Apps → Custom App** → give it the name `gatekeeper` → choose the YAML /
custom config option, and paste:

```yaml
services:
  gatekeeper:
    image: python:3.12-slim
    container_name: gatekeeper
    restart: unless-stopped
    user: "1000:1000"
    ports:
      - "5000:5000"
    environment:
      - AI_TRANSLATOR_HOME=/config
    volumes:
      - /mnt/fast-pool/gatekeeper:/config
      - /mnt/nvme-seed/data:/data
    command: >
      bash -lc "apt-get update && apt-get install -y --no-install-recommends ffmpeg &&
                pip install --no-cache-dir -r /config/requirements.txt &&
                python /config/ai_translator.py"
```

The container installs ffmpeg and its libraries on every start, so give it two or three minutes
before you expect it to answer.

### 5. Check it is alive

```sh
curl -s http://192.168.50.45:5000/health
```

Expect `{"status":"ok","jobs":{}}`. If it does not answer, read the log:

```sh
docker logs --tail 50 gatekeeper
```

The startup checks are deliberately loud — they will tell you if `/data` is missing, if ffmpeg
is missing, or if the folders are on different filesystems.

---

## Phase 4 — turn the new pipeline on (30 minutes)

Do these in order. The last step is a live test.

### 1. qBittorrent

Options → **Downloads**:
- **Default Save Path**: `/data/torrents/complete`
- Keep incomplete torrents in: `/incomplete` (unchanged)
- Scroll to **Run external program** → tick **Run on torrent finished** and paste, as one line,
  replacing `YOUR_SECRET` with the `WEBHOOK_SECRET` from your secrets file:

```sh
curl -sS -X POST -H "X-Api-Key: YOUR_SECRET" -H "Content-Type: application/json" -d "{\"hash\":\"%I\",\"path\":\"%F\",\"category\":\"%L\",\"name\":\"%N\"}" http://192.168.50.45:5000/api/download/complete
```

Existing torrents keep their old save path and keep seeding. Only new downloads use the new one.

### 2. Sonarr and Radarr

- Settings → **Download Clients** → qBittorrent → **untick "Completed Download Handling → Import"**.
  From now on the gatekeeper is the importer. Leave Failed Download Handling on.
- Settings → **Connect** → `AI WORM Gatekeeper` → set the URL to
  `http://192.168.50.45:5000/api/import`, keep the trigger on **On File Import**, and update the
  `X-Api-Key` header to your new secret.
- Settings → Media Management → confirm **Import Extra Files** is ticked with `srt,ass`.

### 3. Tdarr

Libraries → Media → Source:
- **Source folder**: `/data/transcode`
- **Hold Files After Scanning**: turn it back **off** (the gatekeeper now controls what Tdarr sees)
- Folder Watch: leave on
- **Skip hardlinked files: off** — important, do not turn this on
- Filters → **Codecs to skip**: add `av1`
- Transcode Cache: `/data/cache`

Delete the **Media (Duplicate)** library — Phase 5 replaces what it was for.

### 4. Bazarr

Settings → Languages:
- Turn **on** the default language profile for Series and for Movies (both are currently off,
  which is why new content gets nothing).
- Edit the "Standard Subs" profile to contain **English and Swedish**.

Settings → Subtitles → Custom Post-Processing, replace the command with:

```sh
sh -c 'src="{{subtitles}}"; dest="${src%.*}"; dest="${dest%.en}.sv.${src##*.}"; curl -sS -H "X-Api-Key: YOUR_SECRET" -F "file=@$src" http://192.168.50.45:5000/translate -o "$dest"'
```

Leave **Remove Tags off** so subtitle styling survives.

### 5. Live test

Pick something small — a single episode of an ongoing show — and request it in Jellyseerr.

Watch it move:

```sh
watch -n 5 'curl -s -H "X-Api-Key: YOUR_SECRET" http://192.168.50.45:5000/api/jobs | head -c 2000'
```

The `state` field walks through `new` → `transcoding` → `transcoded` → `importing` → `done`.
`Ctrl+C` to stop watching. If anything fails, `error_detail` says why in plain language.

What you should see at the end: the episode in Jellyfin, in AV1, with a Swedish subtitle track,
and the original still seeding in qBittorrent.

---

## Phase 5 — bring the old library across

2.26 TiB sits in `/mnt/Andi/Media/Library` that Jellyfin cannot see. Do this **after** Phase 4 is
proven, and only in batches.

**A note on the pool question first.** You asked about going back to a mirrored CMR library. My
honest advice: don't, yet. Your irreplaceable data — Personal_Archive, Immich, Nextcloud — is
already on the mirrored pair, which is the right place for it. A media library is the most
replaceable data you own. The new pipeline writes to the SMR disk exactly once per file, which is
the workload SMR is actually good at. When you next add disks, `architecture.md` §8 swaps the
library pool without touching a single app setting.

For each batch (a few hundred GB — a handful of series, or one letter of the alphabet):

```sh
# 1. pause the pipeline so nothing else writes to the SMR disk
docker stop gatekeeper

# 2. copy one batch, sequentially. No parallel copies - that is what kills SMR performance.
rsync -a --info=progress2 "/mnt/Andi/Media/Library/TV/Some Show" /mnt/Media/library/tv/

# 3. restart the pipeline
docker start gatekeeper
```

Then in **Sonarr → Add New → Import Existing Series** (Radarr: **Import Existing Movies**), point
it at `/library/tv` and let it match the batch to TVDB. Jellyfin picks it up by itself.

Only delete the source batch once Sonarr shows the files as tracked.

Expect this to take a while: that drive sustains roughly 20–40 MB/s once its cache fills, so the
full 2.26 TiB is on the order of a week of elapsed copying. Batching means you can stop and
resume whenever you like.

---

## If something goes wrong

| Symptom | Fix |
|---|---|
| Torrents show "Missing files" after Phase 1 | Right-click → Force recheck. The data did not move. |
| Want to undo Phase 1 entirely | `zfs rename nvme-seed/data nvme-seed/torrents/complete` — instant, then restore the old volume paths. |
| Want to undo everything in Phase 1–2 | `zfs rollback nvme-seed@before-overhaul` and restore the config file you downloaded. |
| Gatekeeper will not start | `docker logs --tail 50 gatekeeper` — it names the exact problem. |
| Jobs stick at `transcoding` | Tdarr is not seeing `/data/transcode`, or is not stripping subtitles. Check the Tdarr queue. |
| Jobs fail with "did not import" | Sonarr/Radarr rejected the release. The files are still in `/data/work/<id>/` — import them by hand from Sonarr → Wanted → Manual Import. |
| A subtitle came out untranslated | Normal for a few lines — the gatekeeper keeps the English text rather than lose the styling. |
| Everything looks stuck | `curl -s http://192.168.50.45:5000/health` and `docker logs --tail 100 gatekeeper`. |

Nothing in this pipeline deletes a file to recover from a failure. If a job fails, its files are
still on disk and the reason is in `error_detail`.
