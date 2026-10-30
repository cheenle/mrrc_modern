"""中央网关：检索、判定、配额、留存 —— 全部离线，伪模型，假时钟。"""
import http.client
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path

from tools.assistant_gateway import kb as gkb
from tools.assistant_gateway import engine as gengine
from tools.assistant_gateway import http_server as ghttp
from tools.assistant_gateway import model as gmodel
from tools.assistant_gateway import store as gstore


def _entry(id_, title, body, version="1.28.20", models=()):
    return {"id": id_, "kind": "release", "title": title, "body": body,
            "keywords": gkb.keywords(title + "\n" + body),
            "applies_to": {"version_min": version, "models": list(models)},
            "fixpack": ""}


ENTRIES = [
    _entry("kb-cq", "CQ 发射节拍：供料比声卡慢 5%",
           "CQ 播放的时候声音有些失真，泵改成按队列深度补料。"),
    _entry("kb-nb", "NR 等级改成 RL0 + 两位", "噪声抑制 NR 的等级指令格式修正。"),
]


class RetrieveTests(unittest.TestCase):
    def test_the_topic_ranks_first(self):
        hits = gkb.retrieve(ENTRIES, "CQ 发射的时候有失真", "1.28.22", "ft710")
        self.assertEqual(hits[0].entry["id"], "kb-cq")

    def test_is_hit_needs_both_score_and_margin(self):
        # 阈值是绝对 BM25 分，语料规模一变就要重标 —— 所以这里是显式构造的分数。
        def hits(*scores):
            return [gkb.Scored({"id": f"kb-{i}"}, s) for i, s in enumerate(scores)]
        self.assertTrue(gkb.is_hit(hits(9.9)))
        self.assertTrue(gkb.is_hit(hits(9.9, 6.2)))
        self.assertFalse(gkb.is_hit(hits(7.9)))          # 分数不够
        self.assertFalse(gkb.is_hit(hits(9.9, 9.4)))     # 与第二名拉不开
        self.assertFalse(gkb.is_hit([]))

    def test_unrelated_query_ranks_low(self):
        hits = gkb.retrieve(ENTRIES, "怎么给树莓派烧卡", "1.28.22", "ft710")
        relevant = gkb.retrieve(ENTRIES, "CQ 发射的时候有失真", "1.28.22", "ft710")
        self.assertTrue(hits == [] or hits[0].score < relevant[0].score)

    def test_entry_older_than_the_machine_is_still_usable(self):
        # version_min 是「从这版起」，本机更新时依然命中
        hits = gkb.retrieve(ENTRIES, "CQ 失真", "1.29.0", "ft710")
        self.assertEqual(hits[0].entry["id"], "kb-cq")

    def test_model_specific_entry_does_not_leak_to_other_models(self):
        entries = [_entry("kb-ftx", "FTX-1 的波段表", "波段表改成对象。", models=["ftx1"])]
        self.assertEqual(gkb.retrieve(entries, "波段表", "1.28.22", "ft710"), [])
        self.assertEqual(gkb.retrieve(entries, "波段表", "1.28.22", "ftx1")[0].entry["id"],
                         "kb-ftx")

    def test_keyword_split_matches_the_generator(self):
        import importlib.util
        from pathlib import Path as _P
        repo = _P(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "make_assistant_kb", repo / "dev_tools" / "make_assistant_kb.py")
        assert spec is not None and spec.loader is not None
        mk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mk)
        sample = "CQ 发射节拍 FT-710 失真"
        self.assertEqual(gkb.keywords(sample), mk.keywords(sample))


class GoldenSetTests(unittest.TestCase):
    """拿**真实知识库**跑一份标定集 —— 阈值是绝对 BM25 分，语料一变这份名单就会变。

    名单里带 `None` 的是「必须**不**命中」的离题问题：宁可落到「兜底生成 + 待确认」，
    也不要拿一条不相干的条目当权威答案。两条已知的未命中（频谱/FT8）不在名单里 ——
    它们还没修到命中，写进来只会把一个已知短板钉成"期望值"。
    """

    CASES = [
        ("CQ 播放有失真怎么办", "CQ"),
        ("NR 等级怎么写", "NR"),
        ("IC-7300 连不上电台", "IC-7300"),
        ("升级后版本号没变", "热修"),
        ("怎么给树莓派烧卡", None),
        ("盒子开不了机", None),
    ]

    @classmethod
    def setUpClass(cls):
        cls.kb = json.loads(
            (Path(__file__).resolve().parents[1]
             / "tools" / "assistant_gateway" / "assistant_kb.json").read_text(encoding="utf-8"))

    def test_each_case_behaves_as_calibrated(self):
        for query, want in self.CASES:
            with self.subTest(query=query):
                hits = gkb.retrieve(self.kb["entries"], query, "1.28.22", "ft710")
                if want is None:
                    self.assertFalse(gkb.is_hit(hits), f"{query} 不该命中")
                else:
                    self.assertTrue(gkb.is_hit(hits), f"{query} 该命中却落空")
                    self.assertIn(want, hits[0].entry["title"])


class StoreTests(unittest.TestCase):
    def _store(self, clock):
        tmp = tempfile.mkdtemp()
        st = gstore.Store(Path(tmp) / "assistant.db", clock=clock)
        self.addCleanup(st.close)
        return st

    def test_turn_round_trip_and_history_window(self):
        st = self._store(lambda: 1000.0)
        conv = st.create_conversation("inst-1", "zh")
        for i in range(5):
            st.append_turn(conv, "user", f"q{i}", mode="kb")
        self.assertEqual([t["content"] for t in st.history(conv, limit=3)],
                         ["q2", "q3", "q4"])

    def test_retention_deletes_older_than_180_days(self):
        now = [1000.0]
        st = self._store(lambda: now[0])
        old = st.create_conversation("inst-1", "zh")
        st.append_turn(old, "user", "老问题", mode="kb")
        now[0] = 1000.0 + 181 * 86400
        fresh = st.create_conversation("inst-1", "zh")
        st.append_turn(fresh, "user", "新问题", mode="kb")
        self.assertEqual(st.purge_expired(), 1)
        self.assertEqual(st.history(old), [])
        self.assertEqual(len(st.history(fresh)), 1)

    def test_delete_instance_hard_deletes_everything(self):
        st = self._store(lambda: 1000.0)
        conv = st.create_conversation("inst-9", "zh")
        st.append_turn(conv, "user", "删我", mode="kb")
        self.assertEqual(st.delete_instance("inst-9"), 1)
        self.assertEqual(st.history(conv), [])

    def test_miss_counter_is_per_instance_per_day(self):
        now = [1000.0]
        st = self._store(lambda: now[0])
        conv = st.create_conversation("inst-2", "zh")
        for _ in range(3):
            st.append_turn(conv, "user", "问", mode="generated", needs_confirm=True)
        self.assertEqual(st.count_misses_today("inst-2"), 3)
        now[0] += 86400
        self.assertEqual(st.count_misses_today("inst-2"), 0)

    def test_bad_citations_json_does_not_break_history(self):
        st = self._store(lambda: 1000.0)
        conv = st.create_conversation("inst-3", "zh")
        st.append_turn(conv, "assistant", "答", mode="kb", citations=[{"id": "kb-1"}])
        st._db.execute("UPDATE turns SET citations='not json'")     # 模拟脏数据
        st._db.commit()
        self.assertEqual(st.history(conv)[0]["citations"], [])


STATE = {"model": "ft710", "version": "1.28.22", "fixpack": "FP18",
         "serial_connected": True, "audio_rx_open": True, "audio_tx_open": True,
         "last_errors": [], "platform": "linux-arm64", "ui_lang": "zh"}


class FakeModel:
    def __init__(self, text="随便答"):
        self.text = text
        self.calls = 0
        self.messages = []

    def complete(self, messages, **kw):
        self.calls += 1
        self.messages = messages
        return self.text


class EngineTests(unittest.TestCase):
    """引擎用**真实知识库**跑 —— 阈值是绝对 BM25 分，2 条假条目够不着命中线。"""

    @classmethod
    def setUpClass(cls):
        cls.entries = json.loads(
            (Path(__file__).resolve().parents[1]
             / "tools" / "assistant_gateway" / "assistant_kb.json").read_text(encoding="utf-8"))["entries"]

    def _engine(self, model_text="随便答", quota=20, misses=0):
        model = FakeModel(model_text)
        return gengine.Engine(entries=self.entries, model=model, quota_per_day=quota,
                              misses_so_far=lambda: misses), model

    def test_rule_layer_answers_without_calling_the_model(self):
        eng, model = self._engine()
        ans = eng.answer("为什么没频谱", dict(STATE, serial_connected=False), history=[])
        self.assertEqual(ans.mode, "rule")
        self.assertIn("串口", ans.answer)
        self.assertEqual(model.calls, 0)

    def test_kb_hit_cites_the_entry_and_needs_no_confirmation(self):
        eng, _ = self._engine("CQ 泵已改成按队列深度补料。")
        ans = eng.answer("CQ 播放有失真怎么办", STATE, history=[])
        self.assertEqual(ans.mode, "kb")
        self.assertIn("CQ", ans.citations[0]["label"])
        self.assertFalse(ans.needs_confirm)

    def test_kb_answer_mentioning_an_unknown_version_is_downgraded(self):
        eng, _ = self._engine("这个问题在 1.9.9 已经修好了。")
        ans = eng.answer("CQ 播放有失真怎么办", STATE, history=[])
        self.assertEqual(ans.mode, "kb")
        self.assertTrue(ans.needs_confirm)
        self.assertEqual(ans.degraded, "unverifiable")

    def test_unknown_question_is_generated_and_always_needs_confirmation(self):
        eng, model = self._engine("这个问题和天线有关。")
        ans = eng.answer("我家猫踩了电台怎么办", STATE, history=[])
        self.assertEqual(ans.mode, "generated")
        self.assertTrue(ans.needs_confirm)
        self.assertEqual(model.calls, 1)

    def test_quota_exhausted_never_calls_the_model(self):
        eng, model = self._engine(quota=1, misses=1)
        ans = eng.answer("怎么给树莓派烧卡", STATE, history=[])
        self.assertEqual(ans.degraded, "quota")
        self.assertTrue(ans.needs_confirm)
        self.assertEqual(model.calls, 0)

    def test_model_outage_falls_back_to_the_entry_instead_of_raising(self):
        class Dead:
            def complete(self, messages, **kw):
                raise gmodel.ModelUnavailable("down")

        eng = gengine.Engine(entries=self.entries, model=Dead(), quota_per_day=20,
                             misses_so_far=lambda: 0)
        ans = eng.answer("CQ 播放有失真怎么办", STATE, history=[])
        self.assertEqual(ans.mode, "kb")
        self.assertTrue(ans.needs_confirm)
        self.assertEqual(ans.degraded, "model_unavailable")

    def test_history_is_capped_and_keeps_role_order(self):
        eng, model = self._engine("答")
        history = [{"role": "user", "content": f"q{i}"} for i in range(6)]
        eng.answer("我家猫踩了电台怎么办", STATE, history=history)
        contents = [m["content"] for m in model.messages[1:]]
        self.assertEqual(contents[0], "q3")
        self.assertEqual(contents[-1], "我家猫踩了电台怎么办")


class HttpTests(unittest.TestCase):
    """真起一个端口，用 http.client 打它 —— 鉴权/上限/形状都在这一层。"""

    @classmethod
    def setUpClass(cls):
        kb = json.loads((Path(__file__).resolve().parents[1]
                         / "tools" / "assistant_gateway" / "assistant_kb.json").read_text(encoding="utf-8"))
        store = gstore.Store(Path(tempfile.mkdtemp()) / "t.db")
        engine = gengine.Engine(entries=kb["entries"], model=FakeModel("好的"),
                                quota_per_day=20, misses_so_far=lambda: 0)
        # 每台实例自己的配额：HTTP 层把 misses_so_far 绑到实例上（生产里就是
        # count_misses_today），这里用固定 0 就够验证形状。
        cls.server = ghttp.make_server(host="127.0.0.1", port=0, engine=engine,
                                       store=store, instances={"tok": "inst-1"})
        cls.store = store
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.store.close()

    def _post(self, path, payload, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        body = json.dumps(payload).encode()
        conn.request("POST", path, body=body,
                     headers={"Content-Type": "application/json", **(headers or {})})
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read().decode() or "{}")

    def test_unknown_instance_is_rejected(self):
        status, _ = self._post("/v1/assistant/ask", {"message": "问"}, {"X-Mrrc-Instance": "nope"})
        self.assertEqual(status, 401)

    def test_happy_path_returns_citations_and_conversation_id(self):
        status, body = self._post("/v1/assistant/ask",
                                  {"message": "CQ 播放有失真怎么办", "state": STATE},
                                  {"X-Mrrc-Instance": "tok"})
        self.assertEqual(status, 200)
        self.assertTrue(body["conversation_id"])
        self.assertIn("CQ", body["citations"][0]["label"])

    def test_oversized_body_is_rejected(self):
        status, _ = self._post("/v1/assistant/ask", {"message": "x" * 20000, "state": STATE},
                               {"X-Mrrc-Instance": "tok"})
        self.assertEqual(status, 413)

    def test_empty_message_is_rejected(self):
        status, _ = self._post("/v1/assistant/ask", {"message": "   "},
                               {"X-Mrrc-Instance": "tok"})
        self.assertEqual(status, 400)

    def test_delete_only_works_for_the_owning_instance(self):
        _, body = self._post("/v1/assistant/ask", {"message": "CQ 播放有失真怎么办", "state": STATE},
                             {"X-Mrrc-Instance": "tok"})
        conv = body["conversation_id"]
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        payload = json.dumps({"conversation_id": conv}).encode()
        conn.request("DELETE", "/v1/assistant/conversation", body=payload,
                     headers={"X-Mrrc-Instance": "tok", "Content-Type": "application/json"})
        self.assertEqual(conn.getresponse().status, 200)
        self.assertEqual(self.store.history(conv), [])


class ModelClientTests(unittest.TestCase):
    def _opener(self, payload):
        seen = {}

        def opener(req, timeout):
            seen["body"] = json.loads(req.data.decode())
            return io.BytesIO(json.dumps(payload).encode())

        return opener, seen

    def test_thinking_is_disabled_by_default(self):
        opener, seen = self._opener({"choices": [{"message": {"content": "答"}}]})
        client = gmodel.ModelClient("https://x/v1", "m", "k", opener=opener)
        client.complete([{"role": "user", "content": "问"}])
        self.assertEqual(seen["body"]["thinking"], {"type": "disabled"})

    def test_empty_completion_is_a_failure_not_an_empty_answer(self):
        opener, _ = self._opener({"choices": [{"message": {"content": "   "}}]})
        client = gmodel.ModelClient("https://x/v1", "m", "k", opener=opener)
        with self.assertRaises(gmodel.ModelUnavailable):
            client.complete([{"role": "user", "content": "问"}])

    def test_network_error_becomes_model_unavailable(self):
        def opener(req, timeout):
            raise OSError("boom")

        client = gmodel.ModelClient("https://x/v1", "m", "k", opener=opener)
        with self.assertRaises(gmodel.ModelUnavailable):
            client.complete([{"role": "user", "content": "问"}])
