#!/usr/bin/env python3
"""Support autopilot (spec: docs/superpowers/specs/2026-09-17-support-autopilot-design.md).

Polls the MRRC Modern receiver, analyses each new bundle with a code-aware agent
(`pi`, read-only tools, cwd = this repo) and turns the result into an answer card.

    python3 dev_tools/support_autopilot.py --inspect <编号>      # 只看摘要，不调模型
    python3 dev_tools/support_autopilot.py --once [--publish]   # 处理一轮（默认最多 2 条）
    python3 dev_tools/support_autopilot.py --force <编号> [--publish]
    python3 dev_tools/support_autopilot.py --status             # 已处理清单
    python3 dev_tools/support_autopilot.py --install-cron       # 每 10 分钟（带 --publish）

Guardrails (spec §1 D5): idempotent state.json, half-bundles skipped, at most
MAX_PER_RUN bundles per run, PI_TIMEOUT per call, per-item failure never aborts
the batch, and **nothing is published unless --publish is given** — and even then
only for `answered` / `needs_fix` (never `need_more_info`).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

import support_answers as answers                                    # noqa: E402

RECEIVER_URL = os.environ.get("MRRC_SUPPORT_URL",
                             "https://www.vlsc.net/mrrc_modern/support/").rstrip("/")
CRED_FILE = Path(os.environ.get("MRRC_SUPPORT_CREDENTIALS",
                                str(Path.home() / ".mrrc-support-credentials.txt")))
SUPPORT_USER = os.environ.get("SUPPORT_USER", "mrrc")
STATE_FILE = REPO / "dist" / "support_autopilot" / "state.json"
RUN_LOG = REPO / "dist" / "support_autopilot" / "run.log"
INBOX = REPO / "dist" / "support_inbox"
DRAFTS = REPO / "dist" / "support_answers"
ANSWERS_PAGE = REPO / "website" / "answers" / "index.html"
CRON_MARK = "# MRRC Modern support autopilot"
REMOTE_HOST = os.environ.get("MRRC_SUPPORT_DEPLOY_HOST", "cheenle@www.vlsc.net")
REMOTE_PAGE = "/var/www/vlsc.net/mrrc_modern/answers/index.html"
MAX_PER_RUN = 2
# Env override keeps the smoke run and cron tuning cheap (cron uses the default).
PI_TIMEOUT = int(os.environ.get("MRRC_AUTOPILOT_PI_TIMEOUT", "540"))
# "high" spends minutes exploring this repo (measured: >7 min on one real digest,
# vs 3.4 s on a trivial prompt); "medium" is the tuned default for unattended runs.
PI_THINKING = os.environ.get("MRRC_AUTOPILOT_THINKING", "medium")

PROMPT_TEMPLATE = """你是 MRRC Modern 的支持工程师。仓库就是你当前的工作目录（只读检索：read/grep/find/ls）。

下面是一条用户「🐞 遇到问题」上报生成的诊断包摘要。请：
1. 只在包内证据 + 仓库代码里找依据，**不要猜测**；证据不足时把 status 设为 need_more_info；
   **探查预算：最多检索 5 次、最多读 6 个文件**；先 grep 日志里的关键词（如错误原文、函数名），
   不要遍历目录、不要通读整个仓库。预算内没定位到就判 need_more_info，这不算失败；
2. 每条诊断都要带上它依据的原始行/字段（evidence）；
3. solution 必须是**用户自己能执行**的步骤（改配置、换设备、重启、避开某个用法），
   不要写"等维护者修"；
4. 环境类问题（没有音频设备、虚拟机、串口被占用）不算产品缺陷；
5. 只有在代码里定位到具体缺陷时才把 needs_code_change 设为 true，并在 code_hint 写「文件:函数」；
6. 结尾**只输出一个 JSON 对象**，不要额外文字：

{{"verdict":"一句话结论","status":"answered|needs_fix|need_more_info",
 "category":"环境|使用问题|产品缺陷|网络|升级|音频|电台|其他",
 "diagnosis":["要点（附证据）"],"solution":["用户可执行步骤"],"evidence":["原始行/字段"],
 "keys":["搜索关键词"],"needs_code_change":false,"code_hint":"文件/函数"}}

诊断包编号：{rid}
{separator}
{digest}
"""


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line, flush=True)
    try:
        RUN_LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(RUN_LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


# ── receiver access ────────────────────────────────────────────────────────
def password() -> str:
    """Basic-auth password: env first, then the maintainer's credential file."""
    env = os.environ.get("SUPPORT_PASSWORD")
    if env:
        return env
    try:
        return CRED_FILE.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise RuntimeError(f"读不到接收端口令（{CRED_FILE}）：{e}") from e


def _auth_header() -> str:
    return "Basic " + base64.b64encode(f"{SUPPORT_USER}:{password()}".encode()).decode()


def http_get(path: str) -> bytes:
    request = urllib.request.Request(f"{RECEIVER_URL}{path}", headers={"Authorization": _auth_header()})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def list_bundles() -> list[str]:
    """Every bundle id on the receiver (the list page is the only JSON-free view)."""
    page = http_get("/api/list").decode("utf-8", "replace")
    return sorted(set(re.findall(r"<b>(\d{8}-\d{6}-[0-9a-f]{4})</b>", page)))


def fetch_bundle(bundle_id: str) -> Path:
    """Download one bundle; a non-zip answer (half upload) is refused here."""
    if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", bundle_id):
        raise ValueError(f"编号不合法：{bundle_id!r}")
    blob = http_get(f"/api/{bundle_id}/bundle")
    if not blob.startswith(b"PK"):
        raise ValueError(f"{bundle_id} 不是完整的 zip（半包，跳过）")
    target = INBOX / bundle_id
    target.mkdir(parents=True, exist_ok=True)
    path = target / "bundle.zip"
    path.write_bytes(blob)
    return path


def unpack_bundle(bundle_id: str) -> Path:
    """Unpack a downloaded bundle into its inbox folder and return the folder."""
    path = fetch_bundle(bundle_id)
    folder = path.parent
    with zipfile.ZipFile(path) as archive:
        for name in archive.namelist():
            if name.startswith("/") or ".." in name:
                raise ValueError(f"压缩包内有可疑路径：{name}")
            archive.extract(name, folder)
    return folder


def collect_context(folder: Path, bundle_id: str) -> str:
    """The digest the model sees: the operator's words plus our own triage.

    Note: the operator's raw text *does* reach the model (the maintainer's own
    machine) — the privacy filter runs at render time, on the public page.
    """
    parts = [f"### 编号\n{bundle_id}"]
    for name, limit in (("problem.txt", 4000), ("diagnostics/summary.txt", 6000),
                        ("diagnostics/env.json", 4000), ("manifest.json", 3000)):
        path = folder / name
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        parts.append(f"### {name}\n{text[:limit]}")
    for log_name in sorted((folder / "logs").glob("*.log")):
        try:
            text = log_name.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        tail = "\n".join(text.splitlines()[-120:])
        parts.append(f"### logs/{log_name.name}（尾部 120 行）\n{tail}")
    return "\n\n".join(parts)


# ── model ──────────────────────────────────────────────────────────────────
def pi_binary() -> str:
    """Locate `pi` explicitly: cron's PATH is narrow."""
    env = os.environ.get("PI_BIN")
    if env and Path(env).exists():
        return env
    found = shutil.which("pi")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/pi", "/usr/local/bin/pi",
                      str(Path.home() / ".hermes/node/bin/pi"),
                      str(Path.home() / ".local/bin/pi")):
        if Path(candidate).exists():
            return candidate
    raise RuntimeError("找不到 pi 可执行文件（设 PI_BIN，或把 pi 装到 PATH 里）")


def run_pi(digest: str, bundle_id: str, model: str = "") -> dict:
    """One non-interactive analysis call; the contract lives in support_answers."""
    prompt = PROMPT_TEMPLATE.format(rid=bundle_id, digest=digest, separator="--- 诊断包内容 ---")
    cmd = [pi_binary(), "--name", f"support-{bundle_id}", "--tools", "read,grep,find,ls",
           "--thinking", PI_THINKING, "-p", prompt]
    if model:
        cmd[1:1] = ["--model", model]
    log(f"[{bundle_id}] 调用 pi 分析（超时 {PI_TIMEOUT}s）…")
    # stdin=DEVNULL: `pi` would otherwise wait for EOF on an inherited pipe.
    # Cron passes /dev/null so the sibling project never hit this; a run from an
    # interactive/piped shell hung until the timeout (found in the live smoke run).
    proc = subprocess.run(cmd, cwd=str(REPO), capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, timeout=PI_TIMEOUT)
    output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if proc.returncode != 0:
        log(f"[{bundle_id}] pi 退出码 {proc.returncode}")
    return answers.parse_analysis(output)


# ── publishing ─────────────────────────────────────────────────────────────
def write_draft(bundle_id: str, analysis: dict, problem: str) -> Path:
    DRAFTS.mkdir(parents=True, exist_ok=True)
    path = DRAFTS / f"{bundle_id}.card.html"
    path.write_text(answers.render_card(analysis, bundle_id, problem=problem,
                                        at=time.strftime("%Y-%m-%d %H:%M")),
                    encoding="utf-8")
    return path


def rebuild_page(state: dict, cards_dir: Path | None = None) -> Path:
    """Assemble the public page from the cards of published entries."""
    cards = []
    for bundle_id, record in answers.answered_cards(state):
        card_path = (cards_dir or DRAFTS) / f"{bundle_id}.card.html"
        try:
            cards.append(card_path.read_text(encoding="utf-8").rstrip())
        except OSError:
            log(f"[{bundle_id}] 草稿卡缺失，页面里跳过")
            continue
    page = answers.render_page(cards)
    ANSWERS_PAGE.parent.mkdir(parents=True, exist_ok=True)
    ANSWERS_PAGE.write_text(page, encoding="utf-8")
    return ANSWERS_PAGE


def git_commit(bundle_id: str) -> bool:
    try:
        subprocess.run(["git", "add", "website/answers/index.html"], cwd=str(REPO), check=True)
        subprocess.run(["git", "commit", "-q", "-m",
                        f"support(answers): 自动答复 {bundle_id}（support autopilot）"],
                       cwd=str(REPO), check=True)
        return True
    except subprocess.CalledProcessError as e:
        log(f"[{bundle_id}] git commit 失败：{e}")
        return False


STAGING = "~/mrrc_modern_answers.html"


def rsync_page() -> bool:
    """Ship just the answers page (spec §1 D4); the full-site deploy stays manual.

    Two steps on purpose: the docroot belongs to www-data, so a direct rsync into
    it is refused — and it *silently* returns 0 when the file happens to be
    unchanged, which is how the first "verification" of this path passed while
    being unable to write anything (measured 2026-09-17).  Upload to the
    maintainer's home, then install it into place as www-data.
    """
    try:
        subprocess.run(["rsync", "-az", str(ANSWERS_PAGE), f"{REMOTE_HOST}:{STAGING}"],
                       check=True, capture_output=True)
        subprocess.run(["ssh", REMOTE_HOST,
                        f"sudo install -m 644 -o www-data -g www-data {STAGING} {REMOTE_PAGE}"],
                       check=True, capture_output=True)
        return True
    except (OSError, subprocess.CalledProcessError) as e:
        detail = getattr(e, "stderr", b"") or b""
        log(f"答复页发布失败（本地 commit 已保留）：{e} {detail.decode('utf-8', 'replace')[:200]}")
        return False


def publish(bundle_id: str, analysis: dict, problem: str, state: dict, push: bool = False) -> bool:
    """Write the card, rebuild the page, commit, rsync.  Draft-only on refusal."""
    card_path = write_draft(bundle_id, analysis, problem)
    state = answers.record_result(state, bundle_id, analysis, published=True)
    answers.save_state(STATE_FILE, state)
    rebuild_page(state)
    committed = git_commit(bundle_id)
    if push:
        try:
            subprocess.run(["git", "push", "origin", "main"], cwd=str(REPO), check=True)
        except (OSError, subprocess.CalledProcessError) as e:
            log(f"[{bundle_id}] git push 失败（本地 commit 已保留）：{e}")
    rsync_page()
    log(f"[{bundle_id}] 已发布答复（草稿 {card_path.name}）")
    return committed


# ── one bundle ─────────────────────────────────────────────────────────────
def process_one(bundle_id: str, publish_it: bool, model: str = "", push: bool = False,
                state: dict | None = None) -> str:
    """Returns one of: published | drafted | need_more_info | failed."""
    state = answers.load_state(STATE_FILE) if state is None else state
    try:
        folder = unpack_bundle(bundle_id)
        problem = ""
        try:
            problem = (folder / "problem.txt").read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
        digest = collect_context(folder, bundle_id)
        try:
            analysis = run_pi(digest, bundle_id, model=model)
        except ValueError as e:
            log(f"[{bundle_id}] 模型输出不合契约：{e}")
            state = answers.record_result(state, bundle_id, {"status": "failed"}, published=False)
            answers.save_state(STATE_FILE, state)
            return "failed"
        write_draft(bundle_id, analysis, problem)
        if not publish_it:
            log(f"[{bundle_id}] 草稿已生成（未加 --publish）：{DRAFTS / (bundle_id + '.card.html')}")
            return "drafted"
        if not answers.should_publish(analysis):
            state = answers.record_result(state, bundle_id, analysis, published=False)
            answers.save_state(STATE_FILE, state)
            log(f"[{bundle_id}] {analysis['status']}：不发布，草稿留在本地")
            return "need_more_info"
        publish(bundle_id, analysis, problem, state, push=push)
        return "published"
    except Exception as e:                    # one bad bundle never ends the batch
        log(f"[{bundle_id}] 处理失败：{type(e).__name__}: {e}")
        return "failed"


def run_once(publish_it: bool, model: str = "", limit: int = MAX_PER_RUN,
             push: bool = False) -> list[tuple[str, str]]:
    state = answers.load_state(STATE_FILE)
    pending = [bid for bid in list_bundles() if bid not in state]
    results = []
    for bundle_id in pending[:max(0, limit)]:
        results.append((bundle_id, process_one(bundle_id, publish_it, model=model,
                                              push=push, state=state)))
        state = answers.load_state(STATE_FILE)
    if not pending:
        log("没有新包")
    return results


# ── maintenance commands ───────────────────────────────────────────────────
def inspect_bundle(bundle_id: str) -> int:
    """Print the digest without calling the model (the first-live-run pre-check)."""
    folder = unpack_bundle(bundle_id)
    problem = ""
    try:
        problem = (folder / "problem.txt").read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    print(f"=== {bundle_id} ===")
    print(problem.strip() or "(未填写问题描述)")
    summary = folder / "diagnostics" / "summary.txt"
    if summary.exists():
        print(summary.read_text(encoding="utf-8", errors="replace"))
    return 0


def show_status() -> int:
    state = answers.load_state(STATE_FILE)
    if not state:
        print("还没有处理过任何包")
        return 0
    for bundle_id, record in answers.answered_cards(state):
        print(f"{record.get('at', '?')}  {bundle_id}  {record.get('status', '?'):12} "
              f"{record.get('category', '?'):6} 已发布")
    for bundle_id, record in sorted(state.items()):
        if not record.get("published"):
            print(f"{record.get('at', '?')}  {bundle_id}  {record.get('status', '?'):12} "
                  f"{record.get('category', '?'):6} 未发布")
    return 0


def cron_line() -> str:
    """Absolute paths + explicit PATH: cron's environment is nearly empty."""
    return (f"*/10 * * * * PATH=/opt/homebrew/bin:/usr/local/bin:{Path.home()}/.hermes/node/bin "
            f"{sys.executable} {REPO / 'dev_tools' / 'support_autopilot.py'} --once --publish "
            f">> {RUN_LOG} 2>&1 {CRON_MARK}")


def install_cron() -> int:
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    kept = [line for line in current.splitlines() if CRON_MARK not in line]
    kept.append(cron_line())
    subprocess.run(["crontab", "-"], input="\n".join(kept).strip() + "\n", text=True, check=True)
    print("已安装 crontab（每 10 分钟）：")
    print("  " + cron_line())
    return 0


def uninstall_cron() -> int:
    current = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    kept = [line for line in current.splitlines() if CRON_MARK not in line]
    subprocess.run(["crontab", "-"], input="\n".join(kept).strip() + "\n", text=True, check=True)
    print("已移除 crontab 条目")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="MRRC Modern support autopilot")
    parser.add_argument("--once", action="store_true", help="处理一轮未读的包")
    parser.add_argument("--publish", action="store_true",
                        help="把 answered/needs_fix 的结论发布到答复页（默认只出草稿）")
    parser.add_argument("--push", action="store_true", help="发布后额外 git push（默认不推）")
    parser.add_argument("--force", metavar="编号", help="强制重跑某条（忽略 state.json）")
    parser.add_argument("--inspect", metavar="编号", help="只看摘要，不调用模型")
    parser.add_argument("--status", action="store_true", help="列出已处理条目")
    parser.add_argument("--install-cron", action="store_true", help="安装每 10 分钟的 crontab")
    parser.add_argument("--uninstall-cron", action="store_true", help="移除 crontab 条目")
    parser.add_argument("--model", default="", help="传给 pi 的 --model")
    parser.add_argument("--limit", type=int, default=MAX_PER_RUN, help=f"单轮上限（默认 {MAX_PER_RUN}）")
    args = parser.parse_args(argv)

    if args.inspect:
        return inspect_bundle(args.inspect)
    if args.status:
        return show_status()
    if args.install_cron:
        return install_cron()
    if args.uninstall_cron:
        return uninstall_cron()
    if args.force:
        result = process_one(args.force, args.publish, model=args.model, push=args.push)
        print(f"{args.force}: {result}")
        return 0 if result != "failed" else 1
    if args.once:
        results = run_once(args.publish, model=args.model, limit=args.limit, push=args.push)
        for bundle_id, result in results:
            print(f"{bundle_id}: {result}")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
