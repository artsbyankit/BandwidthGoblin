# BandwidthGoblin — Release Notes

> Renamed from **LinkBoost** in build 39. Every change bumps `BUILD` by 1;
> release tags are named `v2.0-buildN`.

## v2.0 build 44 — first stable release (current)

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