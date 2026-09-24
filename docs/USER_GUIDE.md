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

## YouTube downloads

YouTube links work right out of the box — no separate program, no account.

1. **Copy a YouTube link** (a normal `youtube.com/watch?v=...` or short
   `youtu.be/...` link, or a **Shorts** / **Music** link).
2. **Start BandwidthGoblin** and paste the link when it asks — the tool spots
   it's a YouTube link automatically. (Or run it directly from a terminal:
   `py bandwidthgoblin.py --yt <link> 1080`.)
3. **Pick the quality:**
   - press **Enter** for **best available** (what the video itself offers —
     2160p, 1440p, 1080p, ...)
   - or type `2160` / `1440` / `1080` / `720` / `480` to cap the height
   - or type `audio` to save **just the soundtrack** (MP3 when possible)
4. **Pick a save folder** — press **Enter** for the suggested one, or type your
   own, e.g. `D:\Videos`.
5. Watch the progress. The file is saved with the video's **title** as the
   name.

**First-time note:** the very first YouTube download quietly downloads the
yt-dlp engine plus two helpers (a JS runtime and ffmpeg) — about 130–190 MB,
one time, saved in a `tools/` folder next to the script. Your computer will
tell you what's happening as it fetches them. After that, YouTube downloads
are offline and instant to start.

**Honest note:** YouTube downloads use the famous open-source **yt-dlp**
engine (with **deno** and **FFmpeg** for the newest YouTube formats and
1080p+ quality) — big respect and thanks to those projects. If a rare video
says *"Sign in to confirm you are not a bot"*, it's region/age-locked: watch
it once in a browser first, or pick another video.

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
| `py bandwidthgoblin.py --yt <URL> 1080` | Downloads a YouTube video at 1080p (or `best`, `720`, `480`, `audio`) |
| `py bandwidthgoblin.py <YouTube URL> D:\videos` | YouTube link + save folder in one go |
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