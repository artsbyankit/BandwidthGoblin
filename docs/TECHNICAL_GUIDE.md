# BandwidthGoblin — Technical Guide

> **Audience:** engineers, developers, and technically curious friends.
> This document describes **BandwidthGoblin v2.0, build 47** — the current
> shipped version. It assumes you have already read the friendly
> [USER_GUIDE.md](USER_GUIDE.md) and know how to *run* the tool.

---

## 0. The whole idea in plain English

Most download tools pick **one** internet door and queue the whole file
through it. If your computer has two doors — home Wi-Fi *and* a phone
hotspot, say — the second door just sits there idle.

BandwidthGoblin uses **every door at once**:

1. **Ask** the server how big the file is and whether it allows partial
   downloads (most big-file servers do).
2. **Slice** the file into pieces ("chunks", 16 by default).
3. **Send** each internet connection to fetch its own chunks at the same
   time, every connection in parallel.
4. **Write** each chunk straight into its exact spot in the file on disk —
   there's no "assembly later" step.
5. **Steal** unfinished chunks away from a connection that slows down or
   drops, and let the healthy ones finish them, so a dead hotspot never
   stalls the download.
6. **Verify** the finished file (size + file signature + SHA-256 when a hash
   is known) before reporting DONE.

The rest of this document is the technical map of how those six steps are
implemented and hardened.

### What's in this document

| § | Topic | You'll learn |
|---|---|---|
| 1 | What BandwidthGoblin is | the design rules that shape the code |
| 2 | Architecture at a glance | processes, threads, moving parts |
| 3 | Connection layer | finding & binding your internet links |
| 4 | HTTP protocol handling | talking to servers safely |
| 5 | Chunk scheduling & work stealing | how the load balancing works |
| 6 | Link membership & hot-plug | phones joining / leaving mid-run |
| 7 | Concurrency & locking | how shared state stays safe |
| 8 | Persistence & data formats | logs, memory, resume files |
| 9 | Verification subsystem | how files are proven intact |
| 10 | CLI & environment reference | every command line switch & env var |
| 11 | Batch mode | downloading many links at once |
| 12 | Exit codes | what the exit status means |
| 13 | Testing & QA | how the tool tests itself |
| 14 | Build & release process | how versions ship |
| 15 | Design invariants | the non-negotiables |
| 16 | Known limitations & roadmap | honest field results |

### Key numbers at a glance

| What | Value | Means in plain words |
|---|---|---|
| Chunks per file | 16 (1–256) | the file is sliced this many ways |
| Workers per link | auto → 4 (1–16) | parallel streams each connection may use |
| Socket timeout | 20 s | a silent connection is given up on fast |
| Slow-link threshold | 25 s with no progress | a link is treated as "quiet" and gets stolen from |
| Hard watchdog | 180 s with nothing at all | belt-and-suspenders: never hang forever |
| Smallest steal | 8 MiB (1 MiB near the end) | chunks smaller than this are left alone |

---

## 1. What BandwidthGoblin is

A **multi-connection downloader**: it downloads a single file over **every
internet connection the machine has** (Ethernet, WiFi, USB-tethered phone,
hotspot) simultaneously, then verifies the finished file.

Engineering constraints that shape every decision in the code:

1. **100% Python standard library.** No `requirements.txt`, no pip installs,
   no third-party packages. The whole tool is one file (`bandwidthgoblin.py`,
   ≈3.3k lines) that runs on any Python 3.8+.
2. **No server component.** The file flows directly from the origin server to
   the client. BandwidthGoblin never sits between them and never sees your data.
3. **Extremely defensive.** A downloader that corrupts files is worse than a
   slow one. Corruption, stalls, hangs, and silent overwrites are treated as
   bugs.
4. **Safe to interrupt.** Ctrl+C or `ESC` leaves you with a resumable state —
   the same command restarts where it stopped.

### Versioning

- `VERSION` / `BUILD` constants at the top of `bandwidthgoblin.py` (e.g.
  `VERSION = '2.0'`, `BUILD = 44`).
- `BETA = False` — build 44 is the first **stable** release; `BETA = True`
  adds the `(beta)` label to the banner, `--version`,
  dashboard header, and seeded log lines.
- The build number is bumped by 1 for every change; release tags are named
  `v2.0-buildN` (see §14).

---

## 2. Architecture at a glance

```
              URL  ──►  probe & plan (HEAD, size, ranges, ETag, links)
                             │
                    ┌────────┴─────────┐
                    │  run_one()      │
                    │  ── scheduler   │
                    │  queue of chunks│
                    └───┬─────────┬───┘
     ┌────────────────┐  │         │  ┌────────────────┐
     │ ThreadPool ◄───┘         └───► │ ThreadPool     │
     │ worker tasks (64)             │ worker tasks   │
     │ each: link x thread           │ each: link x   │
     │ http.client range GET        │ thread...      │
     └────────────────┘              └────────────────┘
        source-address bound sockets, one per physical link

  + membership thread (hot-plug links)   + auto_tune (workers per link / 429 load)
  + watchdog / stall detector            + throttle token bucket
  + dashboard thread (render)            + perf/history writers
  + --jobs: N subprocesses, one download each (batch isolation)
```

**Process model**

- Interactive/one-shot mode: a single process with many threads.
- Batch mode (`--jobs N`): N separate child processes, one download each.
  Isolation between parallel downloads is *by construction* — child processes
  cannot share globals.

**Thread model** (per download run)

| Thread | Job |
|---|---|
| Chunk workers (pool of 64) | Drive ranged `GET`s, one (link, chunk) at a time |
| Membership | Poll `local_ips()`, join new links, park lost ones, re-queue chunks |
| Auto-tune | Adjust `WORKER_CAP[ip]` (1–4; priority-high starts at 4) from server 429 pressure |
| Watchdog | Hard-stop after 180 s with zero progress |
| Ticker / monitor | Progress milestones, ETA, stall bookkeeping |
| Dashboard | Render the live status (full / off) |

---

## 3. Connection layer

### 3.1 Interface discovery — `local_ips()`

- Windows: parse `ipconfig` per-adapter IPv4 addresses.
- macOS/Linux: parse `ifconfig` (fallback `ip -4 addr`).
- Loopback (`127.0.0.x`) and link-local (`169.254.x`) addresses are **dropped**
  by default — a loopback does not consume a different physical route, and
  link-local/APIPA has no working gateway. Set
  `BANDWIDTHGOBLIN_KEEP_LOOPBACK=1` to keep them anyway (useful for QA-style
  multi-link tests against a server on `127.0.0.x`).
- Order is deterministic per adapter; `MULTILINK_IFACES` can select a subset
  by **index** (`0,1`) or **literal IP** (`192.168.1.133,172.20.10.6`).

### 3.2 Source-address binding

Every HTTP connection is created with `source_address=(ip, 0)` on the
`http.client` transport (`HTTPConnection` / `HTTPSConnection`). Binding to a
specific source IP pins the socket to that physical interface **even when
Windows/Linux would normally pick one default route**. This is the mechanism
that lets Ethernet and a phone-hotspot download in parallel.

Two loopback IPs (`127.0.0.2`, `127.0.0.3`) are used by the QA suite to
synthesize "two links" offline — source-binding to them works on Windows.

### 3.3 Reachability probe — `_probe_links()`

Before planning, BandwidthGoblin opens a TCP connection to `host:port` **from each
candidate source IP**. Binds that fail instantly (e.g. unroutable source —
Windows `WinError 10049`) are dropped with a warning. If fewer than two live
links survive, the run falls back to a single stream with an explicit note.

### 3.4 Connection reuse

- Connection cache `_CONN_CACHE`, keyed by
  `(thread-id, source-ip, host, port, scheme)`, reuses keep-alive connections
  per worker thread so parallel streams don't fight over one socket.
- Global pacing `_pace_conn()` limits new connections to ≈4/s to stay polite
  to servers and routers.
- Registered sockets in `SOCK_REG` carry an owner token, which lets the
  work-stealer (see §5.4) forcibly close a stalled victim's socket.

### 3.5 Tuning hooks

- `_apply_sock_timeout(conn)` — real socket `settimeout(SOCK_TIMEOUT)`, 20 s.
- TLS: default verify-enabled context.
- `IP_TOS` is set to DSCP EF (`0xB8`) where the OS allows, as a QoS hint.

---

## 4. HTTP protocol handling

### 4.1 What a run learns first (HEAD)

1. `HEAD` via each link; redirects are followed (max 4) per link and cached
   per-IP in `REDIR`.
2. Capture: **size** (`Content-Length`), **Accept-Ranges**, **Content-Type**,
   **ETag** and **Last-Modified** (validators used later).
3. `refresh_final` re-probes the final URL periodically (every `retries % 3`
   failures) so an expiring token/signed link gets re-copied automatically.

### 4.2 The mid-file range probe

A server may *claim* `Accept-Ranges` but send `200` full bodies anyway.
BandwidthGoblin probes with a **tiny range in the middle of the file**
(`bytes=size//2-size//2`) — never starting at byte 0, because `0-` is what
headers-only or sloppy servers special-case. To trust a link, the probe must
return `206` **and** a `Content-Range` whose start byte matches the request.
Any other behavior marks that link as **no-range**.

### 4.3 Validators (If-Range)

Each ranged request carries `If-Range` set to the **ETag** (weak prefix `W/`
stripped) or **Last-Modified**. If the server answers `200` (validator
mismatch), the file changed mid-download or the validator is unstable — that
link is demoted; a changed ETag at the policy level is **Fatal** rather than
silently corrupting the file.

### 4.4 Hard stops

- `Content-Encoding: gzip` on a ranged response → **Fatal**. Range requests and
  compressed transfer cannot be merged safely.
- `Content-Type: text/html` on a non-HEAD body → **Fatal** (sign-in page /
  captive portal / error page in disguise).

### 4.5 Rate limiting (429)

- Per-IP failure counters feed exponential backoff
  `min(base · 2ⁿ⁻¹, 60s)` + jitter; `Retry-After` is honored when present.
- A **global cooldown** `G429_COOLDOWN = 60 s` fires after 30 cumulative 429s
  across all links (deliberately parallel-safe; this was a real bug in build
  34 — a shadow-local variable meant the cooldown never engaged, fixed by
  declaring the global in `fetch_direct`).
- Cumulative 429 pressure drives `auto_tune` down (see §5.3).

### 4.6 Failure taxonomy

| Exception | Meaning | Consequence |
|---|---|---|
| `RuntimeError` | chunk I/O failed (stall, reset, timeout) | chunk freed, re-queue or steal |
| `_Stolen` | work-stealer took this chunk | worker exits silently |
| `_NoRange` | link doesn't truly support ranges | link parked, pool continues |
| `_Fatal` | file would corrupt or session is dead | whole run aborts |
| `_Stop` | user pressed ESC/Ctrl+C | clean shutdown + resume save |
| `_SpeedtestDone` | speedtest measurement finished | clean exit of measurement |

---

## 5. Chunk scheduling & work stealing

### 5.1 Slicing

- `NCHUNKS` default **16**, clamped to `[1, 256]`, and clamped to ≤ file size
  for small files (each chunk ≥ 1 byte).
- Chunk `i`: `[ size·i/N , size·(i+1)/N − 1 ]`.
- Desired parallelism: `threads-per-link` — `auto` = **1 per link** (normal
  tier; a per-IP-throttled host like YouTube gains nothing from more
  connections) or **4 when `--priority high` / `MULTILINK_PRIORITY=high`**
  (opt-in for hosts that don't throttle per source IP). Clamped 1–16, and an
  explicit "chunks threads" arg always overrides the tier. A 2-link high-tier
  run with 16 chunks schedules up to **2 links × 4 threads = 8 concurrent
  ranged streams**.

### 5.2 Execution

- Per-link worker tasks are hosted on a `ThreadPoolExecutor(max_workers=64)`.
- A live bitmap (`CHUNK_STATE`) marks `done`; each worker writes only its own
  byte range into the output file (random-access seeks — chunk IDs also index
  the file directly, no assembler pass needed).
- Chunk ownership tokens (`CHUNK_OWNER[ip,time]`) and hard `CHUNK_END` bounds
  keep concurrent writers from overlapping.

### 5.3 Auto-tune

- Starts from per-link caps of 4 (or seeds them from the best historical
  chunk speeds in `perf.jsonl`).
- Server 429 pressure on a link walks its cap **down** (1–4 window); steady
  successful throughput walks it **up**. The intent: extract as much from a
  cooperative server as it will give, without hammering it into throttling.

### 5.4 Work stealing (the heart of load balancing)

A TCP link's throughput varies second to second (WiFi vs Ethernet vs hotspot).
The scheduler is **remainder-based**:

- Sources compute their outstanding byte-remainder continuously.
- When a link has been **quiet for 15 s** while others are hot, the others may
  **steal** its still-open chunks — choosing the chunk with the **best
  remaining size** (biggest win).
- A steal copies the victim's bookkeeping: requests resume at the victim's
  actual progress byte (`CHUNK_POS`), not the chunk start; `CHUNK_END` is
  trimmed so a stolen-chunk tail and the victim's next request never overlap.
- `SOCK_REG` lets the thief abort the victim's stalled socket so
  `STALL_SECS` (25 s) never need to elapse waiting.
- Steal burst is capped (8 chunks per cycle); re-queued chunk attempts are
  counted per index (`CHUNK_TRIES`, reused sockets) with a hard cap of **12**
  before the run goes Fatal.
- The minimum steal size `STEAL_MIN` is **8 MiB** normally and shrinks to
  **1 MiB** in the tail phase (few chunks left) so the final stretch still
  load-balances instead of idling one link.

---

## 6. Link membership & hot-plug

- The membership thread re-reads `local_ips()` every few seconds and diffs it
  against the current pool.
- **New link appears** (phone plugged in mid-download): range-probed, validated,
  then joined with fresh worker tasks.
- **Link vanishes** (hotspot dropped): its chunks are re-queued for the
  survivors, and the adapter is parked (70 s). Links whose probe fails
  `PARK_FAILS` (6) times are parked 30 s before retry — eager retries on a
  dead link just burn wall-clock time.

---

## 7. Concurrency, globals & locking

Everything that must be shared between the scheduler, stealers, auto-tune,
and dashboard lives in module-level structures guarded by one lock each:

| Lock | Protects |
|---|---|
| `G_LOCK` | run-level globals, counters, milestone state |
| `CHUNK_LOCK` | chunk bitmap, positions, ownership, done count |
| `LINK_LOCK` | per-link byte totals |
| `IP_LOCK` | per-IP state store (park, backoff, caps) |
| `SOCK_LOCK` | socket registry (aborts vs reuse) |
| `PERF_LOCK` | `perf.jsonl` append serialization |
| `CHUNK_TRIES_LOCK` | per-chunk retry counters |

Other key structures: `G_FUTS` (all pool futures so a stop can wait on every
worker), `G_RUNID` (generation counter — a new run or link change invalidates
stale membership loops), `STOP`/`RUN_DONE` event flags.

**Run lifecycle.** Between downloads `reset_state()` clears every per-run
global (in build 34 `LINK_THREADS.clear()` was added so a second download in
the same session can't inherit dead worker handles — another silent
shadow-local bug: `STOP` wasn't made global in `run_one`, so Ctrl+C once
actually set only a local copy; both fixed in build 34).

**Shutdown.** ESC/Ctrl+C raise `_Stop`; the runner sets `STOP`, joins the
dashboard (≤10 s), waits on outstanding futures, then writes the `.resume`
manifest if chunks are incomplete.

**Crash safety.** Custom `sys.excepthook` + `threading.excepthook` redirect
any stray traceback into the run log file; `faulthandler.enable()` dumps
native-level stacks on hard signals; module import is wrapped so a broken
console output can't take the whole tool down.

---

## 8. Persistence & data formats

All runtime data lives **next to `bandwidthgoblin.py`** (the script dir), not in the
user's download folder.

### 8.1 `logs/`

- One numbered log per run (`<name>.log`, rotated, newest 20 kept).
- Line 1 is the version seed:
  `BandwidthGoblin v2.0 build 39 (beta) | start ... url=<redacted>`.
- `--no-log` disables disk logging.

### 8.2 `perf.jsonl` (append-only JSON Lines)

Two record kinds, both with `'build'`:

```jsonc
{"type": "chunk", "host": "...", "ip": "192.168.1.133", "mb": 12.3,
 "sec": 4.1, "mbps": 3.0, "outcome": "done", "t": 1730000000.0, "build": 35}
{"type": "run",   "host": "...", "mb_total": 100.0, "sec": 30.2,
 "mbps_avg": 3.3, "links": ["192.168.1.133", "172.20.10.6"],
 "chunks": 16, "tpl": 4, "auto": true, "outcome": "done",
 "per_link_mb": {"192.168.1.133": 52.1, "172.20.10.6": 47.9}}
```

Used as the speed "memory" that seeds auto-tune caps and feeds
`--speedtest`, `--report`, and `--eta` estimates.

### 8.3 `history.jsonl`

One JSON line per completed download: `url` (query-string **redacted** via
`redact()` so tokens never hit disk), host, output name, MB, seconds, MB/s,
links, chunks, outcome, error tail. Only the last ~10 are shown by
`--history`.

### 8.4 Resume state (`<out>.resume`, `<out>.progress`)

If a run stops / fails / is interrupted with chunks pending:

```jsonc
{"url": "https://...", "size": 104857600, "chunks": 16,
 "done": [0,1,2,3,4,5,6,7], "etag": "\"qa-big\"",
 "lastmod": "", "build": 35}
```

A rerun of **the same URL with the same chunk count** reads the manifest and
skips already-done chunks **only if** the servers' `ETag` / `Last-Modified`
are unchanged (the file moved → resume rejected, fresh start). Done chunks are
business-as-usual only when `os.path.getsize(out) == size`.

### 8.5 Sidecar checksums & `.lastdir`

- If a `<out>.sha256` file or a `SHA256SUMS` file exists beside the target,
  its hash is auto-detected and used for the final deep verify.
- `.lastdir` remembers the last output directory across runs.

---

## 9. Verification subsystem

`verify_file(out, size)` — the gate before a run is allowed to report DONE.
While a YouTube run is between dashboards (reading video info, the ffmpeg
video+audio merge, or this verify) a one-line `|\ /|` spinner with the phase
name + elapsed seconds is rendered instead of a blank screen; it clears its
own line when the phase ends and is fully silent when not on a TTY or under
`--quiet` (build 47):

1. **Size** must match the server-declared total.
2. **Header inspection**: reject HTML (sign-in/captcha/error pages) at the top
   of the file.
3. **Extension magic** (cheap, catches garbage early):

   | Extension | Magic bytes |
   |---|---|
   | `.zip` | `PK\x03\x04` |
   | `.exe` | `MZ` |
   | `.webm`, `.mkv` | EBML `\x1a\x45\xdf\xa3` |
   | `.mp4`, `.m4v`, `.mov` | `ftyp` at bytes 3–8 |

4. **SHA-256 deep verify** when a hash is known: from `--sha`, the
   `BANDWIDTHGOBLIN_SHA256` env var, a sidecar `.sha256`, or `--selftest`. A
   mismatch reports `CORRUPT?` and the run is marked failed, not saved.

`--verify <manifest|dir>` is a standalone tool: checks a `sha256sum`-format
manifest, or every file in a directory against `SHA256SUMS` / `*.sha256`
sidecars. Exit code 1 on any failure.

---

## 10. CLI & environment reference

### Direct

```
bandwidthgoblin.py                         interactive (paste a link after prompt)
bandwidthgoblin.py URL OUT [auto auto]     direct download
bandwidthgoblin.py URL --out-dir D:\dl     save straight into a folder
bandwidthgoblin.py --dry-run URL ...       probe links: size, type, ranges, learned speed
bandwidthgoblin.py --sha <sha256> URL OUT  deep-verify against a hash
bandwidthgoblin.py --queue list.txt [--out-dir D:\dl]
                                     batch - one URL per line in the file
bandwidthgoblin.py --history               show the last 10 completed downloads
bandwidthgoblin.py --resume                continue the newest saved .resume download
bandwidthgoblin.py --speedtest URL         measure each link + combined speed (8 MB cap)
bandwidthgoblin.py --yt URL [quality]      YouTube download (quality = best | 2160 | 1440 |
                                         1080 | 720 | 480 | audio; interactive mode
                                         auto-detects youtube.com / youtu.be links)
bandwidthgoblin.py --verify <manifest|dir> check files against sha256sum checksums
bandwidthgoblin.py --selftest              built-in offline QA
bandwidthgoblin.py --help / --version      help / version
```

### Flags (any position)

| Flag | Env equivalent | Effect |
|---|---|---|
| `--queue FILE` | — | batch list (see §12) |
| `--out-dir DIR` | — | output folder for queues / direct |
| `--dry-run` / `--info` | — | probe links: reachability, size, ranges, speed memory |
| `--sha HEX` | `BANDWIDTHGOBLIN_SHA256` | expected sha256 for deep verify |
| `--referer URL` | `BANDWIDTHGOBLIN_REFERER` | Referer header (some CDNs require it) |
| `--cookie STR` | `BANDWIDTHGOBLIN_COOKIE` | Cookie header |
| `--header "N: v"` | `BANDWIDTHGOBLIN_HEADERS` | arbitrary extra headers (repeatable; env uses `;` = new header) |
| `--limit MB/s` | `BANDWIDTHGOBLIN_LIMIT` | aggregate speed cap across all links |
| `--links 0,1` | `MULTILINK_IFACES` | pick interfaces by index or IP |
| `--open` / `--no-open` | — | open the file after success |
| `--history` | — | last 10 completed runs |
| `--resume` | — | newest `.resume` manifest |
| `--speedtest` | `BANDWIDTHGOBLIN_SPEEDTEST_MB` | per-link + combined measurement |
| `--report [host]` | — | per-host throughput summary |
| `--eta host MB` | — | time estimate from learned speeds |
| `--quiet` | — | milestones only, no dashboard |
| `--no-log` | — | no disk logs |
| `--jobs N` | — | parallel batch downloads (1–8) |
| `--yt URL` | — | YouTube download (`--yt URL 1080` caps height; see §10b) |
| `--verify` | — | checksum manifest/dir check |
| `--selftest` | — | offline self-QA |

Other env: `MULTILINK_NODASH=1` (disable dashboard even on a TTY),
`MULTILINK_ASCII=1` (plain-ASCII dashboard),
`MULTILINK_SUB_LANGS=hi.*` (subtitle languages for YouTube .srt fetch),
`NO_COLOR` (disable ANSI colors).

### Dashboard

**Build 47: the dashboard is the only UI** — the legacy one-line `simple`
mode was deleted; any TTY gets `full` (plain Windows consoles get ANSI/VT
enabled via the console API first), and `off` only for piped/`--quiet`/
`--jobs`/`MULTILINK_NODASH=1`. The layout is **resize-aware**: `_geom()` is
re-read every 0.2 s frame, so box width (50–160 cols), meters, sparkline and
chunk bar all re-flow when the console window changes size. Per-link row:
name, IP, %-of-traffic, live MB/s, chunk/activity state. Keys (interactive
only — headless automation simulates `lb.PAUSED`): `P`/`R` pause + resume,
`ESC` stops safely, `H` toggles a
6-second help overlay drawn *inside* the dashboard frame (rendered by the
same 0.2 s loop, so it cannot be clobbered by repaints). The bottom line
always shows the key hints; a one-time "how to drive this" intro appears at
the first interactive prompt, and typing `help` / `menu` at any prompt calls
`_mini_help()`. You never need the guide to drive it.

---

### YouTube mode (§10b)

Since build 44 (first stable release), any link whose host is
`youtube.com` / `youtu.be` (including `m.`, `music.`, `shorts` variants) is
detected by `is_youtube()` and routed into the YouTube pipeline instead of the
plain HTTP engine — YouTube protocols (DASH, SABR, m3u8, signatures) are far
outside the project's scope to re-implement from scratch, so the tool
delegates extraction to the battle-tested open-source engine.

- **Detection:** `is_youtube(url)` checks the hostname; the interactive paste
  path, the one-shot `--yt` flag, and the loop all route YouTube links the
  same way. `--yt URL 1080` caps height; `--yt URL audio` grabs the
  soundtrack.
- **One-time provisioning:** the first YouTube download fetches three
  binaries into a `tools/` folder next to the script (gitignored):
  - `yt-dlp` — the downloader/extractor engine (The Unlicense). Downloaded
    from the official GitHub release assets (`yt-dlp.exe` / `yt-dlp_linux` /
    `yt-dlp_macos`).
  - `deno` — JavaScript runtime (MIT). YouTube extraction without a JS
    runtime is deprecated in yt-dlp and returns incomplete format lists, so
    the tool passes `--js-runtimes deno:<path>`.
  - `ffmpeg` — media merger (LGPL). Needed to merge Best-Video + Best-Audio
    streams into one file — without it 1080p+ video with sound is
    impossible. On Windows it fetches the official gyan.dev static build;
    macOS/Linux use a system `brew`/`apt` install if found.
- **Format selection:** `best` → `bv*[protocol!*=m3u8]+ba[protocol!*=m3u8]/bv*+ba/b`
  (prefer best splittable video + best audio, both direct-HTTP — the m3u8
  exclusion keeps the multi-link engine usable on videos whose "best" would be
  a visionos HLS playlist; falls back to the old unpinned selector, then best
  single-file). A height preset (`2160/1440/1080/720/480`) → the same filter
  with `[height<=H]`. `audio` → `ba/b` (+ `-x --audio-format mp3` when
  ffmpeg exists). `--no-playlist`, `--newline`, and a
  `%(title)s [%(id)s].%(ext)s` output template keep naming tidy.
- **Build 45 — the multi-link split.** When ffmpeg is available and the
  request is not audio-only, `_yt_run` stops delegating the actual download
  to yt-dlp and instead:
  1. `_yt_json()` asks yt-dlp to *extract* only (`-J --no-check-formats`) with
     the URL as the positional target, returning the JSON info dict.
  2. `_yt_split_download()` reads `requested_downloads[0].requested_formats`
     (the concrete googlevideo stream URLs — which answer `206 Partial
     Content` / `Accept-Ranges: bytes`, proven range-splittable). Each stream
     is downloaded **sequentially in-process** through `run_one()` with 16
     chunks — the exact same all-networks, resumable, verified engine used
     for any other link — writing hidden temp files `.<vid>_s{i}.tmp` in the
     save folder. `--limit`/`--links`/`--cookie`/`--referer`/`--sha` flags are
     forwarded to those `run_one` calls so YouTube honors the same controls
     as regular downloads.
  3. The streams are merged with `ffmpeg -c copy` (no re-encode) into
     `%(title)s [%(id)s].mp4/.webm/.mkv`; a merge failure retries once as
     `.mkv`. Temp files are removed on success *and* on failure/stop, so a
     broken run never litters the folder.
  - **Fallbacks, in order:** non-HTTP / HLS (`.m3u8`) stream URLs, a single
    non-splittable stream shape, any `run_one` failure, a failed merge, or a
    user stop → `_yt_direct()` runs plain yt-dlp for the whole download
    (audio-only always uses this path). No ffmpeg → `fmt = 'b'` (best
    single-file, often capped ~720p). A single stream simply renames the temp
    file into place. **A save-drive shortfall is the exception:** there is no
    fallback (yt-dlp needs the same bytes) — run_one returns `'disk-space'`
    and the download stops cleanly before a single byte, telling the user to
    free space, save elsewhere, or re-run at a smaller quality.
  - **Build 46 fixes in this path.** (1) `range_probe()` now follows redirects
    (bounded, 5 hops) instead of raising on `HTTP 302`. googlevideo redirects
    a range probe arriving from a second network to that network's nearest
    node; build 45 dropped the link there, silently collapsing YouTube splits
    to one connection. (2) A new `STOP_USER` flag separates "a human pressed
    ESC/Ctrl+C / the watchdog stalled" from ordinary failures, so ESC during a
    stream returns `stopped` (resume saved, no retry) instead of cascading
    into the yt-dlp fallback with `STOP` still armed. (3) `_clean_tmp()` also
    removes `.progress` leftovers.
  - **Build 47: subtitles.** `_yt_subs()` is called after a successful video
    download (split *and* direct/fallback paths). When the extractor's info
    dict carries `subtitles`/`automatic_captions`, it runs yt-dlp with
    `--skip-download --sub-langs <lang> --sub-format srt/best --convert-subs
    srt` (ffmpeg converts vtt→srt) and prints the single `.srt` that lands
    next to the video. Language choice — exactly **one**: the video's own
    language (first manual caption key; YouTube lists the uploader's original
    track first, `info['language']` wins if present), else English, else the
    first auto track; `--write-auto-subs` only when no manual track exists,
    and any extra files are pruned so only one `.srt` remains. Override via
    `MULTILINK_SUB_LANGS`. No captions → returns early without touching the
    network; a failed sub fetch never fails the download.
  - **Build 47: audit hardening (from real 2–3-link stop/resume testing).**
    (1) The range probes at run start now execute **in parallel** across links,
    and each probe caches its resolved redirect node in `REDIR[ip]`, so the
    first chunks skip the redirect hop. (2) Chunk counts are **size-aware**
    (~4 MB/chunk, clamped 4–64) and are the default everywhere
    (`auto`); an explicit number is still honored (speedtest uses 256,
    selftest 4). (3) `use` (the live link list) is mutated and read strictly
    under `G_LOCK` — previously an unlocked append could race a failing-link
    scan and crash with `list changed size during iteration`, losing the
    resume. (4) On a user stop the split now **keeps** the partial stream and
    run_one's `.resume` manifest (`_clean_tmp()` only runs on genuine failure),
    and resume is accepted across runs for split streams even though
    googlevideo re-signs URLs — the tmp name is video-id+stream scoped and the
    size is binding, so a size+chunk-plan match continues the partial
    (`resuming: skipping N done chunks`). Verified: stop at 10/64 chunks
    (77.9 MB), re-run resumed and merged clean. (5) A `401/403/410` from a
    stale per-link node now drops the cached `REDIR[ip]` before the retry so
    `refresh_final()` can actually rescue an expired token — previously the 403
    dead-loop could fail a long download. (6) `[P]` pause now refreshes
    `LAST_PROGRESS`, so pausing longer than the 180 s watchdog no longer
    force-stops the run. (7) Subtitle language matching is exact-code first,
    then region-stripped (`en` matches `en-US`) with the **manual** track
    preferred over auto. (8) Auto-tune logs no longer claim 2–4 threads per
    link (it keeps one connection per link; the speed comes from aggregating
    links, and YouTube/OVH throttle per source IP anyway). (9) Transient CDN rejections no longer abort the
     multi-link engine: the preflight `head()` retries 4xx/5xx with backoff,
     and if a split stream still fails the tool re-extracts fresh signed URLs
     once and retries before downgrading to the single-connection yt-dlp
     fallback (which now says so on screen). (10) The dashboard snapshots the
     live link list once per frame under `G_LOCK` — the write-side lock was
     already there, the read side is now too.
- **Failure modes:** region/age-locked videos surface as "Sign in to confirm
  you are not a bot" (watch once in a browser first). Exit code follows the
  same 0/1 convention as everything else (see §12).
- **License note:** these are third-party open-source projects fetched at
  runtime, not bundled in the repo — credit lives in the README and guides.

---

## 11. Batch mode (`--queue`, `--jobs`)

- One URL per line: `URL` or `URL output-name.ext`; outputs resolve against
  `--out-dir` (absolute paths taken as-is).
- Name collisions get `_2`, `_3` suffixes automatically.
- `--jobs 1-8`: each entry becomes its own child process
  (`python bandwidthgoblin.py URL OUT auto auto <eflags>`) with
  `MULTILINK_NODASH=1` so parallel output stays readable. Environment
  (headers, cookie, ifaces, limits) is inherited, so per-queue tuning is one
  env export away.

---

## 12. Exit codes

| Code | Meaning |
|---|---|
| `0` | file saved & verified (or utility succeeded) |
| `1` | hard failure (download error, verify failed, batch had any failed file) |

`--yt` follows the same convention: `0` = video/audio saved, `1` = engine
unavailable or the download failed.

---

## 13. Testing & QA

Every release is put through a built-in offline self-check before shipping:
`bandwidthgoblin.py --selftest` exercises the real ranged multi-link code
path — a file is split into chunks, downloaded over synthesized loopback
links, recombined, verified byte-for-byte (sha256) — and must exit 0. Any
user can run the same check with one command.

The heavier offline regression harness (a local ranged QA server plus
per-build driver scripts) lives outside the public repo, so this repository
stays lean and user-focused. The checks and the code under test are the same;
only the shipping location differs.

---

## 14. Build & release process

A release ships in five simple steps:

1. Bump `BUILD` by 1 in the canonical `bandwidthgoblin.py` (right after `VERSION`).
2. Add a `## v2.0 build N` section to `CHANGELOG.md`.
3. `py_compile` the canonical file.
4. Sync the **four byte-for-byte-identical copies**:
   `bandwidthgoblin.py`, `pack/windows/bandwidthgoblin.py`,
   `pack/mac/bandwidthgoblin.py`, `pack/linux/bandwidthgoblin.py`.
5. Tag the commit `v2.0-buildN` and publish a GitHub release from it.

**Invariant:** the four copies are SHA-identical at all times. If a
post-sync edit is ever made to the canonical file by hand, the packs must be
re-synced manually (`Copy-Item bandwidthgoblin.py pack\*\bandwidthgoblin.py`).

The platform folders are self-contained **packs** (each ships its own copy +
launcher: `RUN.bat`/`run.command`/`run.sh` + a short README), so a user can
grab one folder and go — no repo required.

---

## 15. Design invariants (the non-negotiables)

1. **Never corrupt.** If-Range on every ranged request; `Content-Range`
   start-byte must match; a `200` mid-file answer demotes the link; gzip or
   HTML-on-range is Fatal; every chunk's byte range is hard-bounded
   (`CHUNK_END`); the file is verified (size → header magic → sha256) before
   the run may report DONE.
2. **Never hang.** Real socket timeouts (20 s), stall detection (25 s zero /
   trickle), a 180 s watchdog that force-stops instead of blocking forever,
   and a work-stealer that can abort a victim's socket. Build 38 closes the
   last gap: every stop path (speedtest cap, ESC/Ctrl+C, no-range restart,
   hard failure) now raises the global `STOP` flag before the download
   executor shuts down, so an idle worker can never hold `shutdown(wait=True)`
   open forever.
3. **Never waste work.** Resume from the byte granularity of complete chunks;
   stolen chunks resume from the thief's actual progress, not the start.
4. **Never surprise.** Runs are interrupted safely (ESC/Ctrl+C → resume
   state), logs are crash-safe, and URLs are redacted before hitting disk.

## 16. Known limitations & roadmap (honest)

**Field validation (builds 35–38, 41–42)** — real-world results from this machine's
live connections (Ethernet LAN, USB-tethered phone, iPhone hotspot, Wi-Fi):

| Test | Target | Config | Result |
|---|---|---|---|
| Speedtest | `cdn.kernel.org` linux-6.6.tar.xz | 3 links, 8 chunks auto | clean `STOPPED (speedtest)`, 8.2 MB in 4.3 s |
| Full download | kernel.org 140.1 MB | `16 auto` (3 links, auto-tune 1–4 workers) | **26.6 s ≈ 5.3 MB/s**; sha256 = `d926a0…56d0` matches kernel.org's official `sha256sums.asc` (byte-for-byte proof) |
| Over-parallel stress | kernel.org 140.1 MB | `128 4` (totals 46 streams over 3 links) | 37.7 s ≈ **3.7 MB/s** — over-parallelization measurably hurts on real CDNs |
| Max-parallel stress | `proof.ovh.net` 1 GiB | `256 16` (48 streams) | 429 storm + timeouts; watchdog force-stopped at 8.4 MB with resume saved — proving the stall path never hangs |
| Resume | OVH 1 GiB | resumed `256 2` | completed 1073.7 MB in 670.6 s, **INTACT** |
| Single link | kernel.org 140.1 MB | 1 connection (USB-tethered phone, after LAN removal) | 20.6 s ≈ **7.43 MB/s**, sha256 identical; speedtest 8.1 MB in 2.6 s ≈ 3.07 MB/s |
| Speedtest granularity | kernel.org 140.1 MB | build 41: 2 links, 8 chunks | the 8 MB window was satisfied entirely by one fast link (0.77 MB/s "combined") — coarse 17.5 MB chunks hid the other links; **fixed in build 42** with 256 fine chunks |
| 2 links (build 41) | kernel.org 140.1 MB | Wi-Fi (192.168.1.196) + iPhone tether, auto | **21.2 s ≈ 6.6 MB/s**, sha256 verified; Wi-Fi delivered 59.6 MB @ 2.65 MB/s — it had probed out as unreachable 90 s earlier, then recovered (flaky router) |
| 3 links (build 41) | kernel.org 140.1 MB | Wi-Fi + iPhone + Ethernet3 (10.106.77.62), auto, 16 chunks | all 3 joined; 38.3 s ≈ **3.7 MB/s**, sha256 verified; the 10.106.x link is a slow tail-dragger (0.13 MB/s), auto-tune keeps it to 1 worker |

Combining efficiency was ≈100% on the 3-link `16 auto` run (the sum of each
link's individual ceiling matched the combined throughput).

**Limits confirmed in the field (builds 36–38 hardening):**

- **Servers throttle multi-stream clients.** OVH's edge has a hard ceiling of
  roughly 6–10 concurrent streams per source IP (post-flood state from a
  previous test); after that it responds with 429s and long stalls. This is
  *why* the default is **one connection per link** (`auto auto`) — a source
  IP's ceiling is per-IP, so BandwidthGoblin's speed comes from aggregating
  *links*, not hammering one link with parallel streams, and why `256 16` is
  documented as a stress test, not a usage pattern.
- **Per-request ETags exist in the wild.** tele2's CDN returned a *different*
  ETag on every request, turning every `If-Range` into a full `200` rerun.
  Build 36's `VALIDATOR_BROKEN` flag detects this (first 200-on-If-Range) and
  switches the run to no-validator mode for the rest of the file, so a
  validator-quirky host never turns into a churn loop.
- **Some free CDNs block aggressive sources outright.** tele2 403-blocked this
  machine after sustained load; thinkbroadband.com 403s range probes from the
  start. Nothing BandwidthGoblin can do — the host-level block lives on the server.
- **Chunk size & workers are coupled.** On real hosts, more workers than a
  server tolerates *reduce* throughput (46-stream run: 3.7 MB/s vs 5.3 MB/s at
  sane parallelism). Auto-tune caps each link at 1–4 workers for this reason.
- **A weak link drags the tail.** The 10.106.x LAN link tops out near 0.1
  MB/s; while stealing recovers almost everything, the job still runs at the
  slowest link's pace for its chunks. `--links` can exclude a link you know
  is barely alive (`--links=192.168.1.196,172.20.10.2` skips the slow one).
- **Sockets do lie about "up".** Wi-Fi showed a clean `Up` + valid DHCP while
  its own gateway dropped 50% of pings and every outbound test timed out —
  only the app's real probe (bind + connect + 206 request to the actual URL)
  caught it. It also *recovered* mid-session with no interface change, so the
  probe-and-drop logic must not permanently blacklist a link.
- **`--speedtest` never truly finished on the first try** (builds ≤ 36 raced
  past the byte cap and re-queued the same chunk forever). Fixed in build 37 by
  letting `_SpeedtestDone` propagate cleanly. Build 38 additionally fixed a
  *hang* left in the same path: the speedtest stop left idle workers cycling,
  so `ThreadPoolExecutor.shutdown(wait=True)` joined them forever — the run
  printed `STOPPED (speedtest)` and then blocked. The global `STOP` flag is now
  set on every stop path (cap, ESC/Ctrl+C, no-range restart, hard failure)
  before the executor exits.
- **macOS/Linux packs** are code-identical but have not been exercised on
  real Mac/Linux hardware.
- **Pause keys** (`P`/`R`/`ESC`) are interactive-only and headless-untestable;
  automation simulates them via `lb.PAUSED`.
- **HTTP/1.1 only**; no IPv6 source-address binding yet.
- **One URL per direct invocation** (the queue covers many-at-once).
- **Token/signed URLs expire** — refresh_final handles mid-run expiry, but a
  stale link at start needs re-copying.
- **Throughput gains** depend on genuinely separate routes; two links sharing
  one bottleneck still complete safely, just without the speed boost.
- **YouTube (builds 44/45):** reliability centers on the third-party engines.
  A yt-dlp release can break against YouTube's web client at any time (as all
  YouTube downloaders do); `--selftest` does not cover YouTube (network +
  third-party dependent). Testing observed: missing JS runtime degrades the
  format list (fixed via deno), and the `android` client is currently broken
  by YouTube's SABR experiment, so the web client + deno is the default path.
  Region/age-locked videos refuse unauthenticated downloads by design.