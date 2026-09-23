# LinkBoost Changelog - every change = new build number

## v2.0 build 1 (2026-09-22) - first numbered build
Everything proven so far in one package:
- 3-link bonding (LAN + 2x USB tether), per-interface socket binding
- 32-chunk shared queue, IDM-style dynamic assignment, N threads/link
- Direct seek-write, O_BINARY, full-write loops, pre-allocated file
- Size discovery (HEAD + Range probe + open-ended fallback)
- Retry/resume, signed-URL refresh, 429 backoff, redirect-follow per IP
- Work stealing with single-steal lock + exactly-once completion
- Circuit breaker (6 fails = 30s park, 60s if adapter gone)
- Hot-plug every 2s (join/rejoin/leave with lossless handoff)
- ESC stop + `.resume` continue, crash hooks + faulthandler into `.log`
- Clipboard intake, link validation, batch paste, last-dir memory
- 200ms btop-style dashboard (progress, meters, sparkline, chunk strip,
  ONLINE/NEW/OFFLINE), colors, ASCII fallback, self-healing UI
- Completion notifications (toast/balloon), auto-open folder, beep
- Cross-platform: Windows / macOS / Linux interface detection + launchers
- DSCP EF socket tagging

Rules: bump BUILD by 1 for every change below, newest on top.
Log line 1 of every download carries `v{VERSION} build {BUILD}`.

## v2.0 build 38 (dev)
- fix: set global STOP on every run_one stop path (speedtest cap, ESC, no-range restart, hard failure) so idle workers exit the poll loop and ThreadPoolExecutor shutdown(wait=True) cannot hang the run after STOPPED/FAILED is printed

## v2.0 build 37 (dev)
- fix --speedtest churn: worker now lets _SpeedtestDone reach the runner (it was swallowed by the generic except, logged as 'failed ()' and re-queued forever at the same offset - the real-world multi-link collapse was this cap race, not the download engine)

## v2.0 build 36 (dev)
- real-world multi-link fixes: disable If-Range run-wide when validators prove unstable (tele2/ETag-per-request servers no longer pay a 200 round-trip + conn drop per chunk); silent stale keep-alive reconnect on BadStatusLine/RemoteDisconnected without burning give-up credit; resume trusts url/size/chunks only when the prior run logged unstable validators (manifest 'vok' flag)

## v2.0 build 35 (dev)
- beta: label all version strings as v2.0 build 35 (beta) in the banner, --version, dashboard header, and seeded log lines (BETA=True)

## v2.0 build 34 (dev)
- audit: G429_COOLDOWN twin bug - fetch_direct wrote a shadow local (global 60s throttle cooldown never fired), now declared global; Ctrl+C STOP flag in run_one now actually reaches threads (was a shadow local); reset_state clears stale LINK_THREADS between runs; head() guards malformed Content-Length; removed dead code: unused _p206 in range-probe loop, unused 'import queue' in try_steal, and dead 'global' declarations in monitor/try_steal/reset_state/main; pyflakes-clean

## v2.0 build 33 (dev)
- built-in --selftest (local ThreadingHTTPServer serving a ranged 4 MiB payload, real 2-link download over loopback, sha256-intact check, auto-cleanup, rc=0) and --verify manifest|dir (sha256sum-format checksums, per-file PASS/FAIL, exit 1 on any failure)
- QA polish in the same build: banner suppressed for utility commands (--verify/--selftest/--history/...); selftest server conn-reset noise silenced; one-shot CLI only treats the 2nd token as an output file when it is not itself a link (two --out-dir URLs = a batch, not URL+filename)

## v2.0 build 32 (dev)
- batch downloads: --jobs 1-8 (each queue entry in its own isolated process), queue lines now accept 'URL OUT' per-line output names, meaningful exit codes (0=saved, 1=hard failure) for one-shot runs, live 2-connection probe before every multi-link run (dead hotspot/WiFi links dropped with a loud single-stream warning), duplicate output names get _2/_3 suffixes

## v2.0 build 31 (dev)
- Build 31 (multi-link usability for WiFi+LAN combining): new --links 0,1 flag (also bare IPs, = MULTILINK_IFACES) selects exactly which interfaces to combine and feeds the existing selection channel; selector accepts indices or IPs; logs a hint when chosen links share a subnet (same router/ISP means reliability gain, not raw speed); multi-link runs now print a per-link MB @ MB/s summary on the DONE line

## v2.0 build 30 (dev)
- Build 30 (audit fixes): reset_state now clears PAUSED (a paused run no longer freezes the next download in the same process) and LINK_SPEED (stale seeded rates no longer ghost across runs); headless --out-dir/queue mode disambiguates files whose URL has no path name (download_1.bin, download_2.bin, ...) so queued token URLs stop overwriting each other

## v2.0 build 29 (dev)
- Build 29: desktop toast is now fire-and-forget - notify() spawns a detached powershell via -EncodedCommand (no temp .ps1, no blocking) and the toast shows ~3s instead of running for 7-9s; mac/linux branches also switch to non-blocking Popen; base64 added to imports

## v2.0 build 28 (dev)
- Build 28: QA fixes - STOP_REASON now declared global in run_one (speedtest branch previously crashed post-run); _throttle token bucket re-anchors the clock after each sleep so the slept interval is never double-credited (--limit now holds true to the cap instead of ~2x over); paused time no longer fast-forwards the throttle tokens (PAUSED branch re-anchors THR_LAST); --quiet also suppresses the launch banner (checked pre-parse)

## v2.0 build 27 (dev)
- Tier 2: P/R soft-pause keys (keeps sockets, stalls workers/steals/auto-tune, watchdog-safe); --limit MB/s aggregate token-bucket speed cap (env LINKBOOST_LIMIT); --history shows last 10 completed/verify-failed downloads (history.jsonl next to perf.jsonl); --resume flag continues the newest .resume plus interactive resume offer when idle; --speedtest URL measures per-link and combined MB/s via real chunks up to a configurable MB cap then cleans up; --quiet (milestones only on stdout, no chatter) and --no-log (no per-run disk logs)

## v2.0 build 26 (dev)
- forced the new run-feature log lines (extra headers set, sha256 enabled via --sha/env/sidecar) to be written into the per-run disk log file instead of only stdout; relocated them to right after the log file is created

## v2.0 build 25 (dev)
- --queue batch mode (one URL per line, headless, with --out-dir auto-save and queue ETA); --dry-run probe reports size/type/range support/206 check/learned speeds without downloading; deep verify via --sha <hash> OR auto-discovered <file>.sha256 / SHA256SUMS sidecars (reuses the existing EXPECT_SHA engine); extra request headers via --referer/--cookie/--header (repeatable) or LINKBOOST_REFERER/COOKIE/HEADERS env - sent on every probe and download request; --open/--no-open control auto-open of the folder on success (disk-space pre-flight and tty auto-open already existed); --help text

## v2.0 build 24 (dev)
- QA cycle 7: Ctrl+C now stops gracefully and writes a resume (was instant data loss in plain consoles); tune input is clamped (chunks 1-256, threads/link 1-16 - 0/huge values previously deadlocked); --version flag; is_link accepts localhost and bare IPv4 for LAN test servers; HEAD/redirect probes request identity encoding and close their connections; RUN.bat finds the per-user py launcher and can auto-install Python 3.13 via winget when missing; .gitignore added

## v2.0 build 23 (dev)
- QA-hardening: RUN.bat detects the MS Store stub + user/machine Python installs (dir cannot glob middle path segments); tiny-file runs clamp chunks to file size (negative ranges no longer wedge multi-link); drive-root save entries (D:\) are rejected with guidance instead of failing; redirect hops in head() close their connection

## v2.0 build 22 (dev)
- QA cycle 4: remove dead _xml_esc helper (never referenced)

## v2.0 build 21 (dev)
- QA cycle 1: steal CHUNK_END handed as full e (steal no longer breaks instantly), empty-response retries always get a fresh conn, unfinished-chunk exit is owner-verified give-up (never assumed theft), worker re-queues from real progress, LINK_THREADS dict races locked, logs pruned on every run, BrokenPipe-proof stdout (piped runs now exit 0), monitor exits before stale TOTAL line

## v2.0 build 20 (dev)
- per-link device labels (iPhone/Android/LAN) via neighbour OUI + PTR + subnet hints; monitor stops 10x sooner; labels in interfaces/join/range logs and dash rows

## v2.0 build 19 (dev)
- Core engine rewrite: strict 206/Content-Range validation, If-Range fallback (retry keeps Range), mid-file 200 -> per-link drop (_NoRange) + auto single-stream restart, real mid-file range probe, 412/HTML/Content-Encoding fatal guards, watchdog 180s, btop-style adaptive dashboard, atomic resume manifest with etag/lastmod, LINKBOOST_SHA256 verify, perf.jsonl rotation + junk filter, log redaction, RUN.bat py/python fallback

## v2.0 build 18 (2026-09-22)
- Safe default is now 16 chunks (was 32): enough to keep 3-4 links busy,
  half the concurrent range burst, fewer tails to steal. Rule of thumb:
  4-6 chunks per link, min 8, max 32. Strict sites: `16 1`.
- Fix retry-counter reset (stall-test finding): a fresh 206 with zero
  follow-up bytes no longer resets the give-up counter, and empty
  (premature-EOF) responses count toward give-up instead of looping
  forever. Progress still resets the counter.

## v2.0 build 17 (2026-09-22)
- Fix 90%+ tail stall: real socket timeouts (sock.settimeout, not just the
  attribute), 25s zero-byte stall abort turning hangs into retry/steal,
  64KB reads for prompt steal checks, victim-socket close on steal so the
  old owner notices immediately.
- Fix resume wiping progress: resume is loaded before file creation, done
  bytes are kept (r+b), chunk count is stored and must match to resume.
- Auto-tune holds slow links instead of scaling them up (below 1 MB/s or
  half of best stays capped).

## v2.0 build 16 (2026-09-22)
- Stopwatch row + simple HUD show running average next to elapsed.

## v2.0 build 15 (2026-09-22)
- Global connection-rate limiter: max 4 new connections/sec across all
  workers (reused keep-alive calls are free). Stays under throttle radar
  instead of tripping it.

## v2.0 build 14 (2026-09-22)
- Dirty-connection guard: reused sockets validated before use (kills the
  Request-sent churn that froze 95%+ downloads).
- Global throttle cooldown: total stall triggers calm 60s pause, not hot spin.
- Terminal-width truncation: narrow windows can't smear the UI anymore.
- Stopwatch row (HH:MM:SS, not ETA) + DONE screen holds 8s with save path.
  Simple HUD: inverse hero speed, stopwatch, location line at end.

## v2.0 build 13 (2026-09-22)
- Total speed is the hero: inverse-highlight block in full UI, yellow in
  simple HUD. Traffic-light % (red/yellow/green) + per-link own colors
  (sky/peach/pink) + per-link speed traffic lights.

## v2.0 build 12 (2026-09-22)
- Tail phase: when queued work runs out, steal threshold drops 8MB->1MB
  so even small slow tails get rescued instead of crawling solo.

## v2.0 build 11 (2026-09-22)
- Human traffic: keep-alive connection reuse per worker, full browser
  headers, natural pacing between new connections.

## v2.0 build 10 (2026-09-22)
- Anti-throttle: jittered backoff (workers no longer retry in lockstep),
  browser User-Agent on all requests.

## v2.0 build 9 (2026-09-22)
- Save prompt rejects pasted links with guidance instead of crashing.

## v2.0 build 8 (2026-09-22)
- Goal MIN time / MAX speed / file intact: auto-tune starts workers from
  learned history (fast links 3, slow 1) instead of ramping blindly.
- INTACT check on every finish: size + header magic per type.

## v2.0 build 7 (2026-09-22)
- Learning engine: every chunk + run recorded to perf.jsonl (per link,
  per host, speeds, throttles). `--report` shows per-connection tables,
  `--eta <host> <MB>` predicts min-time plan. Past runs seed speed memory.
- Auto-tune default: starts 1 worker/link, scales clean links to 4,
  backs off throttled ones. Fewer, smarter workers.
- Central white-log `logs/` folder (ANSI-stripped) next to the script.

## v2.0 build 6 (2026-09-22)
- RUN.bat opens Windows Terminal (full btop UI), falls back to plain
  console with one-line HUD when wt.exe missing.
- Simple HUD per-link labels L1/L2/L3 (was confusing IP tail chars).
- 429 backoff is exponential now (5s,10s,20s..cap 60s) instead of flat.

## v2.0 build 5 (2026-09-22)
- Catppuccin Mocha theme: mauve borders, lavender titles, pastel
  green/yellow/red accents (truecolor, NO_COLOR-safe).

## v2.0 build 4 (2026-09-22)
- Steal cooldown: one steal per chunk per 15s ends ping-pong storms.
- Nested steals no longer count as link failures (no false parking).
- Display-mode + steal-time state reset between session downloads.

## v2.0 build 3 (2026-09-22)
- Accept-Ranges tolerant: `bytes, bytes` duplicates (rd4.seedr.cc) no
  longer force single-link fallback.
- ESC abort closes all live sockets: file handles release in ~1s, stopped
  files deletable immediately.

## v2.0 build 2 (2026-09-22)
- Display modes: full btop UI (modern terminals), simple one-line live
  status (legacy Windows console, works everywhere), off (piped).
  Mode auto-picked, logged as `display: full|simple|off`.
  Escape hatches: MULTILINK_FORCE_DASH=1, MULTILINK_ASCII=1, MULTILINK_NODASH=1.
- Dashboard self-heal: UI errors fall back to plain logs, never silence.
- No full-screen flash: redraw in place.
