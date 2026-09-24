BandwidthGoblin - macOS
=================

WHAT IT DOES
  Downloads one file using all your internet connections at once
  (WiFi/LAN + USB-tethered phones), so big files finish faster.
  Also downloads YouTube videos - paste any YouTube link and pick
  quality (best / 1080 / 720 / ... / audio); first use fetches the
  yt-dlp engine (~30-40 MB) automatically. YouTube downloads split
  over all your connections like any other link (ffmpeg via brew/apt
  enables full-quality 1080p+; auto-fetched on Windows).

FIRST TIME ONLY
  1. Install python3 if missing: https://www.python.org (or: brew install python).
  2. Android USB tethering needs the HoRNDIS driver; iPhones work natively
     via Personal Hotspot (tap Trust on the phone).
  3. First launch: right-click run.command -> Open (clears Gatekeeper).
     Or in Terminal: chmod +x run.command

RUN IT
  1. Connect phones with USB, enable tethering/hotspot.
  2. Double-click run.command (opens in Terminal).
  3. Copy a download link first - the tool offers it on Enter.
  4. Pick a save folder, watch the live dashboard.
     Keys during download: P pause | R resume | H help | ESC stop safely.
     Type 'help' at any prompt for the feature list - no manual needed.

TIPS
  - Strict sites (Seedr): answer tune with: 16 1
  - Notifications use macOS Notification Center.
  - Full manual: see ../../docs/USER_GUIDE.md
