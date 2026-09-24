BandwidthGoblin - Linux
=================

WHAT IT DOES
  Downloads one file using all your internet connections at once
  (LAN + USB-tethered phones), so big files finish faster.
  Also downloads YouTube videos - paste any YouTube link and pick
  quality (best / 1080 / 720 / ... / audio); first use fetches the
  yt-dlp engine (~30-40 MB) automatically. YouTube downloads split
  over all your connections like any other link (ffmpeg via apt/brew
  enables full-quality 1080p+; auto-fetched on Windows).

FIRST TIME ONLY
  1. Install python3 if missing: sudo apt install python3   (or dnf/pacman equivalent)
  2. Android USB tethering works natively (shows as usb0).
     iPhone needs libimobiledevice + Personal Hotspot ON.
  3. Make the launcher executable once:
       chmod +x run.sh

RUN IT
  1. Connect phones with USB, enable tethering/hotspot.
  2. Double-click run.sh (or: ./run.sh in a terminal).
  3. Copy a download link first (xclip/xsel help clipboard read;
     pasting manually always works).
  4. Pick a save folder, watch the live dashboard.
     Keys during download: P pause | R resume | H help | ESC stop safely.
     Type 'help' at any prompt for the feature list - no manual needed.

TIPS
  - Strict sites (Seedr): answer tune with: 16 1
  - Notifications use notify-send.
  - Full manual: see ../../docs/USER_GUIDE.md
