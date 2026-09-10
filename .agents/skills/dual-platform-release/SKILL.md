---
name: dual-platform-release
description: Use when publishing a new MRRC Modern release end-to-end — bumping the version, building the macOS DMG and Windows installer, syncing CHANGELOG/SDD/docs/website download pages, deploying installers to www.vlsc.net, verifying download URLs and SHA-256, or tagging/pushing a release commit. Symptoms it fixes include a stale SHA-256 on the download cards, an installer built with the old version, or a release tag missing from GitHub after push.
---

# Dual-Platform Release Pipeline (MRRC Modern)

## Overview

One version → two artifacts → one website. The version's **single source of truth is the top `## [vX.Y.Z]` heading in `CHANGELOG.md`** — `packaging/macos/build.sh` parses it at build time, and `packaging/windows/MRRC-Modern.iss` `#define MyAppVersion` must be manually kept equal. Platform mechanics live in the sibling skills (`macos-installer`, `windows-installer`) and the operator manuals (`mac_pack.md`, `win_pack.md`); this skill owns the release-day order and the cross-cutting traps. Optional Raspberry Pi artifact: `MRRC-Modern-v<ver>-rpi64.img.xz` built/published via `pi_pack.md` (`packaging/rpi/build-image.sh`); build it before the release-day pipeline when shipping it with a release, and re-verify its download URL in step 7 alongside the installers.

## Pipeline (v1.14.0-proven order)

**0. Pre-flight**

- Full suite green: `.venv/bin/python -m unittest discover -s tests` (never bare `python3`/`python` — no deps; never `source .venv/bin/activate` — wrong project's venv; see `macos-installer` gotcha 10).
- `python3 .agents/skills/sdd-guardian/harness/sdd_context.py brief <files>` before edits; chore-commit pending runtime data first (e.g. `atr1000_tuner.json` tuner learning).

**1. Version bump (BEFORE both builds)**

- `CHANGELOG.md`: new top entry `## [vX.Y.Z] — <date> — <headline>` with user-visible changes grouped (### Audio / Security / Installers …).
- `packaging/windows/MRRC-Modern.iss`: `MyAppVersion "X.Y.Z"`.

**2. Build macOS** → `macos-installer` skill. Record DMG **byte size + SHA-256** (printed by build.sh).

**3. Build Windows** → `windows-installer` skill. Record exe **byte size + SHA-256** (VM `Get-FileHash` == local `shasum`).

**4. Docs sync (needs the SHAs from steps 2–3)**

- `SDD/14-version-history.md`: new `| SDD V2.x |` row — release record with both artifacts' sizes + SHA-256 + verification summary.
- `SDD/README.md`: SDD Version, Baseline Date, Status line (latest release + artifact sizes).
- `docs/WINDOWS_INSTALLER_GUIDE.md`: Download table (file, size, SHA-256, mirrors) + the "built from main on Windows 11…" paragraph.
- `docs/MACOS_INSTALLER_GUIDE.md`: dmg filename + version mentions.
- `win_pack.md` / `mac_pack.md`: `> 最新构建：` header line (version, date, tests count, size, SHA-256).
- `website/index.html` + `website/zh/index.html`: hero badge, both download cards (version, MB, byte count, SHA-256, hrefs), Windows blurb paragraph, footer version; `website/guide.html` + `website/zh/guide.html`: dmg filename.
- Regenerate SDD pages: `python3 website/build_sdd.py`.
- Re-run the full suite (docs changes can break version-pinning tests).

**5. Deploy installers** (server-managed; `website/deploy.sh` EXCLUDES `downloads/`):

```bash
scp <artifact> www.vlsc.net:/tmp/<name>.new        # dmg, versioned exe, generic exe (3 files)
ssh www.vlsc.net 'for f in <names>; do
  sudo -n mv /tmp/$f.new /var/www/vlsc.net/mrrc_modern/downloads/$f
  sudo -n chown www-data:www-data /var/www/vlsc.net/mrrc_modern/downloads/$f
  sudo -n chmod 644 /var/www/vlsc.net/mrrc_modern/downloads/$f; done'
```

Old versioned installers stay as archives; only the generic `MRRC-Modern-Setup.exe` is replaced in place.

**6. Deploy HTML**: `cd website && echo y | ./deploy.sh` (interactive `read -p`; backup + `nginx -t` built in).

**7. Verify (all three URLs)**

```bash
curl -sI https://www.vlsc.net/mrrc_modern/downloads/MRRC-Modern-Setup.exe            # 200 + content-length == exe bytes
curl -sI .../downloads/MRRC-Modern-vX.Y.Z-Windows-x64-Setup.exe                       # 200 + same
curl -sI .../downloads/MRRC-Modern-vX.Y.Z-arm64.dmg                                   # 200 + dmg bytes
curl -s -o /tmp/v.exe .../downloads/MRRC-Modern-Setup.exe && shasum -a 256 /tmp/v.exe # == local SHA-256
curl -s https://www.vlsc.net/mrrc_modern/ | grep -c vX.Y.Z                            # site shows the new version
```

**8. Commit, tag, push**

```bash
git add -A
python3 .agents/skills/sdd-guardian/harness/sdd_context.py check --staged   # must exit 0
git commit -m "release: vX.Y.Z — <headline> ..."                             # embed sizes + SHAs
git tag vX.Y.Z
git push origin main && git push origin vX.Y.Z                               # tag pushed EXPLICITLY
git ls-remote --tags origin | grep vX.Y.Z                                    # verify it landed
```

## CRITICAL Gotchas

1. **`git push --follow-tags` does NOT push lightweight tags** — `git tag vX.Y.Z` (no `-a`) stays local and the release ships tag-less (v1.14.0 hit this). Always `git push origin vX.Y.Z` explicitly, then verify with `git ls-remote`.
2. **Order matters**: bump versions → build → only then write SHA-256/sizes into docs and website cards (the hashes don't exist before the build; the build reads the version from CHANGELOG). Writing docs first = stale-checksum release.
3. **Mac interpreter trap**: `PYTHON=$(pwd)/.venv/bin/python bash packaging/macos/build.sh`; `.venv/bin/python` for tests. Canary: any error path mentioning `mrrc_ft710/.venv` means the wrong interpreter was selected (`macos-installer` gotcha 10).
4. **Static-asset changes need cache-bust bumps** (`ft710_main.js?v=N`, service worker `mrrc-vN`) pinned by tests; download-page HTML edits don't.
5. **The v1.13.0 lesson**: docs-only releases drift — `docs/WINDOWS_INSTALLER_GUIDE.md` stayed on v1.12.1 through the whole v1.13.0 cycle. Step 4's checklist is the antidote; run it even when the release is "just installers".
6. **Post-release operator checks stay open**: real-QSO recording acceptance on the radio; TX audio on physical Windows hardware (the KVM VM can never verify it — `windows-installer` gotcha 4). Say so in the release summary.

## Release-Day Checklist

| # | Step | Gate / evidence |
| --- | ------ | ----------------- |
| 0 | Tests green + SDD brief | `693 tests OK` |
| 1 | CHANGELOG top entry + `.iss` version | `grep MyAppVersion` == CHANGELOG |
| 2 | macOS build | DMG bytes + SHA-256 recorded |
| 3 | Windows build (KVM VM) | VM hash == local hash |
| 4 | Docs + website sync + `build_sdd.py` | `grep -r vX.Y.Z` clean of old version (except CHANGELOG history) |
| 5 | Installers on <www.vlsc.net> | `ls -la downloads/` shows new files |
| 6 | HTML deploy | deploy.sh completes, nginx -t ok |
| 7 | URL + hash verification | 3× `HTTP/2 200`, content-length match, shasum match |
| 8 | Commit + tag + explicit push | `git ls-remote` shows tag; `check --staged` exit 0 |
