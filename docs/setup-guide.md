# Server overhaul — step by step

A runbook for rebuilding the pipeline described in `architecture.md`. Written to be followed
in order, by someone who is not a programmer. Every command is copy-paste.

**Your seeding torrents are safe.** The storage reshape is done with a ZFS rename, which changes
a label, not data. No torrent file is copied, moved on disk, or re-checked. The container path
qBittorrent knows them by (`/complete/...`) stays exactly the same.

Where to run commands: **TrueNAS web UI → System → Shell** (the `>_` icon). Run `sudo -i` once
when you open it — you log in as `truenas_admin`, and every command here needs root.

Total time: about 2 hours of attention, spread over as many evenings as you like. Each phase
ends with the system working.

---

## The one rule that makes or breaks this

**Every app in the chain must run as the same user.** qBittorrent downloads a file, the
gatekeeper hardlinks it, Tdarr rewrites it, Sonarr moves it. If they run as different users,
Linux quietly refuses some of those steps — and "quietly" is the problem. Linux also blocks
hardlinking a file you do not own, which is the single thing this whole design rests on.

Your apps run as **`568:568`** — the TrueNAS apps user. Sonarr and Radarr are official apps, so
rather than reconfiguring them, we move the two custom apps (qBittorrent and the gatekeeper) to
568 and chown the data to match. Nothing official gets touched.

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

Whatever numbers (c) shows are the user everything else gets aligned to. On this server it is
`568 568`.

If (e) prints nothing, say so before Phase 4 and we will use a different hook command.

### 3. What these checks showed on this server (14 Sep 2026)

- Hardlinks: **not working** — every import stored a second full copy.
- `arr-ingest` **392 G** (movies 345 G, anime/tv 44.9 G, tv 1.75 G), `torrents/complete` 470 G,
  `tdarr-cache` 224 K (already empty), **74.3 G free**.
- Sonarr and Radarr run as **568:568**.
- qBittorrent has `curl`.

Phase 0.5 below exists because of the first two.

### 4. Find out what is sitting in the ingest folders

A *root folder* is the shelf Sonarr puts shows on — every show belongs to exactly one. You have
four: two in the fast NVMe scratch area (`/nvme/arr-ingest/...`) and two in the real library
(`/library/...`). The old design added new shows to the scratch shelf and relied on the translator
script to carry them to the library afterwards. That script could never see your files, so that
step has most likely never run.

This matters because Phase 2 removes the scratch shelves and destroys that dataset. Anything still
on it would be deleted with it.

```sh
du -sh /mnt/nvme-seed/arr-ingest
du -sh /mnt/nvme-seed/arr-ingest/*/* 2>/dev/null | sort -h | tail -20
```

**If the total is a few MB** — empty folders. Nothing to do.

**If it is gigabytes** — that is real content, and moving it is a Phase 5-sized job, not a warm-up.
Do not try to move it now. Instead:

- Leave the scratch shelves where they are; they harm nothing.
- Stop *new* content landing there (next step).
- Skip the "delete the ingest roots" step in Phase 2, and do not destroy `nvme-seed/arr-ingest`.
- Drain it in Phase 5, in batches, alongside the old library.

### 5. Point Jellyseerr at the library shelves

So that nothing new lands in the scratch area from here on:

- **Jellyseerr → Settings → Services → Sonarr → Edit**: Root Folder `/library/tv`,
  Anime Root Folder `/library/anime/tv`.
- **Jellyseerr → Settings → Services → Radarr → Edit**: Root Folder `/library/movies`.

This one change is the only part of the ingest-folder question that has to happen before Phase 1.

---

## Phase 0 — quick wins (20 minutes, no restructuring)

These stand alone. Do them tonight even if you go no further.

1. **Rotate the Radarr API key.** Radarr → Settings → General → API Key → the circular arrow.
   It was legible in the screenshots you sent. Afterwards update it in Prowlarr, Bazarr and
   Jellyseerr, which all store a copy.
2. **Protect your subtitles until the new pipeline lands.** Tdarr → Libraries → Media → Source →
   turn **Hold Files After Scanning** on (the 1 hour duration is already set). This is currently
   the only thing preventing Tdarr from stripping an English track before anything extracts it.
3. **Tighten the two public hostnames — the parts that take five minutes.**

   First, the framing: Jellyfin and Jellyseerr behind Certbot TLS, each with its own login, is a
   normal self-hosted setup. Worth improving, not worth panicking about, and it should not hold up
   the overhaul.

   **Do not put an NPMplus Access List on `watch.pandi.se` or `request.pandi.se`.** An access list
   is HTTP Basic Auth in front of the whole site. TV apps, phone apps, Roku and Chromecast cannot
   answer a basic-auth challenge, so every remote Jellyfin user would stop being able to connect.
   Both apps already authenticate — access lists are the right tool only for something you expose
   that has *no* login of its own.

   What is actually a five-minute job, per proxy host in NPMplus:
   - **Block Common Exploits** → on
   - **Force SSL** → on, plus **HSTS** if your version offers it on the SSL tab

   In Jellyfin → Dashboard → Users, check whether your build offers a per-user lockout after
   repeated failed logins and set it. Keep Jellyfin updated, and do not let the admin account be
   the one people watch with.

   **Deliberately not here:** CrowdSec and GeoIP country filtering are what genuinely stop
   brute-force attempts, but in NPMplus neither is a UI toggle. CrowdSec needs a separate CrowdSec
   container, `LOGROTATE="true"` in the compose, and an API key from
   `docker exec crowdsec cscli bouncers add npmplus` written into
   `/opt/npmplus/crowdsec/crowdsec.conf`. GeoIP needs `NGINX_LOAD_GEOIP2_MODULE="true"` plus
   hand-written `geoip2` directives in `/opt/npmplus/custom_nginx/http_top.conf`. Both are config-file
   projects worth doing on their own, once the pipeline is finished — not mixed into a storage
   migration.

---

## Phase 0.5 — free the space (one evening, mostly unattended)

**Do this before Phase 1.** `arr-ingest` holds 392 GiB of media that Sonarr and Radarr already
track but Jellyfin cannot see. Moving it to the library frees the space that makes everything
else comfortable, and takes the pool from 92 % to roughly 50 %.

**Use the *arr bulk editors, not rsync.** These files are in Sonarr's and Radarr's databases.
Their own editors move the files *and* update the database in one action. (rsync is the right
tool for the old `Andi` library in Phase 5, where nothing tracks the files — it is the wrong tool
here.)

### Sonarr — two passes, because anime has its own shelf

1. Series → switch on the **Mass Editor** (select mode) at the bottom of the list.
2. Sort by **Path** so the `/nvme/arr-ingest/anime/tv/...` shows group together. Select them.
3. At the bottom set **Root Folder** → `/library/anime/tv` → **Apply**.
4. When it asks whether to move the files, say **yes**.
5. Repeat for the shows under `/nvme/arr-ingest/tv` → `/library/tv` (only 1.75 GiB, quick).

### Radarr — one pass, and this is the big one

Movies → **Movie Editor** → sort by Path → select everything under `/nvme/arr-ingest/movies` →
**Root Folder** → `/library/movies` → **Apply** → yes, move the files.

That is 345 GiB going to the SMR disk. Expect it to run for several hours — start it and leave it.
Nothing else should be writing to that disk while it works.

### When both are empty

```sh
du -sh /mnt/nvme-seed/arr-ingest
zfs destroy -r nvme-seed/arr-ingest
zfs list -o name,used,avail -r nvme-seed
```

You should now have roughly 466 GiB free. Also delete the two ingest root folders in Sonarr and
Radarr (Settings → Media Management → Root Folders) — they are empty now, so the Phase 2 warning
about them no longer applies.

**One thing to decide, and it is fine to skip:** some of that backlog is x265 rather than AV1.
Converting it is a separate one-off job you can point Tdarr at later, against `/library`. It is
not part of getting the new pipeline working, and the new pipeline never revisits old files.

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
chown -R 568:568 /mnt/nvme-seed/data
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
    environment:
      - PUID=568
      - PGID=568
      - WEBUI_PORT=8080
    volumes:
      - /mnt/fast-pool/qbittorrent/config:/config
      - /mnt/nvme-seed/data/torrents/complete:/complete
      - /mnt/nvme-seed/torrents/incomplete:/incomplete
      - /mnt/nvme-seed/data:/data
```

The `PUID`/`PGID` change is what puts qBittorrent on the same user as everything else. Its config
needs to follow:

```sh
chown -R 568:568 /mnt/fast-pool/qbittorrent /mnt/nvme-seed/torrents/incomplete
```

The `/complete` line is the important one: the host path changed, the container path did not.
Every seeding torrent still finds its files exactly where it left them.

Start qBittorrent. **Check that your critical torrents still show as Seeding, not Errored.**
If any show "Missing files", right-click → Force recheck — the data is there, it just needs to
look again.

### 7. Repoint the other apps' `/complete` mount

Sonarr, Radarr and Tdarr will **refuse to start** until you do this, with
`bind source path does not exist: /mnt/nvme-seed/torrents/complete` in
`/var/log/app_lifecycle.log`. That is expected - their config still points at the old
location.

For each of **Sonarr**, **Radarr** and **Tdarr**: Apps -> Edit -> Storage -> find the entry
whose container path is `/complete` and change only its **host path**:

| | |
|---|---|
| old | `/mnt/nvme-seed/torrents/complete` |
| new | `/mnt/nvme-seed/data/torrents/complete` |

Leave the container path as `/complete`, and leave every other mount alone for now. Save,
and they start.

Then in **Sonarr and Radarr -> Settings -> Download Clients -> Remote Path Mappings**,
delete the entry (`/complete/` to `/nvme/torrents/complete/`). It pointed at the old
location through the `/nvme` mount; now that qBittorrent and the *arr apps both call the
folder `/complete`, the paths match on their own and that mapping would send imports
somewhere that no longer exists.

### 8. Fix the SMB shares that pointed at the old paths

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
| `/nvme` - **only after Phase 0.5 drains `arr-ingest`** | (keep `/library` → `/mnt/Media/library`) |

**Do not remove `/nvme` while `arr-ingest` still holds content.** Sonarr and Radarr reach
those files through that mount; remove it early and every show and movie stored there goes
missing in their libraries. If Phase 0.5 is not finished, add `/data` now and leave `/nvme`
and `/complete` where they are.

Leave the User ID and Group ID alone — they are already 568, which is what everything else now matches.

### Bazarr

Change its one storage entry from `/Media/library` to **`/library`** → `/mnt/Media/library`.
Leave its user/group as they are.

### Tdarr

| Remove | Add |
|---|---|
| `/ingest`, `/complete`, `/tdarr-cache`, `/Library`, `/dont_media` | `/data` → `/mnt/nvme-seed/data` |

Set the **Tdarr Transcode Storage** host path to `/mnt/nvme-seed/data/cache`. Leave its
user/group as they are.

### Now delete the mappings that exist only because paths differed

- **Sonarr → Settings → Download Clients → Remote Path Mappings** — delete the entry.
- **Radarr → Settings → Download Clients → Remote Path Mappings** — delete the entry.
- **Bazarr → Settings → Sonarr → Path Mappings** — delete all rows. Same under **Settings → Radarr**.
- **Sonarr → Settings → Media Management → Root Folders** — delete `/nvme/arr-ingest/tv` and
  `/nvme/arr-ingest/anime/tv`, **but only if the Prep check showed they are empty.** If they still
  hold shows, leave them until Phase 5 has drained them. Either way, keep `/library/tv` and
  `/library/anime/tv`.
- **Radarr → same** — the two `/nvme/arr-ingest/...` roots, under the same condition.

Start all the apps. Bazarr → System → Status should now show **no path errors**.

### Clean up the empty datasets

Once everything is running and happy:

```sh
zfs destroy nvme-seed/tdarr-cache
```

`nvme-seed/arr-ingest` was already destroyed at the end of Phase 0.5.

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
chown -R 568:568 /mnt/fast-pool/gatekeeper
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

Apps → **Discover Apps** → use the **Install via YAML / custom config** option (not the
Custom App *form* — that form cannot express the `command:` line below). Name it `gatekeeper`
and paste this exactly:

```yaml
services:
  gatekeeper:
    image: python:3.12-slim
    container_name: gatekeeper
    restart: unless-stopped
    ports:
      - "5000:5000"
    environment:
      - AI_TRANSLATOR_HOME=/config
    volumes:
      - /mnt/fast-pool/gatekeeper:/config
      - /mnt/nvme-seed/data:/data
    command: >
      bash -lc "apt-get update &&
                apt-get install -y --no-install-recommends ffmpeg &&
                pip install --no-cache-dir -r /config/requirements.txt &&
                exec setpriv --reuid=568 --regid=568 --clear-groups python /config/ai_translator.py"
```

**`image:` must stay `python:3.12-slim`.** It is tempting to put a GitHub URL there, but Docker
does not clone repositories — it would fail with `invalid reference format`. The image is just a
minimal Linux with Python in it. Your code reaches the container through the *volume*:
`/mnt/fast-pool/gatekeeper` appears inside as `/config`, and the last line runs
`/config/ai_translator.py` from there. Nothing points at GitHub once the files are downloaded.

**Note what the last line does.** The container starts as root, because installing packages needs
root. `setpriv` then drops to user 568 before starting the service, so the Python process — and
every file and hardlink it creates — belongs to the same user as Sonarr and Radarr. Setting
`user: "568:568"` on the container instead would look tidier and fail immediately: a non-root user
cannot run `apt-get`.

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

**Drain the ingest scratch area the same way** if the Prep check found content there. It is the
same job with a shorter path — `rsync` a batch from `/mnt/nvme-seed/arr-ingest/tv/...` into
`/mnt/Media/library/tv/`, re-import it in Sonarr, delete the source batch. Once it is empty you can
remove the ingest root folders and run `zfs destroy nvme-seed/arr-ingest`.

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
| Gatekeeper will not start | `docker logs --tail 50 gatekeeper`, and `tail -20 /var/log/app_lifecycle.log` if no container was even created. |
| `invalid reference format: repository name must be lowercase` | A URL went into `image:`. It must be `python:3.12-slim`; the code comes from the volume mount, not from GitHub. |
| Container exits immediately after `apt-get` | The container must start as root. Do not add `user:` to the compose — `setpriv` in the command drops privileges instead. |
| Jobs stick at `transcoding` | Tdarr is not seeing `/data/transcode`, or is not stripping subtitles. Check the Tdarr queue. |
| Jobs fail with "did not import" | Sonarr/Radarr rejected the release. The files are still in `/data/work/<id>/` — import them by hand from Sonarr → Wanted → Manual Import. |
| A subtitle came out untranslated | Normal for a few lines — the gatekeeper keeps the English text rather than lose the styling. |
| Everything looks stuck | `curl -s http://192.168.50.45:5000/health` and `docker logs --tail 100 gatekeeper`. |

Nothing in this pipeline deletes a file to recover from a failure. If a job fails, its files are
still on disk and the reason is in `error_detail`.
