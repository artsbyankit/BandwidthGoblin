# 👺 BandwidthGoblin — User Guide

**Downloads one file using all your internet connections at once** (Wi-Fi +
Ethernet + phone hotspot), so big downloads finish faster. Free, no account,
no installs — just one Python file.

---

## Before you start

You need **Python 3.8 or newer**:

- Get the free installer from **[python.org/downloads](https://www.python.org/downloads/)**.
- On Windows, tick **"Add python.exe to PATH"** during setup.

To make the most of BandwidthGoblin, you can also:

- **Plug in your phone** with a USB cable and turn on **USB tethering**
  (Android: *Settings → Network → USB tethering*; iPhone: *Personal Hotspot
  ON*, then **Trust** the computer).
- Or turn on a **Wi-Fi hotspot** from the phone.

Each *different* internet connection the computer sees = one more lane for
the download. Two lanes to the same router (e.g. Wi-Fi + LAN cable on one
home router) still works safely — it just won't double your speed.

---

## Downloading, step by step

1. **Copy a download link** (like a game, movie, or ISO download URL).
2. **Double-click the launcher** for your system:
   - Windows: `RUN.bat`
   - macOS: `run.command`
   - Linux: `run.sh`
3. The app **offers your copied link** — press **Enter** to use it.
4. Choose a **save spot** — press **Enter** to accept the suggested folder and
   file name. (You can type your own, like `D:\Games\patch.zip` — but don't
   save straight to a drive root like `D:\`.)
5. On the tuning question, press **Enter** for automatic (best choice).
6. **Watch it fly.** You'll see the combined speed, each connection's speed,
   the progress bar, and how long it's been running.
7. Press **ESC** anytime to stop — **your progress is saved**. Run the same
   link again later and it continues where it left off.

When it finishes you'll see `DONE`, the file size, and the time it took. The
finished file is **checked automatically** (size + file type + SHA-256 when
available) so you know it's not corrupt.

---

## Reading the screen

- The big **▼ MB/s** number is your **combined speed** across all connections.
- Each connection gets its own row: **green ●** = working, **cyan ●** = new,
  **red ●** = offline (lost connection).
- The progress bar shows overall percent. The bar of `#` / `>` / `.` marks
  finished / active / waiting chunks.
- On older consoles you get a simple one-line status instead — that's normal.

---

## Good to know

- **Plug in / unplug phones any time** — new connections join automatically;
  lost ones don't break anything.
- **Stopping is always safe** — ESC or Ctrl+C keeps your progress; rerun to
  continue.
- **Expired or token links** (some file hosts) fail fast — just copy a fresh
  link.
- **Speed depends on your internet** plus how nice the server is. If a site
  throttles (like some free file hosts), BandwidthGoblin automatically uses
  fewer workers and waits politely — that's the fastest safe option there.
- The tool writes a **white log** of each download into a `logs/` folder next
  to the script (first line shows the build number).

---

## Optional commands

Open a terminal in the folder and run:

| Command | What it does |
|---|---|
| `py bandwidthgoblin.py --help` | Shows every option |
| `py bandwidthgoblin.py --selftest` | Quick self-check that everything works |
| `py bandwidthgoblin.py --dry-run <URL>` | Inspects a link — size, type, speed — *without* downloading |
| `py bandwidthgoblin.py --speedtest <URL>` | Measures each connection's real speed |
| `py bandwidthgoblin.py --queue list.txt` | Downloads many links from a text file (one URL per line) |
| `py bandwidthgoblin.py --links 0,1` | Combine only specific connections (listed as `[0]`, `[1]`... at startup) |
| `py bandwidthgoblin.py --limit 5` | Cap the combined speed at 5 MB/s |
| `py bandwidthgoblin.py URL --out-dir D:\dl` | Download one link straight into a folder, no questions |

During a download: press **P** to pause, **R** to resume, **ESC** to stop.

---

## If something looks wrong

| What you see | What it means | What to do |
|---|---|---|
| "Link is broken" (404 / 403) | The link expired or the host error'd | Copy a fresh link |
| Only 1 working connection | Extra connections can't reach the host | Check the hotspot / plug in the phone |
| A link shows **OFFLINE** | Adapter lost / unplugged | Replug it — it rejoins automatically |
| "Not enough space" | Target drive is full | Free space or save elsewhere |
| Download stuck near 100% | One slow tail chunk | Normal — fast links steal the rest automatically |
| Verification says CORRUPT | File didn't pass the checks | Rerun with a fresh link; the file wasn't kept |

---

## Where files live

Auto-created next to `bandwidthgoblin.py`:

- `logs/` — one white log per download (newest 20 kept)
- `perf.jsonl` — speed "memory" used to tune future downloads
- `history.jsonl` — quick history of recent downloads
- `<filename>.resume` — saved progress for interrupted downloads

That's it — happy downloading. 👺