"""Outbound TLS trust: a packaged build must verify certificates where the user is, not
only on the machine that built it (2026-10-04 field report).

Why these tests exist
---------------------
The macOS v1.25.0 bundle carried the *build* host's OpenSSL (MacPorts), whose compiled-in
CA path is ``/opt/local/libexec/openssl3/etc/openssl/cert.pem`` — a path no user has.  The
trust store therefore came up empty and every outbound HTTPS request failed with
``CERTIFICATE_VERIFY_FAILED: unable to get local issuer certificate``: the Cloud Hub portal
(呼号接入), the 🐞 diagnostics upload (the report had to be handed over by path because of
it) and the update check.  The build machine is the one place that cannot notice, so the
empty store is reproduced deliberately here and in ``dev_tools/tls_trust_gate.py``.

The last class keeps new call sites on ``net_tls.urlopen``: a fix that only holds where
someone remembers to pass a context is the same bug one file later.
"""
from __future__ import annotations

import ast
import os
import ssl
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import net_tls

REPO_ROOT = Path(__file__).resolve().parents[1]
SHIPPED_BUNDLE = REPO_ROOT / "vendor" / "ca" / "cacert.pem"

#: Directories that hold code the app never runs (tests, build tooling, dev scripts).
#: ``FT710Android`` 与 ``promo`` 和 ``FT710Mobile`` 同理，而且 win_pack.md 的源码包排除表
#: 也不带它们 —— 两边不一致，这个闸门就会在构建机上数出一个和 macOS 不同的模块数
#: （2026-10-06 实测：macOS 53 / 出货树 50，阈值 ``> 50`` 正好一刀砍在出货树上）。
_SKIP_DIRS = {"tests", "dev_tools", "tools", "packaging", "dist", "build", "venv",
              "node_modules", "__pycache__", "payload", "static", "website", "docs",
              "SDD", "FT710Mobile", "FT710Android", "promo"}


def _empty_context() -> ssl.SSLContext:
    """A verifying context with no roots at all — the user-machine state."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.verify_mode = ssl.CERT_REQUIRED
    context.check_hostname = True
    return context


class EmptyStoreTests(unittest.TestCase):
    def test_scrubbed_environment_reproduces_the_field_failure(self):
        """POSIX: SSL_CERT_FILE/DIR pointed at nothing ⇒ empty store, and net_tls still verifies.

        Windows is the other half of the same fact: `create_default_context()` reads the **OS
        certificate store** there (not SSL_CERT_FILE), which is exactly why this defect never
        bit Windows — and why the scrub cannot empty the store on that platform.  What must
        hold on both is that net_tls ends up with roots.
        """
        broken = {"SSL_CERT_FILE": str(REPO_ROOT / "no-such-ca.pem"),
                  "SSL_CERT_DIR": str(REPO_ROOT / "no-such-ca-dir")}
        with patch.dict(os.environ, broken):
            os.environ.pop(net_tls.CA_BUNDLE_ENV, None)
            default_roots = net_tls.store_size(ssl.create_default_context())
            if sys.platform == "win32":
                self.assertGreater(default_roots, 0,
                                   "Windows uses its own certificate store; if this is 0 the "
                                   "OS store itself is unusable and the check below is the only one")
            else:
                self.assertEqual(0, default_roots,
                                 "the scrub must actually empty the store, or this test is vacuous")
            roots = net_tls.store_size(net_tls.build_context())
        self.assertGreater(roots, 0, "net_tls must find a CA bundle when the default store is empty")

    def test_a_broken_candidate_does_not_stop_the_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "gone.pem"
            garbage = Path(tmp) / "garbage.pem"
            garbage.write_text("this is not a certificate\n", encoding="utf-8")
            context = net_tls.build_context(base=_empty_context(),
                                            candidates=[missing, garbage, SHIPPED_BUNDLE])
        self.assertGreater(net_tls.store_size(context), 0)

    def test_no_candidate_leaves_the_context_usable_and_says_so(self):
        with self.assertLogs("mrrc.tls", level="WARNING") as logs:
            context = net_tls.build_context(base=_empty_context(), candidates=[])
        self.assertEqual(0, net_tls.store_size(context))
        self.assertIn(net_tls.CA_BUNDLE_ENV, "\n".join(logs.output))

    def test_a_healthy_store_is_left_alone(self):
        healthy = ssl.create_default_context()
        healthy.load_verify_locations(cafile=str(SHIPPED_BUNDLE))
        bogus = Path("/nonexistent/bogus.pem")
        self.assertIs(healthy, net_tls.build_context(base=healthy, candidates=[bogus]))


class BundleTests(unittest.TestCase):
    def test_the_shipped_bundle_is_a_real_ca_file(self):
        self.assertTrue(SHIPPED_BUNDLE.is_file(), f"missing {SHIPPED_BUNDLE}")
        context = _empty_context()
        context.load_verify_locations(cafile=str(SHIPPED_BUNDLE))
        self.assertGreater(net_tls.store_size(context), 20,
                           "a Mozilla-derived bundle has ~120 roots; a stub means the "
                           "packaged app cannot verify anything")

    def test_candidates_start_with_an_explicit_override(self):
        with tempfile.NamedTemporaryFile(suffix=".pem") as handle:
            with patch.dict(os.environ, {net_tls.CA_BUNDLE_ENV: handle.name}):
                candidates = net_tls.ca_bundle_candidates()
        self.assertEqual(Path(handle.name), candidates[0])

    def test_candidates_include_the_bundle_and_the_system_files(self):
        candidates = net_tls.ca_bundle_candidates()
        self.assertIn(SHIPPED_BUNDLE, candidates)
        for system_file in net_tls.SYSTEM_CA_FILES:
            self.assertIn(Path(system_file), candidates)


class UrlopenTests(unittest.TestCase):
    def test_urlopen_injects_the_process_context(self):
        with patch("urllib.request.urlopen", MagicMock(return_value=MagicMock())) as fake:
            net_tls.urlopen("https://example.com/", timeout=7)
        args, kwargs = fake.call_args
        self.assertEqual("https://example.com/", args[0])
        self.assertEqual(7, kwargs["timeout"])
        self.assertIs(net_tls.default_context(), kwargs["context"])

    def test_an_explicit_context_wins(self):
        mine = _empty_context()
        with patch("urllib.request.urlopen", MagicMock(return_value=MagicMock())) as fake:
            net_tls.urlopen("https://example.com/", context=mine)
        self.assertIs(mine, fake.call_args.kwargs["context"])


class CallSiteGuardTests(unittest.TestCase):
    """Every direct ``urlopen`` in shipped code must set a context (or go through net_tls)."""

    #: 出货树里必定存在的模块 —— 扫描集里必须看得见它们。
    _MUST_SEE = ("server.py", "cloud_hub.py", "net_tls.py", "launcher_net.py")

    def _offenders(self) -> tuple[list[str], set[str]]:
        offenders: list[str] = []
        scanned: set[str] = set()
        for path in sorted(REPO_ROOT.rglob("*.py")):
            # 相对 REPO_ROOT 判断：绝对路径里可能带 .worktrees（git worktree）或任何以点
            # 开头的父目录，那与"出货树里有没有隐藏目录"无关。按绝对路径判断会让整个
            # worktree 被跳过、scanned 为空，于是这条守卫在每个 worktree 会话里都是红的
            # —— 而红的原因是它看不见代码，不是代码有问题。
            relative = path.relative_to(REPO_ROOT).parts
            if set(relative) & _SKIP_DIRS or any(part.startswith(".") for part in relative):
                continue
            if path.name.startswith("_") or path.name.startswith("test_"):
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            try:
                tree = ast.parse(source)
            except SyntaxError:                    # not ours to police here
                continue
            scanned.add(str(path.relative_to(REPO_ROOT)))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if isinstance(func, ast.Attribute):
                    if func.attr != "urlopen":
                        continue
                    # net_tls.urlopen is the wrapper: it supplies the context itself.
                    if isinstance(func.value, ast.Name) and func.value.id == "net_tls":
                        continue
                elif not (isinstance(func, ast.Name) and func.id == "urlopen"):
                    continue
                if not any(keyword.arg == "context" for keyword in node.keywords):
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{node.lineno}")
        return offenders, scanned

    def test_the_hidden_directory_filter_is_relative_to_the_repo(self):
        """A git worktree lives under ``.worktrees/``, which starts with a dot.

        Filtering on the absolute path's parts therefore skips every file in the
        tree and the scan comes back empty. The superpowers workflow always runs
        in a worktree, so this guard was red in every such session — and it was
        red because it could not see the code, not because the code was wrong.
        """
        offenders, scanned = self._offenders()
        self.assertIn("server.py", scanned)
        self.assertIn("net_tls.py", scanned)
        self.assertEqual([], offenders)

    def test_every_call_site_sets_a_context(self):
        offenders, scanned = self._offenders()
        # 原来的 assertGreater(scanned, 50) 是个照开发机选的魔数：macOS 上 53 个模块，
        # 源码包装到构建机上只有 50 个，于是同一份代码在 macOS 绿、在**出货树**上红
        # （2026-10-06 实测）—— 闸门自己成了噪声。换成内容对照：名字不随模块增删漂移，
        # 而扫描一旦空转（REPO_ROOT 指错、过滤写反）它必定失败。
        for required in self._MUST_SEE:
            self.assertIn(required, scanned, "the scan must actually look at the shipped tree")
        self.assertGreater(len(scanned), 40, "the scan found almost nothing")
        self.assertEqual([], offenders,
                         "outbound urlopen without context= — use net_tls.urlopen instead "
                         "(2026-10-04: a build without a trust store failed every HTTPS call)")


if __name__ == "__main__":
    unittest.main()
