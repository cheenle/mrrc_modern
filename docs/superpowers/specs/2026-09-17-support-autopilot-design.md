# Support Autopilot and Answers Page (support chain 2/4)

**Date:** 2026-09-17

**Status:** Approved for implementation (decisions D1–D6 below, user review 2026-09-17)

**Scope:** After sub-project 1 shipped the report loop (AD-021), this adds the **analysis and reply** half:
a maintainer-side autopilot that polls the MRRC Modern receiver, fetches each new bundle, has a
code-aware agent analyse it against **this repository**, and turns the result into a published answer card.
Modeled on the sibling project's `dev_tools/support_autopilot.py` + `website/answers/` (the user's
instruction: "same as MRRC"), with MRRC Modern's own decision to rsync the single answers file.

## 1. Decisions taken during design review (2026-09-17)

| # | Question | Decision |
| --- | --- | --- |
| D1 | Analysis engine | **`pi` CLI** (`--thinking high --tools read,grep,find,ls`, cwd = repo root, read-only), like the sibling. The value is mapping log evidence onto real code (`REC_QUEUE_MAX` full → silent MP3 is not inferable from the log alone) |
| D2 | Publishing | **Draft by default** (`dist/support_answers/<id>.card.html`); `--publish` updates `website/answers/index.html`, commits, and rsyncs **that one file**. The cron line installs with `--publish`, so unattended runs do publish — but only when the analysis returned `answered` or `needs_fix` (never `need_more_info`) |
| D3 | Page language | **Chinese first** (matches the operation guide); the page keeps a `#<id>` deep link that pre-fills the search box |
| D4 | Page delivery | **Single-file publish** to `/var/www/vlsc.net/mrrc_modern/answers/index.html`, non-interactive (the full site deploy is interactive and must not run from cron). Two steps, because the docroot belongs to `www-data`: upload to the maintainer's home, then `sudo install -m 644 -o www-data`. A failed publish only warns — the local commit is the source of truth. (A direct rsync was the first attempt; it is refused *and* returns 0 when the file happens to be unchanged, so it looked healthy while writing nothing — measured 2026-09-17.) |
| D5 | Guardrails | Same set as the sibling: idempotent `state.json`, `--force <id>` to re-run, half-bundles (non-zip) skipped, **max 2 bundles per run**, 9-minute `pi` timeout, per-item failure never aborts the batch |
| D6 | Privacy | The answers page is **public**: rendering filters contact details, serial device names, private IPs and server-side paths, and the user's own `problem.txt` prose passes the same filter. Enforced by negative tests, not by review |

## 2. Non-goals

- One-click upgrade (sub-project 3) and hotfix packaging (4).
- Changing the receiver or the bundle format (sub-project 1 is frozen; the autopilot reads it as-is).
- Any answers UI inside the app/iOS/Android clients.
- Publishing raw logs. Only conclusions, evidence lines and steps go public.

## 3. Data flow

```
cron (every 10 min) → dev_tools/support_autopilot.py --once --publish
  GET  <receiver>/api/list            (Basic auth)  → ids not in state.json
  GET  <receiver>/api/<id>/bundle     → dist/support_inbox/<id>/bundle.zip → unpack
  read   problem.txt, diagnostics/summary.txt, diagnostics/env.json, manifest.json warnings
  run    pi --thinking high --tools read,grep,find,ls  (cwd = repo root, prompt carries the id + the summary)
  parse  the JSON contract (§4) — a malformed answer is a recorded failure, never a published card
  render support_answers.render_card() → draft; with --publish: rebuild the page, git commit, rsync 1 file
  write  state.json {id: {status, category, verdict, published, at}}
```

## 4. Output contract (identical to the sibling project)

```json
{"verdict":"一句话结论","status":"answered|needs_fix|need_more_info",
 "category":"环境|使用问题|产品缺陷|网络|升级|音频|电台|其他",
 "diagnosis":["要点（附证据）"],"solution":["用户可执行步骤"],"evidence":["原始行/字段"],
 "keys":["搜索关键词"],"needs_code_change":false,"code_hint":"文件/函数"}
```

Judgement discipline (in the prompt, asserted by tests): be conservative (insufficient material →
`need_more_info`); every entry in `solution` must be something the operator can actually do; environment
problems are not product defects; each diagnosis carries its raw evidence line; no "wait for the
maintainer to fix it" phrasing.

## 5. Units

| Unit | Responsibility | Depends on |
| --- | --- | --- |
| `support_answers.py` (repo root, stdlib only) | JSON contract validation, card rendering, **privacy filter**, page assembly (search + `#id` anchors), state file read/write | nothing (unit-testable) |
| `dev_tools/support_autopilot.py` | Poll/fetch/unpack, prompt building, `pi` invocation, draft/publish orchestration, git commit + single-file rsync, cron install/status | `support_answers`, receiver HTTP API, `pi` CLI |

## 6. Answers page

Replaces the placeholder: a search box (id or keyword), `#<id>` deep links that pre-fill it, and cards with
`编号 / 症状 / 诊断（附证据） / 结论 / 你要做的 / 状态`. Cards are sorted newest-first. Pure HTML+CSS+inline
JS, no build step, no framework — same shape as the guide pages so the site's deploy and audit rules keep
working.

## 7. Privacy rules (public page)

Never rendered: contact details from `problem.txt`, serial device names (`/dev/cu.*`, `COM*`), private IPs
(10/172.16–31/192.168), the local config path, the bundle id's owner name field, or any `MRRC_*` value that
is not on the diagnostic allow-list. The user's `problem.txt` prose is filtered too — an operator may type a
phone number or an e-mail into it.

## 8. Test plan (no hardware, no model, no network)

| Module | Coverage |
| --- | --- |
| `tests/test_support_answers.py` | contract validation (good/bad/incomplete JSON), draft vs published rendering, page assembly + `#id` anchors + search, state round-trip, **privacy negatives** (contact / serial / private IP / config path never in the rendered HTML) |
| `tests/test_support_autopilot.py` | idempotency (a bundle is processed once), `--force`, per-run cap of 2, half-bundle skip, bad JSON recorded as failure and **no page write**, `need_more_info` never publishes, cron line contains absolute paths + marker, `--inspect` does not invoke `pi` — all with a fake receiver and a fake `pi` |

## 9. Risks and acceptance boundaries

| Risk | Mitigation |
| --- | --- |
| Model misjudgement | Conservative default, draft-first workflow, every diagnosis carries its evidence line so a reader can check it, `--inspect` for a model-free pre-check |
| Cost | One `pi` call per bundle, max 2 bundles per run |
| Public page leaks operator data | Filter + negative tests (D6) |
| `pi` missing/broken | Recorded as a failure, page untouched, nothing half-published |
| rsync failure from cron | Warning only; the committed file is the source of truth and the next site deploy publishes it |

Not verified by this change: the quality of the model's conclusions on real field reports (that needs real
bundles — the first live run happens interactively with the maintainer, `--inspect` first), and unattended
cron behaviour over days.

## 10. SDD / doc traceability

- SDD/08: AD-021 amendment (the chain's second half) — the analysis engine and the publish rule.
- SDD/12.5.1: the operator/maintainer procedure gains "how an answer gets published".
- SDD/14: version row on completion; `docs/PROJECT_MAP.md`, `tests/README.md`, `AGENTS.md` module table,
  CHANGELOG, and the operation guide's §6 ("答复会发布在…" stops being aspirational).
