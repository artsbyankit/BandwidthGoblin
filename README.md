# LinkBoost

> **One download. All your internet. Done faster.**

LinkBoost is a downloader with a superpower: it can use **more than one internet
connection at the same time**. If you have your home WiFi *and* your phone's
mobile hotspot, LinkBoost splits the file between them so it arrives much
faster than either connection could manage alone.

No accounts. No cloud. No subscription. Just a single file that runs on
Windows, macOS, and Linux.

- **[Friendly guide](docs/USER_GUIDE.md)** - how to use it, plain language
- **[Technical guide](docs/TECHNICAL_GUIDE.md)** - how it works under the hood
- **[Marketing copy](docs/PROMO.md)** - ready-to-use description text

---

## What it does

Most downloads travel over a single path, like a highway with one lane open.
LinkBoost opens **every lane you have** at once:

- Paste the link to a big file (a game, a movie, a large update).
- LinkBoost finds every internet connection on your computer -
  LAN cable, WiFi, USB-tethered phone, hotspot.
- It downloads the file using **all of them together**, then double-checks
  that the finished file is complete and correct.

Think of 100 boxes arriving from a warehouse. One delivery van takes all day.
LinkBoost grabs every van you own and splits the boxes between them.

## Quick start

```
# Windows - double-click RUN.bat          (or:)
py linkboost.py

# macOS - double-click run.command        (or:)
python3 linkboost.py

# Linux - double-click run.sh             (or:)
python3 linkboost.py
```

Then: **copy a download link**, paste it in, pick where to save, and watch it
fly. Press `ESC` anytime to stop safely.

Everything lives in one file and uses only Python's built-in libraries -
nothing to install.

## Features

- **Use every connection at once** - WiFi + phone hotspot together
- **Never restarts from zero** - interrupted? Run it again, it continues
- **Safe & double-checked** - the finished file is verified, not just saved
- **Works while you browse** - your normal internet keeps working
- **Straight to you** - the file goes from the website to your computer,
  never through a third-party server
- **Honest about limits** - if two connections share the same pipe, it still
  works safely, it just can't double the speed (see below)

## The honest fine print

**LinkBoost is new and in beta.** It is built on a deep test suite, but real
life has surprises. Your feedback is what turns beta into 1.0.

- The speed gain depends on your setup. Two *genuinely separate* connections
  (e.g. broadband WiFi + a phone hotspot on a different network) give the
  biggest gain.
- If both links share the same bottleneck, the download still completes
  safely, just without the speed boost.
- Token/signed links expire - copy a fresh one.

Requires **Python 3.8+** (Windows users: install from python.org - the
Microsoft Store "python" shortcut does not work).

## Repository layout

```
LinkBoost/
├── linkboost.py            # the whole tool - canonical copy (single file)
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
│   ├── windows/            # ready-to-use Windows pack (linkboost.py + RUN.bat)
│   ├── mac/                # ready-to-use macOS pack (linkboost.py + run.command)
│   └── linux/              # ready-to-use Linux pack (linkboost.py + run.sh)
└── tests/
    ├── qa_server.py        # local ranged HTTP server for offline QA
    ├── qa_b29.py ...       # regression drivers, one per build milestone
    ├── qa_drive.py         # scenario driver (multi / resume / norange)
    ├── qa_tier2.py         # combined end-to-end suite
    └── big.src             # deterministic 4 MiB test payload
```

The four `linkboost.py` copies (root + packs) are kept **byte-for-byte
identical** by `scripts/devbump.py` - the packs are just the tool ready to run
on each OS.