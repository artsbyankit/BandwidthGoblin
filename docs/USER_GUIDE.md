# <span style="color:#22d3ee">BandwidthGoblin v2.0</span> - Guide

<span style="color:#4ade80">Feed the goblin. Get your files faster.</span>

<span style="color:#4ade80">Downloads one file using all your internet connections at once</span>
(WiFi/LAN + phone hotspots) so it finishes faster. Free. No installs.

<span style="color:#9ca3af">Proven: 140 MB kernel.org download combined over 3 real links (Ethernet +
hotspot + USB tether) at ~5.3 MB/s with 100% combining efficiency, 1 GiB OVH
resume byte-intact, sha256 matching the server's official hash, and a
single-connection 140 MB run at 11.2 MB/s.</span>

## <span style="color:#22d3ee">How to use it (step by step)</span>

<span style="color:#9ca3af">Setup, one time: connect phones with USB cables, turn on USB tethering
(Android: Settings → USB tethering. iPhone: Personal Hotspot ON + Trust).
Windows/macOS/Linux packs live in windows/ mac/ linux/ folders.

Need Python? Grab the free installer from
👉 <span style="color:#facc15">[python.org/downloads](https://www.python.org/downloads/)</span>
(Windows: tick "Add python.exe to PATH" during setup).</span>

1. <span style="color:#facc15">Copy a download link</span> (one or several, one per line).
2. Double-click <span style="color:#facc15">`RUN.bat`</span> (opens Windows Terminal with the live dashboard).
3. Link first: clipboard link is offered - <span style="color:#4ade80">Enter</span> uses it,
   `p` pastes another. Empty/non-link clipboard gets an explanation + re-ask.
   Broken links (404/403/timeout) fail fast with the reason in plain words.
4. Save spot: <span style="color:#4ade80">Enter</span> accepts the suggestion (auto filename,
   last folder remembered). A name without extension = new folder.
   Never save straight to `D:\` (Windows blocks drive roots).
5. Tune: <span style="color:#4ade80">Enter</span> = automatic (starts 1 worker/link, scales clean
   links up, backs throttled ones down). Manual e.g. `16 1` for strict sites.
6. Watch: hero speed block, per-link rows, stopwatch + average, chunk strip.
7. <span style="color:#facc15">ESC</span> stops safely - progress saved, rerun continues it.
8. <span style="color:#4ade80">DONE</span> prints file + size + time, notifies, opens the folder.
   Type the next link or `N` to quit.

## <span style="color:#22d3ee">What the screen tells you</span>

- Hero block <span style="color:#4ade80">▼ combined MB/s</span> + traffic-light % + ETA.
- Per-link rows, each its own color: <span style="color:#4ade80">green ● ONLINE</span>,
  <span style="color:#22d3ee">cyan ● NEW</span>, <span style="color:#f87171">red ● OFFLINE</span>;
  speed green/yellow/red by pace; stopwatch + running average at the bottom.
- Chunk strip: <span style="color:#4ade80">#</span> done, <span style="color:#facc15">></span> active, `.` waiting.
- Legacy consoles get a one-line live HUD instead (auto-detected).
- Questions: <span style="color:#4ade80">green = yes/go</span>, <span style="color:#f87171">red = no/quit</span>.

## <span style="color:#22d3ee">Good to know (plain words)</span>

- <span style="color:#4ade80">Plug/unplug phones anytime</span> - checked every 2s, joins and
  handoffs are automatic, nothing is lost.
- <span style="color:#4ade80">Stopping is safe</span> - ESC or closing keeps progress, rerun continues.
- Expired/token links fail fast - copy a fresh link.
- Speed limit = your lines + the website's throttle. Throttled sites
  (Seedr free) do best with few workers - auto mode handles it.
- Every download writes a white-text log into `logs/` next to the script,
  first line carries the build number. Learning data accumulates in
  `perf.jsonl`: `py bandwidthgoblin.py --report` shows per-connection history,
  `--eta <host> <MB>` predicts the fastest plan.
- Settings via environment variables:
  - `BANDWIDTHGOBLIN_SHA256=<hex>` - verify the finished file's SHA-256 (streamed,
    no extra disk usage). Any mismatch aborts cleanly.
  - `MULTILINK_IFACES=0,2` - only use the listed interfaces (indices from logs).
  - `MULTILINK_NODASH=1` - plain text logs, no dashboard.
  - `MULTILINK_ASCII=1` - ASCII box drawing for very old terminals.
  - `NO_COLOR=1` - disable all colors.

## <span style="color:#22d3ee">If something looks wrong</span>

| <span style="color:#f87171">Symptom</span> | Cause | <span style="color:#4ade80">Fix</span> |
|---|---|---|
| Broken (404) / expired (403) | stale link | copy a fresh link |
| Constant 429 sleeps | host throttling | automatic (fewer workers, backoff, pacing) |
| Stuck near 100% | slow tail | automatic: fast links steal the remainder |
| OFFLINE link | unplugged/failing | replug, it rejoins alone |
| Permission denied | drive root/protected path | save inside a folder |
| Not enough space | target full | message shows need vs free |

---
---

# <span style="color:#f87171">ADVANCED / TECHNICAL SECTION</span>

## Architecture

- Interface discovery per OS; per-link sockets via `source_address`; shared
  SSL context; DSCP EF tags; browser UA + keep-alive reuse + pacing.
- Chunk queue + per-link worker pools; direct seek-write (`O_BINARY`,
  full-write loops); completion by piece counter, never byte totals.
- Work stealing (single-steal mutex, 15s cooldown, 8MB/1MB-tail threshold,
  exactly-once completion); circuit breaker (6 fails); hot-plug (2s);
  per-IP redirect cache with edge following; global 4-conn/sec limiter;
  socket abort on ESC; crash hooks + faulthandler into `.log`.
- Threads: workers, monitor (5s), ticker (1s `.progress`), membership (2s),
  dash (200ms), esc listener, auto-tuner (20s, 1-4 workers/link by 429 rate
  and learned speed memory seeded from `perf.jsonl`).

## Command-line flags (build 33+)

Run `bandwidthgoblin.py --help` for inline usage. Flags work at any position.

- `bandwidthgoblin.py --queue list.txt --out-dir D:\dl`
  Headless batch. One entry per line: `URL` or `URL output-name.ext`
  (`#` comments ok). No name = guessed from the URL (or `download_<n>.bin`
  for token URLs that carry no filename); duplicate names get `_2`/`_3`
  suffixes. `16 auto` tuning, no prompts. The run exits 0 when every file
  was saved, 1 if any failed.
- `bandwidthgoblin.py --jobs <1-8>` - download a `--queue` in parallel: up to N
  entries at once, each in its own isolated process (no shared state, safe
  for chained scripts). Works with per-line output names.
- `bandwidthgoblin.py URL --out-dir D:\dl` - save a single link straight into a
  folder (bypasses the save/tune prompts).
- `bandwidthgoblin.py --dry-run URL ...` - probe only: final URL, size, type,
  range support (with a live 206 check), and speeds BandwidthGoblin has learned
  for that host. Downloads nothing.
- `--sha <64-hex>` - deep-verify the finished file against a SHA-256.
  Auto-detected from a `<filename>.sha256` file or `SHA256SUMS` sitting
  next to the target, or `BANDWIDTHGOBLIN_SHA256` env var.
- `--referer <url>` / `--cookie <str>` / `--header "Name: value"` (repeat)
  - extra request headers for CDNs that demand them. Env equivalents:
  `BANDWIDTHGOBLIN_REFERER`, `BANDWIDTHGOBLIN_COOKIE`, `BANDWIDTHGOBLIN_HEADERS="a: 1; b: 2"`.
- `--open` / `--no-open` - force or suppress opening the folder when done
  (default: auto-open in a real console).
- `--limit <MB/s>` - cap the *combined* speed of all streams at once
  (all workers share one token budget). Env: `BANDWIDTHGOBLIN_LIMIT`.
- `--links 0,1` (or `--links 192.168.1.20,172.20.10.2`) - pick exactly which
  interfaces to combine, by index or by IP. Equivalent env:
  `MULTILINK_IFACES`. Before every multi-link run BandwidthGoblin probes each
  link's real reachability to the download host; dead links (expired
  hotspot, stale Wi-Fi) are dropped up front with a clear 
  "only 1 working connection - single-stream" warning instead of being
  silently counted.
- `--speedtest URL [URL ...]` - download real bytes and report each link's
  speed plus the combined rate, then clean up the temp file. Stops after
  ~8 MB of measurement (tune with `BANDWIDTHGOBLIN_SPEEDTEST_MB=2`).
- `--history` - show the last 10 downloads (outcome, MB, average speed)
  recorded in `history.jsonl`.
- `--resume` - continue the newest saved `.resume` automatically. When you
  start interactive and a `.resume` exists, BandwidthGoblin offers to resume it
  (press `r`).
- `--quiet` - keep only milestone lines on screen (no per-chunk chatter,
  no launch banner). `--no-log` - skip the per-run disk log in `logs/`.
- `--verify <manifest-or-dir>` - check downloaded files against stored
  SHA-256 checksums, no re-download. A manifest is a standard `sha256sum`
  file (`<64-hex>  filename`); a directory uses its `SHA256SUMS`, or any
  `file.sha256` sidecars. Prints one PASS/FAIL line per file; exits 1 when
  anything is missing or mismatched.
- `--selftest` - built-in offline QA: serves a random 4 MiB payload from a
  local ranged HTTP server, downloads it over 2 loopback links, verifies the
  SHA-256, cleans up. Exits 0 when healthy. Stdlib only, no internet.
- Exit codes: one-shot runs and batches exit `0` when the file was saved
  and `1` on a hard failure (unreachable link, web page, no disk space,
  permission, verification mismatch) - chain-friendly for scripts.
- During a download press `P`/`p` to pause (sockets stay open, workers
  wait) and `R`/`r` to resume. `ESC` still stops and saves a resume file.
- `--report [host]` (existing), `--eta host MB` (existing), `--help`.

## Files

- `bandwidthgoblin.py` (identical in root, windows/, mac/, linux/)
- `RUN.bat` (+ per-OS `windows/RUN.bat`, `mac/run.command`, `linux/run.sh`)
- `GUIDE.md` (this file, colorful), `GUIDE.txt` (plain), `CHANGELOG.md`
  (every change = new build number)
- Runtime (auto-created): `logs/` white logs, `perf.jsonl` learnings,
  `history.jsonl` (last downloads), `.lastdir`,
  `<out>.resume`, `<out>.progress`
