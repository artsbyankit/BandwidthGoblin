<div align="center">

<img src="docs/logo.png" alt="BandwidthGoblin 👺" width="110"/>

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
- **[Marketing copy](docs/PROMO.md)** - ready-to-use description text

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
fly. Press `ESC` anytime to stop safely.

Everything lives in one file and uses only Python's built-in libraries -
nothing to install.

## When it goes feral

- 🌐 **60 GB Game Patches** - finish before your mates even open Discord.
- 🎬 **Fat ISOs & Movies** - high-throughput direct downloads without
  throttled single-stream limits.
- 📱 **Desperate Hotspotting** - combine sketchy hotel Wi-Fi with cellular
  data when neither works alone.
- 📦 **Batch Slurping** - feed it up to 8 links at once and let it hoard data
  in parallel.

## Features

- **Use every connection at once** - WiFi + phone hotspot together
- **Never restarts from zero** - interrupted? Run it again, it continues
- **Safe & double-checked** - the finished file is verified, not just saved
- **Works while you browse** - your normal internet keeps working
- **Straight to you** - the file goes from the website to your computer,
  never through a third-party server
- **Honest about limits** - if two connections share the same pipe, it still
  works safely, it just can't magically break physics (see below)

## The honest fine print

**BandwidthGoblin is in public beta (v2.0).** It works, but real-world
networking is a mess. Your feedback is what turns beta into 1.0.

- **Where the speed comes from:** two *actually different* networks (e.g.
  Fiber Wi-Fi + Mobile 5G) will unleash maximum speed. If both interfaces
  feed off the same capped home router, the goblin still works safely - it
  just won't magically break physics.
- **Signed URL Expiry:** if a link has a temporary token that expires
  mid-download, feed the goblin a fresh URL.
- Requires **Python 3.8+** (Windows users: install from python.org - skip the
  Microsoft Store dummy app).

## Repository layout

```
BandwidthGoblin/
├── bandwidthgoblin.py      # the whole tool - canonical copy (single file)
├── RUN.bat                 # Windows launcher (finds Python for you)
├── CHANGELOG.md
├── docs/
│   ├── USER_GUIDE.md       # friendly manual (plain language)
│   ├── USER_GUIDE.txt      # same manual, plain text
│   ├── TECHNICAL_GUIDE.md  # deep dive for engineers and curious friends
│   └── PROMO.md            # ready-to-use marketing copy
├── scripts/
│   ├── devbump.py          # release helper: bump build, sync packs, changelog
│   └── audit_dead.py       # AST dead-code checker used in CI-style audits
├── pack/
│   ├── windows/            # ready-to-use Windows pack (bandwidthgoblin.py + RUN.bat)
│   ├── mac/                # ready-to-use macOS pack (bandwidthgoblin.py + run.command)
│   └── linux/              # ready-to-use Linux pack (bandwidthgoblin.py + run.sh)
└── tests/
    ├── qa_server.py        # local ranged HTTP server for offline QA
    ├── qa_b29.py ...       # regression drivers, one per build milestone
    ├── qa_drive.py         # scenario driver (multi / resume / norange)
    ├── qa_tier2.py         # combined end-to-end suite
    └── big.src             # deterministic 4 MiB test payload
```

The four `bandwidthgoblin.py` copies (root + packs) are kept **byte-for-byte
identical** by `scripts/devbump.py` - the packs are just the tool ready to run
on each OS.