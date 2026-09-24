# BandwidthGoblin — Release Notes

> Renamed from **LinkBoost** in build 39. Every change bumps `BUILD` by 1;
> release tags are named `v2.0-buildN`.

## v2.0 build 47 — one dashboard, subtitles, and it follows your window (current)

**Changes in this version**

- **The btop-style dashboard is now the only UI.** The old one-line "simple"
  status display (and its code) is gone entirely - every run in a real
  terminal gets the full dashboard with per-link meters, chunk bars and the
  speed sparkline. No tty = clean lines, same as before.
- **The dashboard follows window resizes.** Drag the console bigger or smaller
  mid-download and the box, meters, sparkline and chunk bar re-layout to the
  new width every frame - no more clipping after a resize.
- **YouTube subtitles as .srt (best-effort).** After a video downloads, the
  tool checks for captions and drops **one** `<title> [<id>].srt` next to the
  file - the video's original language when the uploader provided a manual
  track, otherwise English (auto-generated), converted to srt via ffmpeg.
  Override with `MULTILINK_SUB_LANGS=hi.*`. Videos with no captions simply
  skip it.

**Everything from build 46 still here**

- YouTube multi-link really engages (googlevideo 302 redirects followed, so
  every network joins the download).
- ESC/Ctrl+C stop a split cleanly - resume saved, no fallback download, no
  crash; `STOP_USER` keeps "user stopped" apart from real failures.
- `_clean_tmp()` also removes stale `.progress` leftovers.

**Smart + hardened after real 2-and-3-link stress testing (build 47 audit)**

- **Faster multi-link start.** The initial range probes now run in parallel -
  one probe per link at the same time instead of one after another - so a
  googlevideo redirect chain is walked for every network before the first
  byte, not serially.
- **Probe-learned redirect cache.** The probe stores each link's resolved
  googlevideo node URL, so the first chunks skip the redirect hop entirely.
- **Size-aware chunk counts.** Instead of a fixed 16, the tool sizes chunks to
  ~4 MB each (4-64 clamp) once the file size is known - small files get few
  chunks, 500 MB videos get 64. Now the default for YouTube splits, `--queue`,
  `--jobs` and the interactive prompt (`[Enter = auto auto]`).
- **Fixed: a crash that could kill a run and lose the resume.** A link joining
  mid-run was appended to the link list outside its lock; if another link
  failed at that exact moment the run could die with "list changed size during
  iteration". All link-list reads/writes now share one lock.
- **Fixed: ESC/Ctrl+C on a YouTube split now *really* preserves progress.**
  Previously the partial file and the freshly-saved `.resume` were deleted
  right after saving (then "your progress is preserved" was printed anyway).
  Partials + resume now stay on disk, and a re-run resumes mid-stream even
  though googlevideo re-signs URLs on every extraction (size+chunk-plan match
  for split streams). Verified: stopped at 10/64 chunks (77.9 MB), re-ran,
  `resuming: skipping 10 done chunks`, finished and merged clean.
- **Fixed: expired per-link node URLs no longer stall a run to death.** When a
  googlevideo token expires mid-download (403), the tool now drops the cached
  node URL so it can pull the freshly-signed one instead of replaying the dead
  node until give-up.
- **Fixed: [P] pause longer than ~3 minutes no longer trips the stall
  watchdog** and force-stops the run.
- **Fixed: subtitles prefer the uploader's manual track** even when the video's
  language code is a region prefix of it (e.g. `en` video with an `en-US`
  manual track used to pick auto captions instead).
- **Honest logs:** stream lines say "size-aware chunks" (no more stale
  "16 chunks"), and auto-tune no longer claims it spawns 2-4 threads per link
  when it keeps one connection per link.
- **Fixed: a transient googlevideo 403 silently dropped the dashboard + all
  but one network.** YouTube's signed stream URLs are served from rotating
  edges, and one edge occasionally rejects the *very first* request (HTTP 403)
  while the next edge accepts it. The tool used to treat that as "link
  unreachable", abort the multi-link engine, and fall over to the plain
  single-connection yt-dlp download (no dashboard) - looking like "UI lost +
  one network". Now the preflight check retries 4xx/5xx with backoff, and if a
  stream still fails the tool **re-extracts fresh signed URLs once** and gives
  the multi-link engine another shot before downgrading. Verified: the same
  video that 403'd from one edge minutes later downloaded over all links.
- **Fixed (audit C1 follow-up): the dashboard could die mid-download.** The
  link-list lock was added to writes, but the dashboard still *read* the list
  three times per frame without it - a link joining at the wrong moment could
  still raise "list changed size during iteration" and silently kill the UI
  thread while the download kept going. The dashboard now takes one locked
  snapshot per frame and reuses it.
- **Fixed: a save-drive shortfall no longer drops you to a single-connection,
  no-dashboard yt-dlp download.** A video that is bigger than the free space
  on the save drive (e.g. a 36 GB 4K HDR video on a drive with 19 GB free)
  used to abort the multi-link engine and then fall back to plain yt-dlp -
  which needs the *same* bytes, so it would only fail partway (or fill the
  disk) with no UI and one network. The split now distinguishes "not enough
  disk space" and stops cleanly before anything is downloaded, telling you to
  free space, save elsewhere, or re-run with a smaller quality.
- **New: `--priority high` gives EVERY connection a higher tier.** YouTube
  learns one connection per link by default (per-IP throttle means more
  connections don't add bytes); this is the opt-in knob for hosts that don't
  throttle per source IP. Set `MULTILINK_PRIORITY=high` (or pass `--priority
  high`) and every link runs at 4 connections + maxed start caps in one go -
  all links, including ones that join mid-run. `normal` (default) keeps the
  tuned history-based 1-3. Explicit "chunks threads" args still win. The
  tier is visible live in the dashboard (`x{N}` per link) and in the logs
  ("priority tier: high - 4 connections/link on EVERY link").
- **New: YouTube "processing" phases show a loading animation.** The phases
  that run with no dashboard used to sit on a blank screen - reading the video
  info, the ffmpeg video+audio merge at the end, and the final chunk-integrity
  verify. Each now animates a one-line `|\ /|` spinner with the phase name and
  an elapsed-seconds counter, and the line clears itself so the next status
  prints cleanly. Terminal-only: silent under `--quiet` and when output is
  piped (batch/child processes), so logs stay clean.
- **Fixed: "why is it not using the dashboard?" - the format picker now
  prefers splittable (direct-HTTP) streams.** Some videos' "best available"
  format is a visionos HLS `m3u8` playlist (small segments, not
  range-requestable), so the multi-link engine correctly refused and fell back
  to plain single-connection yt-dlp with no dashboard. The quality presets
  (best/2160/1440/1080/720/480) now exclude `m3u8` formats first and pick the
  best direct-https stream - which the engine CAN split over all your links -
  keeping the old selector as a fallback for videos that only offer HLS. The
  fallback line also explains itself when it does happen.

## v2.0 build 46 — YouTube really uses every connection now

**Fixes in this version**

- **YouTube multi-link actually kicks in.** Build 45 promised "all your
  networks" for YouTube, but googlevideo answers a range probe from a second
  network with an `HTTP 302` redirect to that network's nearest node. Build 45
  treated the redirect as a failure and dropped the link, so the split
  silently fell back to one slow connection (`0.8 MB/s`, single stream).
  Build 46 **follows the redirect** during the probe, so every link that can
  serve ranges joins the download - video + audio streams download over all
  your connections at once, exactly like the build-45 promise.
- **The btop-style dashboard is back on Windows.** On a plain `cmd.exe` /
  conhost console the dashboard had been auto-detected as the one-line
  "simple" mode (the full terminal UI was reserved for Windows Terminal and
  friends). Build 46 asks the console API to enable ANSI VT and shows the
  full live dashboard there too - every link, every chunk, live speeds.
- **ESC stops a YouTube split cleanly.** Pressing ESC during a stream used to
  fall through into the yt-dlp fallback download (with `STOP` still armed -
  Ctrl+C turned it into a crash). ESC/Ctrl+C now stop the run properly: the
  partial download is cleaned up, resume data is saved, and it exits with
  "stopped - your progress is preserved." - no surprise background download,
  no traceback.

**Everything from build 45 still here**

- YouTube split downloads over every connection, then merges with ffmpeg.
- One-time intro, `help`/`menu` at prompts, `[H]` help overlay in the dashboard.
- Verified `DONE` line, resume files, and crash-safe behavior.

## v2.0 build 45 — YouTube now uses all your networks

**What's new in this version**

- **YouTube downloads split over every connection** - a YouTube video is no
  longer downloaded by yt-dlp alone: BandwidthGoblin pulls the raw video and
  audio streams through its own multi-link engine (all your networks at once,
  16 chunks, resumable) and merges them back into one file with ffmpeg. YouTube
  gets the same full treatment as any other link.
- **Still dead simple** - paste a link, pick a quality (`Enter` = best,
  `1080` / `720` / `audio` ...), done. Streaming keys `[P]` / `[R]` / `[ESC]`
  now work during YouTube downloads too.
- **No guide needed, by design** - the first prompt shows a one-time
  "how to drive this" intro, every download dashboard shows the live keys
  (`[P] pause  [R] resume  [ESC] stop safely  [H] help`), pressing **H**
  flashes a help overlay right in the dashboard, and typing **help** at any
  prompt prints the feature list.
- **More information while it works** - starting a YouTube download names the
  engines in use and ends with a verified `DONE` line (path + size).
- **Bug fixes** - `--limit` / `--links` / `--cookie` / `--referer` / `--sha`
  now also apply to YouTube split downloads; leftover temp stream files are
  cleaned up on failure or stop; YouTube filenames handle Windows-forbidden
  trailing dots/spaces and reserved names (`CON`, `NUL`, ...); unexpected
  stream shapes fall back to plain yt-dlp instead of failing.

**Everything from build 44 still here**

- **YouTube downloads, built in** - paste any `youtube.com` / `youtu.be`
  link and it downloads itself; pick `2160` / `1440` / `1080` / `720` /
  `480` / `audio`, or one-line `--yt <link> 1080`
- **Powered by proven open source** - **yt-dlp** engine + **deno** runtime +
  **ffmpeg**, all auto-fetched once into `tools/`
- **First stable release**

## v2.0 build 44 — first stable release

The beta label comes off, and YouTube joins the feast.

**What's new in this version**

- **YouTube downloads, built in** - paste any `youtube.com` / `youtu.be`
  link (watch, Shorts, or Music) and it downloads itself:
  - `best` (default) = the highest quality the video offers
  - pick a height: `2160` / `1440` / `1080` / `720` / `480`
  - or `audio` for just the soundtrack (MP3 when possible)
  - one-line form: `bandwidthgoblin.py --yt <link> 1080`
- **Powered by proven open source** - the engine is **yt-dlp** (the famous,
  actively maintained YouTube downloader, ~190k ⭐ on GitHub), with **deno**
  as the JS runtime YouTube extraction requires and **ffmpeg** to merge
  full-quality video + audio into one file. BandwidthGoblin downloads all
  three for you on first use (one time, cached in a `tools/` folder) - no
  install steps, no accounts.
- **First stable release** - `BETA` is off; `--version` reports
  `v2.0 build 44` with no beta label.

**Everything from build 43 still here**

- Uses **every internet connection at once** - Wi-Fi + Ethernet + phone
  tether/hotspot download the same file together over all links
- **Never restarts from zero** - interrupted downloads resume where they
  stopped
- **Safe & double-checked** - finished files are verified (size + file
  signature + SHA-256 when a hash is known) before reporting DONE
- **Live dashboard** - per-connection speed, combined speed, progress,
  pause / resume / stop keys
- **Hot-plug friendly** - plug a phone in mid-download and it joins
  automatically; unplug one and nothing breaks
- **Honest about speed** - genuinely different networks multiply speed; two
  links to one capped router still work safely, just without the magic
- **Loopback/link-local ignored** by default (not separate physical routes);
  `BANDWIDTHGOBLIN_KEEP_LOOPBACK=1` opts back in for LAN-loopback testing
- Works on Windows, macOS, and Linux; requires **Python 3.8+** (standard
  library only, nothing to install)
- Licensed under the **BandwidthGoblin License v1** - non-commercial,
  open-source only, no warranty (see `LICENSE`)

## Earlier highlights

- **build 42** - speedtest now samples every live link (256 fine chunks)
- **build 41** - encoding-safe console and log output everywhere
- **build 39** - renamed from LinkBoost; BandwidthGoblin v2.0
- **build 1** - everything proven so far: multi-link bonding, work stealing,
  hot-plug, resume, live dashboard

The full build-by-build history is preserved in git; it was removed from this
file to keep the project quiet and user-friendly.