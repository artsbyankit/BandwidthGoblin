<div align="center">

# 👺 BandwidthGoblin

> **Feed the goblin. Get your files faster.**
>
> Feeds on every internet connection your machine has. Zero cloud, zero
> accounts, pure feral speed.

</div>

BandwidthGoblin is a downloader with a superpower: it **scavenges every active
internet connection your computer owns** — home Wi-Fi, Ethernet cable, 5G
phone tether, hotel guest network — and stitches them into a single
hyper-threaded download. That game patch or ISO that crawls on one connection
drops to a fraction of the time.

Single-lane downloading is for cowards. Paste link. Release the goblin. Watch
it fly.

No accounts. No telemetry. No subscription bloatware. One lightweight script,
100% Python standard library, nothing to install. Works on Windows, macOS, and
Linux.

- **[Friendly guide](docs/USER_GUIDE.md)** - how to use it, plain language
- **[Technical guide](docs/TECHNICAL_GUIDE.md)** - how it works under the hood

---

## What it does

Most downloads travel over a single path, like a highway with one lane open.
BandwidthGoblin opens **every lane you have** at once:

- Paste the link to a big file (a game, a movie, a large update).
- BandwidthGoblin finds every internet connection on your computer -
  LAN cable, WiFi, USB-tethered phone, hotspot.
- It downloads the file using **all of them together**, splits it into
  chunks, pulls them in parallel, then stitches the file back together with
  zero corruption.

Imagine ordering 100 packages online. Normal downloaders send one delivery
driver to fetch them one by one while three other delivery vans sit idle in
your driveway. BandwidthGoblin hijacks all four vans at once.

## Quick start

> **First feed the goblin: it runs on Python.** Grab the free installer from
> 👉 **[python.org/downloads](https://www.python.org/downloads/)** — on
> Windows, tick **“Add python.exe to PATH”** during setup, then come back here.

```
# Windows - double-click RUN.bat          (or:)
py bandwidthgoblin.py

# macOS - double-click run.command        (or:)
python3 bandwidthgoblin.py

# Linux - double-click run.sh             (or:)
python3 bandwidthgoblin.py
```

Then: **copy a download link**, paste it in, pick where to save, and watch it
fly. During a download: `[P]` pause, `[R]` resume, `[ESC]` stop safely, `[H]`
in-dashboard help. Type **help** at any prompt for the feature list - no guide
needed.

### YouTube videos

YouTube is built in. Paste any YouTube link and tell it the quality:

```
# one line, paste your link and pick a quality:
bandwidthgoblin.py --yt https://www.youtube.com/watch?v=XXXX 1080

# or just paste the link into the interactive prompt:
bandwidthgoblin.py
> (paste the YouTube link)          -> auto-detected
> quality? (Enter = best / 2160 / 1440 / 1080 / 720 / 480 / audio)
> save folder?                      -> video title is used as the file name
```

`best` (the default) downloads the highest quality the video offers
(2160p/1440p/1080p when available). `1080`, `720`, `480` cap it to that
height. `audio` saves just the soundtrack (MP3 when possible).

YouTube downloads use the **same all-networks engine as any other link**:
the video and audio streams are split over every connection you have, pulled
in parallel, then merged into one file with ffmpeg. `[P]` / `[R]` / `[ESC]`
work during YouTube downloads too.

> The very first YouTube download quietly fetches the yt-dlp engine plus a JS
> runtime and ffmpeg (~130-190 MB, one time, cached in a `tools/` folder next
> to the script). After that, YouTube downloads are fully offline. You need an
> internet connection for that first setup, obviously.

Everything lives in one file and uses only Python's built-in libraries -
nothing to install (the YouTube engine files are the one exception, and
BandwidthGoblin downloads them for you automatically).

## When it goes feral

- 🌐 **60 GB Game Patches** - finish before your mates even open Discord.
- 🎬 **Fat ISOs & Movies** - high-throughput direct downloads without
  throttled single-stream limits.
- 🎥 **YouTube Videos** - paste any YouTube link, download it at best quality
  or a resolution you pick (1080p, 720p, ...) - even audio-only - split over
  **every connection you have**, exactly like a normal link.
- 📱 **Desperate Hotspotting** - combine sketchy hotel Wi-Fi with cellular
  data when neither works alone.
- 📦 **Batch Slurping** - feed it up to 8 links at once and let it hoard data
  in parallel.

## Features

- **Use every connection at once** - WiFi + phone hotspot together
- **Never restarts from zero** - interrupted? Run it again, it continues
- **Safe & double-checked** - the finished file is verified, not just saved
- **Works while you browse** - your normal internet keeps working
- **YouTube built in** - paste a youtube.com / youtu.be link, it grabs the
  video at the best quality it offers - or your pick (2160 / 1440 / 1080 /
  720 / 480 / audio-only) - split over **all your connections** like any
  other link
- **Self-explanatory** - a one-time "how to drive this" intro at the first
  prompt, the download dashboard always shows its keys
  (`[P] pause  [R] resume  [ESC] stop  [H] help`), and typing **help** at any
  prompt prints the feature list
- **Straight to you** - the file goes from the website to your computer,
  never through a third-party server
- **Honest about limits** - if two connections share the same pipe, it still
  works safely, it just can't magically break physics (see below)

## The honest fine print

**BandwidthGoblin v2.0 build 47 is the current stable release.** It works -
the beta label is off.

- **Where the speed comes from:** two *actually different* networks (e.g.
  Fiber Wi-Fi + Mobile 5G) will unleash maximum speed. If both interfaces
  feed off the same capped home router, the goblin still works safely - it
  just won't magically break physics.
- **YouTube:** downloads work without a Google login or cookies. If a video
  shows "Sign in to confirm you are not a bot", it's region/age-locked -
  watch it once in a browser first, or pick a different video.
- **Signed URL Expiry:** if a link has a temporary token that expires
  mid-download, feed the goblin a fresh URL.
- **Loopback/link-local are ignored** by default (not separate physical
  routes); set `BANDWIDTHGOBLIN_KEEP_LOOPBACK=1` to include them for
  LAN-loopback testing.
- Requires **Python 3.8+** (Windows users: install from python.org - skip the
  Microsoft Store dummy app).

## Repository layout

```
BandwidthGoblin/
├── bandwidthgoblin.py      # the whole tool - canonical copy (single file)
├── RUN.bat                 # Windows launcher (finds Python for you)
├── CHANGELOG.md
├── LICENSE                 # non-commercial, open-source-only license
├── docs/
│   ├── USER_GUIDE.md       # friendly manual (plain language)
│   └── TECHNICAL_GUIDE.md  # deep dive for engineers and curious friends
└── pack/
    ├── windows/            # ready-to-use Windows pack (bandwidthgoblin.py + RUN.bat)
    ├── mac/                # ready-to-use macOS pack (bandwidthgoblin.py + run.command)
    └── linux/              # ready-to-use Linux pack (bandwidthgoblin.py + run.sh)
```

The four `bandwidthgoblin.py` copies (root + packs) are kept **byte-for-byte
identical** - the packs are just the tool ready to run on each OS.

## Open-source credits

YouTube downloads are powered by three open-source projects - BandwidthGoblin
downloads them for you (once) and gives full credit:

- **[yt-dlp](https://github.com/yt-dlp/yt-dlp)** - the famous, battle-tested
  YouTube downloader engine (The Unlicense / public domain). 190k+ ⭐ on
  GitHub, updates every couple of weeks to keep up with YouTube. BandwidthGoblin
  is a friendly face on top of it: yt-dlp figures out which streams a video
  has, BandwidthGoblin pulls them down over all your connections at once, and
  FFmpeg merges them back together.
- **[deno](https://github.com/denoland/deno)** - the JS runtime YouTube
  extraction needs (MIT license).
- **[FFmpeg](https://ffmpeg.org/)** - merges high-quality video + audio
  streams into one file (LGPL/GPL licensed, official builds from
  [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) on Windows).

Big respect to those maintainers - without them this one-file tool couldn't
offer YouTube downloads at all.

## License

BandwidthGoblin is released under the **BandwidthGoblin License v1** (see
[LICENSE](LICENSE)) — **non-commercial and open-source only**:

- You may **not sell** the software, or anything that depends on it.
- Any project that uses it **must be open source** and released under this
  same license.
- The software is provided **"as is"** with **no warranty and no liability** —
  use it entirely at your own risk; the author is not responsible for any
  harm or damage caused by its use.