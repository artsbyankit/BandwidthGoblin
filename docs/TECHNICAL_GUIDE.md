# BandwidthGoblin — Technical Guide

> **Audience:** engineers, developers, and technically curious friends.
> This document describes **BandwidthGoblin v2.0, build 39 (beta)** — the current
> shipped version. It assumes you have already read the friendly
> [USER_GUIDE.md](USER_GUIDE.md) and know how to *run* the tool.

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
  `VERSION = '2.0'`, `BUILD = 38`).
- `BETA = True` adds the `(beta)` label to the banner, `--version`,
  dashboard header, and seeded log lines.
- The build number is bumped **only** by `scripts/devbump.py` (see §16).

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
| Auto-tune | Adjust `WORKER_CAP[ip]` (1–4) from server 429 pressure |
| Watchdog | Hard-stop after 180 s with zero progress |
| Ticker / monitor | Progress milestones, ETA, stall bookkeeping |
| Dashboard | Render the live status (full / simple / off) |

---

## 3. Connection layer

### 3.1 Interface discovery — `local_ips()`

- Windows: parse `ipconfig` per-adapter IPv4 addresses.
- macOS/Linux: parse `ifconfig` (fallback `ip -4 addr`).
- Loopback (`127.0.0.x`) and link-local (`169.254.x`) addresses are **dropped**
  — a loopback does not consume a different physical route.
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
- Desired parallelism: `threads-per-link` (`auto` → **4**, clamped 1–16), so a
  2-link run with 16 chunks schedules up to **2 links × 4 threads = 8
  concurrent ranged streams**.

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

`verify_file(out, size)` — the gate before a run is allowed to report DONE:

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
bandwidthgoblin.py URL OUT [16 auto]       direct download
bandwidthgoblin.py URL --out-dir D:\dl     save straight into a folder
bandwidthgoblin.py --dry-run URL ...       probe links: size, type, ranges, learned speed
bandwidthgoblin.py --sha <sha256> URL OUT  deep-verify against a hash
bandwidthgoblin.py --queue list.txt [--out-dir D:\dl]
                                     batch - one URL per line in the file
bandwidthgoblin.py --history               show the last 10 completed downloads
bandwidthgoblin.py --resume                continue the newest saved .resume download
bandwidthgoblin.py --speedtest URL         measure each link + combined speed (8 MB cap)
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
| `--verify` | — | checksum manifest/dir check |
| `--selftest` | — | offline self-QA |

Other env: `MULTILINK_NODASH=1` (disable dashboard even on a TTY),
`MULTILINK_ASCII=1` (plain-ASCII dashboard), `MULTILINK_FORCE_DASH=1`
(force the full unicode dashboard where auto-detection declines),
`NO_COLOR` (disable ANSI colors).

### Dashboard

Auto-detects: full unicode (with per-link meters) on modern terminals /
Windows Terminal / `TERM_PROGRAM`; a compact single line on legacy Windows
consoles; **off** when output is piped, `--quiet`, `--jobs`, or
`MULTILINK_NODASH=1`. Per-link row: name, IP, %-of-traffic, live MB/s,
chunk/activity state. Pause keys: `P`/`R` toggle, `ESC` stops safely
(currently only interactive — headless automation simulates `lb.PAUSED`).

---

## 11. Batch mode (`--queue`, `--jobs`)

- One URL per line: `URL` or `URL output-name.ext`; outputs resolve against
  `--out-dir` (absolute paths taken as-is).
- Name collisions get `_2`, `_3` suffixes automatically.
- `--jobs 1-8`: each entry becomes its own child process
  (`python bandwidthgoblin.py URL OUT 16 auto <eflags>`) with
  `MULTILINK_NODASH=1` so parallel output stays readable. Environment
  (headers, cookie, ifaces, limits) is inherited, so per-queue tuning is one
  env export away.

---

## 12. Exit codes

| Code | Meaning |
|---|---|
| `0` | file saved & verified (or utility succeeded) |
| `1` | hard failure (download error, verify failed, batch had any failed file) |

---

## 13. Testing & QA (`tests/`)

### 13.1 The QA server (`tests/qa_server.py`)

```
python tests/qa_server.py 8765 tests/big.src
```

Stdlib HTTP/1.1 server exposing precisely the failure modes that matter:

| Endpoint | Behavior |
|---|---|
| `/big.bin` | 4 MiB payload, single-range `206` + `Content-Range`, `ETag`, `If-Range` validation, `Accept-Ranges` |
| `/tiny.txt` | 5-byte file (small-file chunk clamp) |
| `/nohead.bin` | `HEAD → 405`, ranged `GET` works (CDNs that refuse HEAD) |
| `/norange.bin` | **ignores Range**, always `200` full body (the trick BandwidthGoblin must detect) |
| `/echo-headers` | echoes `Referer`/`Cookie`/`X-Qa` headers back |

`big.src` is a **deterministic** 4 MiB payload; its sha256 is checked by the
drivers, so any tool corruption is caught.

### 13.2 Drivers

Run from the repo root with the server on `127.0.0.1:8765`; each expects
`big.src` beside the driver and `bandwidthgoblin.py` importable from the repo root.
They simulate multiple real links by patching `lb.local_ips` to loopback
sources (`127.0.0.2/3/4`) — source-binding to loopback works on Windows and
exercises the *entire real code path* except the NIC itself.

| Driver | Covers |
|---|---|
| `qa_b29.py` | baseline ranged multi-link download, ETag paths |
| `qa_b30.py` | resume + list-of-downloads behaviors of that build |
| `qa_b31.py` | per-link totals, one-link runs, IP-index selection |
| `qa_b32.py` | queue/batch, dry-run/probe, unroutable-source probe, ghost run |
| `qa_b33.py` | `--selftest`, `--verify` manifest/dir/sidecars, corrupt/missing files |
| `qa_b37.py` | build 37/38 regression: `--speedtest` cap-stop must not churn (`failed ()`), must end < 20 s, must leave no `.resume`, and must not hang in-process — plus a plain-download control |
| `qa_drive.py` | scenarios: `multi` / `resume` / `norange` |
| `qa_tier2.py` | combined suite: pause simulation, `--limit`, `--history`, `--resume`, `--speedtest`, `--quiet`, `--no-log`, re-runs the three qa_drive scenarios |

Expected: **all `PASS`, exit 0**. The server's stderr fills with
`ConnectionReset`/`ConnectionAborted` noise — that is the tool closing
aborted sockets on purpose, not a failure.

### 13.3 Static checks

- `scripts/audit_dead.py` — custom AST checker for unreferenced
  defs/globals (the audit that found build 34's dead code).
- `pyflakes` (run from a temporary install, not the user env) — 0 findings on
  build 34/35; the only remaining "warnings" are intentional framework hooks
  (`connect`, `do_HEAD`/`do_GET`, `log_message`, `protocol_version`).

---

## 14. Build & release process

`scripts/devbump.py "what changed"` is the *only* way builds ship:

1. Reads `VERSION`/`BUILD` in the canonical `bandwidthgoblin.py`, bumps BUILD by 1.
2. Appends a `## v2.0 build N (dev)` CHANGELOG entry.
3. `py_compile`s the canonical file.
4. Syncs the **four byte-for-byte-identical copies**:
   `bandwidthgoblin.py`, `pack/windows/bandwidthgoblin.py`, `pack/mac/bandwidthgoblin.py`,
   `pack/linux/bandwidthgoblin.py`.
5. Prints `all copies match: True` (sha256 comparison).

**Invariant:** the four copies are SHA-identical at all times. If a
post-bump edit is ever made to the canonical file by hand, the packs must be
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
  *why* `16 auto` (1–4 workers/link) is the practical maximum and ship-able
  default, and why `256 16` is documented as a stress test, not a usage
  pattern.
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