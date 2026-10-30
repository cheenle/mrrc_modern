"""KB 生成器：同输入同输出，且「CHANGELOG 多一条 → KB 多一条」。"""
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location(
    "make_assistant_kb", REPO / "dev_tools" / "make_assistant_kb.py")
assert _SPEC is not None and _SPEC.loader is not None
mk = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mk)


CHANGELOG = """# Changelog

## [镜像线 v1.28.22 · FP18] — 2026-10-30 — CQ 泵按队列深度补料

**修法**：泵改成按队列深度补料，平均速率由声卡决定。

## [v1.28.6] — 2026-10-26 — 盒子级音频电平

TX MIC 与 RX 各一个 dB 旋钮。
"""


class BuildTests(unittest.TestCase):
    def _build(self, changelog=CHANGELOG):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
            return mk.build(root)

    def test_one_changelog_heading_one_entry(self):
        kb = self._build()
        kinds = [e["kind"] for e in kb["entries"]]
        self.assertEqual(kinds.count("release"), 2)

    def test_box_line_heading_carries_version_and_fixpack(self):
        entries = [e for e in self._build()["entries"] if e["kind"] == "release"]
        box = [e for e in entries if e["fixpack"]][0]
        self.assertEqual(box["applies_to"]["version_min"], "1.28.22")
        self.assertEqual(box["fixpack"], "FP18")

    def test_ids_are_stable_and_output_reproducible(self):
        first = json.dumps(self._build(), ensure_ascii=False, sort_keys=True)
        second = json.dumps(self._build(), ensure_ascii=False, sort_keys=True)
        self.assertEqual(first, second)

    def test_entries_have_a_searchable_body_and_keywords(self):
        for entry in self._build()["entries"]:
            self.assertTrue(entry["body"].strip())
            self.assertTrue(entry["keywords"])

    def test_newest_box_version_is_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
            self.assertEqual(mk.newest_box_version(root), "1.28.22")


class FreshnessTests(unittest.TestCase):
    """仓库里那份 assistant_kb.json 必须覆盖最新的镜像线版本。

    这条守卫替掉了「知识库进发布注册表」的规则：注册表的 `expect: box` 语义是
    「每个匹配都等于镜像线版本」，而知识库里天然有几十个历史版本，规则会全红。
    真正要防的是一件事 —— **发了版但助手不知道**，用一条测试直接钉住更清楚。
    """

    def test_committed_kb_covers_the_newest_box_line(self):
        kb = json.loads((REPO / "tools" / "assistant_gateway" / "assistant_kb.json")
                        .read_text(encoding="utf-8"))
        versions = {e["applies_to"]["version_min"] for e in kb["entries"]}
        self.assertIn(mk.newest_box_version(REPO), versions,
                      "知识库没覆盖最新的镜像线版本 —— 跑 dev_tools/make_assistant_kb.py")

    def test_committed_kb_matches_a_fresh_build(self):
        """仓库里那份必须是当前 CHANGELOG 的产物，否则它就是"陈旧但看起来正常"。"""
        committed = json.loads((REPO / "tools" / "assistant_gateway" / "assistant_kb.json")
                               .read_text(encoding="utf-8"))
        self.assertEqual(committed, mk.build(REPO))
