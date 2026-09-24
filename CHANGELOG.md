# BandwidthGoblin — Release Notes

> Renamed from **LinkBoost** in build 39. Every change bumps `BUILD` by 1;
> release tags are named `v2.0-buildN`.

## v2.0 build 43 — current

The merged line: everything proven in the old LinkBoost builds is included
here, rebuilt and branded **BandwidthGoblin**, in one lean file.

**What's in this version**

- **Uses every internet connection at once** - Wi-Fi + Ethernet + phone
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