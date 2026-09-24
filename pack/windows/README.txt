BandwidthGoblin - Windows
====================

WHAT IT DOES
  Downloads one file using all your internet connections at once
  (LAN cable + USB-tethered phones), so big files finish faster.
  Also downloads YouTube videos - paste any YouTube link and pick
  quality (best / 1080 / 720 / ... / audio); first use fetches the
  yt-dlp engine (~30-40 MB) automatically. YouTube downloads split
  over all your connections like any other link.

RUN IT
  1. Connect phones with USB, turn on USB tethering
     (Android: Settings -> USB tethering.
      iPhone: Personal Hotspot ON + tap Trust).
  2. Double-click RUN.bat in this folder.
  3. Copy a download link first - the tool offers it on Enter.
  4. Pick a save folder, watch the live dashboard.
     Keys during download: P pause | R resume | H help | ESC stop safely.
     Type 'help' at any prompt for the feature list - no manual needed.

TIPS
  - Strict sites (Seedr): answer tune with: 16 1
  - Pin browser to LAN (admin PowerShell once):
      Set-NetIPInterface -InterfaceAlias "Ethernet" -InterfaceMetric 5
  - Full manual: see ..\..\docs\USER_GUIDE.md
