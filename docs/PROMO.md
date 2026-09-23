# 👺 BandwidthGoblin — Promo & Ad Copy

<div align="center">

<img src="logo.png" alt="BandwidthGoblin 👺" width="96"/>

Feeds on every internet connection your machine has. Zero cloud, zero
accounts, pure feral speed.

</div>

> Ready-to-use marketing text — free to paste into an app store listing, a
> product-hunt post, a tweet, or a landing page.

---

## One-liners (taglines)

- Feed the goblin. Get your files faster.
- Why let idle Wi-Fi sit there when your phone hotspot is right next to it?
- Sucking bandwidth from every network adapter on your motherboard simultaneously.
- Single-lane downloading is for cowards.
- Paste link. Release the goblin. Watch it fly.

## Short blurb (social / store description)

BandwidthGoblin doesn't ask nicely — it scavenges every active internet
connection your computer owns (home Wi-Fi, Ethernet cable, 5G phone tether,
coffee shop guest network) and stitches them into a single hyper-threaded
downloader. That massive game patch or ISO that takes 3 hours on one
connection drops down to a fraction of the time.

No accounts, no telemetry, no subscription bloatware. One lightweight script.
Works on Windows, macOS, and Linux.

## Longer story (landing page)

**Paste a link. BandwidthGoblin gobbles the rest.**

Imagine ordering 100 packages online. Normal downloaders send one delivery
driver to fetch them one by one while three other delivery vans sit idle in
your driveway.

BandwidthGoblin hijacks all four vans at once.

Your machine almost always has redundant paths to the web: Ethernet + Wi-Fi,
or home broadband + phone tethering. Regular apps ignore the extra pipes.
BandwidthGoblin binds raw sockets across every network interface, splits the
download into chunks, pulls them in parallel, and stitches the file back
together with zero corruption.

Free, open-source, and entirely local.

## When it goes feral (use cases)

- 🌐 **60 GB Game Patches** — finish downloading before your mates even open Discord.
- 🎬 **Fat ISOs & Movies** — high-throughput direct downloads without throttled single-stream limits.
- 📱 **Desperate Hotspotting** — combine sketchy hotel Wi-Fi with cellular data when neither works alone.
- 📦 **Batch Slurping** — feed it up to 8 links at once and let it hoard data in parallel.

## The honest fine print

**BandwidthGoblin is in public beta (v2.0).** It works, but real-world
networking is a mess.

- **Where the speed comes from:** two *actually different* networks (e.g.
  Fiber Wi-Fi + Mobile 5G) will unleash maximum speed. If both interfaces
  feed off the same capped home router, the goblin still works safely — it
  just won't magically break physics.
- **Signed URL Expiry:** if a link has a temporary token that expires
  mid-download, feed the goblin a fresh URL.
- **Python 3.8+ Required:** run it natively through Python (Windows users:
  grab it from python.org, skip the Microsoft Store dummy app).

## Try it

Copy a download link, run BandwidthGoblin, and watch every connection on your
computer go to work. It's free — no accounts, no cloud, nothing to install.

- **[Friendly guide](USER_GUIDE.md)** — plain-language manual
- **[Technical guide](TECHNICAL_GUIDE.md)** — how it works under the hood