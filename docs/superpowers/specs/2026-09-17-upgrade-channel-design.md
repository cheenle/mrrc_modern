# One-Click Upgrade (support chain 3/4)

**Date:** 2026-09-17

**Status:** Approved for implementation, **in two slices** (see §1)

**Scope:** Let an installed MRRC Modern notice a newer release, verify it, and install it — with a
success criterion that cannot be faked. Modeled on the sibling project's update channel (`latest.json`
manifest, launcher-triggered download, `%LOCALAPPDATA%\MRRC\updates\state.json` self-proof, PTT refused
while transmitting).

## 1. Why two slices

Slice 2 (download → verify → run the installer → self-prove on next boot) modifies the machine that is
running the upgrade — a half-built version of it is worse than having no upgrade path at all. Slice 1
carries no self-modification: it fetches a manifest, compares versions, records state, and reports.
Each slice ships working, testable software; slice 2 starts only when it can be done and verified in one
sitting, on a real installer.

| Slice | Contents | Self-modifying |
| --- | --- | --- |
| **1 (this change)** | `upgrade_core.py` (manifest fetch/parse/compare, target selection, `state.json` schema), `dev_tools/make_latest_json.py` (publish the manifest from the real artifacts), `GET /api/update/check` (auth-gated, read-only), tests, docs | No |
| 2 (next) | Download into `<data dir>/updates/`, SHA-256 verification, PTT/recording gate (refuse while transmitting), launcher/UI entry, installer hand-off, `state.json.lastResult` **self-proof on the next boot**, rollback pointer | Yes |

## 2. Decisions

| # | Question | Decision |
| --- | --- | --- |
| D1 | Manifest location and shape | `https://www.vlsc.net/mrrc_modern/downloads/latest.json`, same shape as the sibling: `{latest, installer:{version,url,sha256,size}, previous:{...}, minSupported, mandatory, releasedAt, notes, hotfix:{...}}`. It is generated from the released artifacts, never hand-written |
| D2 | Version authority | `version.txt` inside each artifact (sub-project 1 already writes it on all three platforms) compared against `latest` in the manifest. Semver-ish triple compare, pre-release suffixes ignored for ordering |
| D3 | Success criterion | **`state.json.lastResult.status == "ok"` written by the NEW version when it boots** — the old version cannot claim success, and "installer launched" (`installing`) is explicitly not success. Same as the sibling project |
| D4 | PTT safety | An upgrade request is refused with **423** while the radio is transmitting or recording, and a download may run in the background but **never** installs while keyed. Re-checked immediately before hand-off (sub-project 4's hotfix channel reuses this rule) |
| D5 | Offline / failure | A failed check is silent (a maintenance screen must not nag); a failed download keeps the previous `state.json` and reports the reason. **Never** auto-install: the operator presses the button |
| D6 | Scope of "previous" | The manifest carries `previous` (the release before this one) so a rollback target exists; slice 2 must refuse a `previous` equal to the new version and the site must actually host it (`release_check --online` already verifies download URLs) |
| D7 | Verification in the suite | `upgrade_core` is pure logic (urlopen injected) → fully unit-testable; the manifest tool is tested against fabricated artifacts; `state.json`'s self-proof rule gets an explicit test that an old version cannot write `ok` |

## 3. Units (slice 1)

| Unit | Responsibility |
| --- | --- |
| `upgrade_core.py` (repo root, stdlib only) | `CURRENT_VERSION` resolution (reuses `support_bundle.detect_version`), `fetch_manifest(url)`, `parse_manifest(dict)`, `compare_versions(a, b)`, `check(current, manifest) -> UpdateStatus`, `state.json` read/write (`read_state`, `record_check`) |
| `dev_tools/make_latest_json.py` | Build `website/downloads/latest.json` from the CHANGELOG version + the artifact sizes/SHA-256 (arguments or a directory), refusing to write when a URL/size/hash is missing |
| `server.py` | `GET /api/update/check` (behind the existing auth middleware) → `{available, current, latest, notes, url, sha256, size, mandatory, releasedAt}`; never downloads, never writes |

## 4. Non-goals (slice 1)

No download, no installer execution, no UI banner (the SPA's two tooling-guarded JS files are deliberately
untouched), no hotfix channel (sub-project 4). `latest.json` is published to the site by the release flow,
not by the app.

## 5. Test plan (slice 1)

| Module | Coverage |
| --- | --- |
| `tests/test_upgrade_core.py` | version compare (equal/patch/minor/major, `v` prefix, pre-release suffix, malformed), manifest validation (missing keys/types, bad sha256 length, non-https url), `check()` for newer/equal/older/`mandatory`/`minSupported`, offline (urlopen raising) → no exception, state round-trip, and the self-proof rule (`ok` recorded only by the version that matches `latest`) |
| `tests/test_make_latest_json.py` | builds a valid manifest from fabricated artifacts, refuses on a missing file/SHA, keeps `previous` when given, is deterministic (no timestamp churn beyond `releasedAt`) |
| `tests/test_support_api.py` (extend) | `/api/update/check` needs auth (401), returns the manifest fields, and on a fetch failure reports `{available: false, reason}` instead of a 500 |

## 6. Risks

| Risk | Mitigation |
| --- | --- |
| A stale/hand-written manifest sends users to a bad build | The manifest is generated from artifacts whose size and SHA-256 come from the files themselves; `release_check.py --online` already verifies the download URLs it points at |
| A user upgrades while transmitting | D4: 423 while keyed, re-checked before hand-off in slice 2 |
| "It says it upgraded but nothing changed" | D3: only the new version's boot writes `ok`; slice 2's acceptance reads that file, not the installer's exit code |
| Offline machines nag | D5: a failed check is silent |

## 7. Traceability

- SDD/08: new **AD-022** (upgrade channel: generated manifest, self-proving state, PTT gate).
- SDD/12: the operational model gains the update procedure (check → download → verify → install → self-prove).
- SDD/14, `docs/PROJECT_MAP.md`, `tests/README.md`, `AGENTS.md`, CHANGELOG.
- Slice 2 additionally touches: launcher menu (macOS/Windows), the SPA banner (guarded files — see AGENTS.md),
  `packaging/**` (installer arguments), and the release flow (`latest.json` deployment + `previous` retention).
