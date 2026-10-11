# Why `vendor/mediatek/mt7663_n9_v3.bin` is a factory Android binary

The W103D's WiFi is a MediaTek MT7663S (SDIO). The driver loads two blobs:
the ROM patch (`mt7663pr2h.bin`, from linux-firmware, fine) and the N9 MCU
firmware (`mt7663_n9_v3.bin`) — **the N9 is the one that matters**.

The N9 shipped in the ophub base image (via linux-firmware) reports
`3.1.1 / 20200604161656`. On the W103D it boots, associates, and then
progressively dies (field report 2026-10-11):

- scans return stale or empty results while `nmcli device wifi list` still
  shows cached networks;
- authentication times out (`auth try 1/3 … 3/3`), disassociation loops with
  `Reason: 9=STA_REQ_ASSOC_WITHOUT_AUTH` every ~30 s;
- after ~15 minutes the MCU mailbox stops answering
  (`W103D mailbox write … failed: -110`), SDIO RX reads return
  `invalid RX packet length`, and the firmware wedge survives a soft reboot —
  only a power cycle restores the chip.

The vendored file here is the **factory build extracted from the W103D's
Android image** (`vendor/firmware/WIFI_RAM_CODE_MT7663.bin`, renamed for the
mt76 loader, bytes unmodified). It reports `7663mp1827 / 20210308205639`.
With it, the same board holds 200-packet pings at 0–0.5 % loss / ~6 ms RTT
with zero disconnects.

This is **factory STA firmware** — it steadies the station side only. **AP mode
stays hardware-failed on this board**: the driver and NetworkManager both report
the AP activated (software all green), but no beacon reaches the air (phone and
Mac both saw nothing), and forcing `prefer_offload_fw=0` loads the `_rebb`
firmware so the chip never initialises at all (`MCU initialization failed:
-110`). Onboarding therefore does not depend on AP — see the LAN box-settings
design §1 (`docs/superpowers/specs/2026-10-11-w103d-lan-box-settings-design.md`).

Pinned identity:

```
SHA-256  223f73f17f0f986dc4e7167daa6eef14ffb41c713f22d70f9645eb049bdec80a
size     441736 bytes
version  7663mp1827 / 20210308205639
```

`box-overlay.sh` installs it to `/lib/firmware/mediatek/`, verifies that SHA
(the build fails on mismatch), and registers a `dpkg-divert` so a
linux-firmware package upgrade cannot silently put the 2020 build back.

## Provenance and licensing

This is a MediaTek device binary, not GPL-licensed kernel code, and this
repository does not relicense it. The extraction and the working kernel port
(DTS, SDIO/MT7663S patches, validation records) come from the community
effort documented in
[ophub/amlogic-s9xxx-armbian#3571](https://github.com/ophub/amlogic-s9xxx-armbian/issues/3571)
and the contributor's fork (`pigeon2049/amlogic-s9xxx-armbian`, branch
`w103d-linux-6.18`, `board/w103d/`). Their note stands: no license text for
these exact bytes accompanied the factory file, and the linux-firmware notice
for a *different* N9 version is not asserted to grant rights over this one.
