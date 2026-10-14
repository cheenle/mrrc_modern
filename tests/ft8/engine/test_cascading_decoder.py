from __future__ import annotations

import asyncio
import time

import pytest

from ft8.core.models import DecodeBatch, DecodeConfig, DecodePath, DecodeResult
from ft8.core.models import CloudUnavailable
from ft8.engine.cascade import CascadePolicy, CascadingDecoder
from ft8.engine.orchestrator import slot_id_for, slot_start_epoch


def _r(text: str, snr: int = -5) -> DecodeResult:
    return DecodeResult(1, 0.0, snr, 0.1, 1500.0, text, 0, 0.0, 0)


def _b(path: DecodePath, *rs: DecodeResult, elapsed: float = 0.15) -> DecodeBatch:
    return DecodeBatch(1, path, rs, False, elapsed)


class PathAwareFake:
    def __init__(self, by_path: dict[DecodePath, DecodeBatch]) -> None:
        self.by_path = by_path
        self.seen: list[tuple[DecodePath, int, DecodeConfig | None]] = []

    async def decode_path(self, slot_id, samples, path, *, threads=None):
        self.seen.append((path, threads, None))
        return self.by_path[path]


@pytest.mark.parametrize(
    ("kwargs", "missing"),
    [
        ({"cutoff": 2.5}, "boundary_epoch_for"),
        ({"boundary_epoch_for": lambda s: 100.0}, "cutoff"),
    ],
)
def test_omitting_a_required_injection_is_an_error(kwargs, missing) -> None:
    """两个必填注入项**各自**都要证明「漏了会当场 TypeError」。

    它们故意都不给默认值，且理由是同一条：漏掉的后果不是异常，是预算判断
    静悄悄地错。`boundary_epoch_for` 若默认成 `slot * 15.0`，就把时隙号和
    epoch 时钟混着算；`cutoff` 若在本模块自带一个 2.5，就等于让决策截止这条
    物理量有了第二个来源（唯一来源是 `orchestrator.py:35`）。

    参数化而不是合成一个用例：一个「两个都漏」的用例在只补回其中一个默认值时
    仍然会红，于是它证明不了**是哪一个**被钉住了 —— 那正是 R67 的形状。
    """
    with pytest.raises(TypeError, match=missing):
        CascadingDecoder(PathAwareFake({}), CascadePolicy(), **kwargs)


def test_strong_slot_returns_fast_without_escalating() -> None:
    fast = _b(DecodePath.FAST, _r("CQ AAA", snr=-5))
    inner = PathAwareFake({DecodePath.FAST: fast})
    decoder = CascadingDecoder(inner, CascadePolicy(escalate_on_weak=False),
                               cutoff=2.5,
                               clock=lambda: 100.0, boundary_epoch_for=lambda s: 100.0)
    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))
    assert out.path is DecodePath.FAST
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST]


def test_escalates_and_merges_during_a_qso() -> None:
    """升级由 AP 上下文触发 —— 快路径的 snr 不可信，通联中才是真实触发场景。"""
    fast = _b(DecodePath.FAST, _r("CQ AAA"), elapsed=0.16)
    deep = _b(DecodePath.IMPROVED, _r("CQ AAA", snr=-12), _r("CQ BBB"),
              elapsed=2.2)
    inner = PathAwareFake({DecodePath.FAST: fast, DecodePath.IMPROVED: deep})
    decoder = CascadingDecoder(inner, CascadePolicy(),
                               cutoff=2.5,
                               boundary_epoch_for=lambda s: 100.0,
                               clock=lambda: 100.0,
                               ap_context=lambda: ("BA4EG", 2))
    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))
    assert out.path is DecodePath.IMPROVED
    assert {r.text for r in out.results} == {"CQ AAA", "CQ BBB"}
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST, DecodePath.IMPROVED]
    assert inner.seen[1][1] >= 3, "deep pass must run multi-threaded (spec D-4)"


def test_budget_guard_skips_escalation_when_time_is_short() -> None:
    fast = _b(DecodePath.FAST, _r("CQ AAA"), elapsed=0.16)
    inner = PathAwareFake({DecodePath.FAST: fast})
    # 时隙边界 100.0、cutoff 2.5、时钟已走到 100.4 → 剩 2.5−0.4−0.05(安全边际)=2.05s，
    # < deep 下界 2.3s。（快路径耗时已含在 clock()−boundary 里，实现里不再另减。）
    decoder = CascadingDecoder(inner, CascadePolicy(),
                               cutoff=2.5,
                               boundary_epoch_for=lambda s: 100.0,
                               clock=lambda: 100.4,
                               deep_lower_bound_s=2.3,
                               ap_context=lambda: ("BA4EG", 2))
    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))
    assert out.path is DecodePath.FAST
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST]
    assert decoder.deadline_risk == 1, "被预算挡下的升级要计数（Task 13 依赖它）"


def test_default_clock_is_the_epoch_base() -> None:
    """**不传 `clock=`** —— 钉死默认值就是 `time.time`（epoch 秒）。

    这是全仓库唯一能看见那个默认值的地方：其它用例都显式注入 `clock=`，
    所以把默认改成 `time.monotonic` 时它们全绿。后果却不在测试里，在生产：
    `clock() − boundary_epoch` 变成约 −1.7e9，`_remaining_budget` 返回一个
    巨大的正数，**预算守卫永不触发**（守卫看着还在，实际从不生效 —— 最贵的一种错）。

    判据用**量级**而非函数对象身份：两种时钟都是单调递增的，只有量级能把
    「基准不一致」和「正常运转」分开。取真实时隙网格上的一个 epoch 尺度边界
    （当前槽的起点），同一基准下预算在秒级（这里落在 (−12.55, 2.45]），
    基准错位时则是约 1.7e9。边界与时钟同源自 time.time，所以无论测试落在
    当前 15s 时隙的哪一段，断言都稳；不需要 sleep，也没有竞态。
    """
    decoder = CascadingDecoder(
        PathAwareFake({}), CascadePolicy(),
        cutoff=2.5,
        boundary_epoch_for=slot_start_epoch,
    )
    slot_id = slot_id_for(time.time())
    budget = decoder._remaining_budget(slot_id)
    assert budget < 60.0, (
        f"remaining budget {budget!r} is not on the seconds scale — "
        f"the default clock is off its epoch base (clock={decoder._clock!r})"
    )
    assert decoder._clock is time.time


def test_fast_failure_falls_back_to_deep() -> None:
    deep = _b(DecodePath.IMPROVED, _r("CQ AAA"))

    class Failing(PathAwareFake):
        async def decode_path(self, slot_id, samples, path, *, threads=None):
            self.seen.append((path, threads, None))
            if path is DecodePath.FAST:
                raise RuntimeError("fast path unavailable")
            return self.by_path[path]

    failing = Failing({DecodePath.IMPROVED: deep})
    decoder = CascadingDecoder(failing, CascadePolicy(), cutoff=2.5,
                               clock=lambda: 100.0, boundary_epoch_for=lambda s: 100.0)
    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))
    assert out.path is DecodePath.IMPROVED
    assert [r.text for r in out.results] == ["CQ AAA"]


def test_requested_flag_reaches_the_policy() -> None:
    """「深度解码本槽」那个一次性开关必须真的接到策略上。

    如果它靠 getattr 兜底去内层对象上摸一个并不存在的属性，拿不到时不会报错，
    只会永远不升级 —— 功能静默死亡，是最坏的失败方式。
    """
    fast = _b(DecodePath.FAST, _r("CQ AAA"))
    deep = _b(DecodePath.IMPROVED, _r("CQ AAA", snr=-12), _r("CQ BBB"))
    inner = PathAwareFake({DecodePath.FAST: fast, DecodePath.IMPROVED: deep})
    decoder = CascadingDecoder(
        inner, CascadePolicy(escalate_on_request=True),
        cutoff=2.5,
        boundary_epoch_for=lambda s: 100.0,
        clock=lambda: 100.0,
        requested=lambda: True,
    )
    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))
    assert out.path is DecodePath.IMPROVED
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST, DecodePath.IMPROVED]


def test_set_policy_takes_effect_on_the_next_slot() -> None:
    """运行期改策略必须立刻生效（Task 14 的设置保存路径靠它）。

    若 set_policy 只是把策略存进一个构造期副本、或干脆不存在，保存设置后
    要重启引擎才生效 —— 用户按了保存、行为不变，无从判断是不是按到了。
    """
    fast = _b(DecodePath.FAST, _r("CQ AAA"))
    deep = _b(DecodePath.IMPROVED, _r("CQ AAA", snr=-12), _r("CQ BBB"))
    inner = PathAwareFake({DecodePath.FAST: fast, DecodePath.IMPROVED: deep})
    decoder = CascadingDecoder(
        inner, CascadePolicy(escalate_on_weak=False),
        cutoff=2.5,
        boundary_epoch_for=lambda s: 100.0,
        clock=lambda: 100.0,
        requested=lambda: True,
    )
    # 旧策略 escalate_on_request=False → 就算 requested() 说 True 也不升级。
    assert asyncio.run(decoder.decode(1, b"\x00" * 360_000)).path is DecodePath.FAST
    inner.seen.clear()

    decoder.set_policy(CascadePolicy(escalate_on_request=True))

    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))
    assert out.path is DecodePath.IMPROVED
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST, DecodePath.IMPROVED]


class RemoteFake:
    """A remote provider: a fixed batch or a raised error, plus a call count."""

    def __init__(self, batch: DecodeBatch | None = None, error: Exception | None = None) -> None:
        self.calls = 0
        self.batch = batch
        self.error = error

    async def decode_path(self, slot_id, samples, path, *, threads=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.batch


def _qso_cascade(inner, remote, **kwargs):
    """This file's own construction shape, with the AP context that forces an escalation."""

    return CascadingDecoder(
        inner, CascadePolicy(),
        cutoff=2.5,
        boundary_epoch_for=lambda s: 100.0,
        clock=kwargs.pop("clock", lambda: 100.0),
        ap_context=lambda: ("BA4EG", 2),
        remote=remote,
        **kwargs,
    )


def test_the_remote_provider_answers_the_escalation() -> None:
    # 两个**不同频率**的信号：同频同 dt 的会被合并键正确地当作同一条（见
    # test_a_cloud_result_a_few_hertz_off_is_not_a_duplicate）。
    fast = _b(DecodePath.FAST, DecodeResult(1, 0.0, -20, 0.1, 900.0, "CQ AAA", 0, 0.0, 0))
    deep = _b(DecodePath.IMPROVED, _r("CQ BBB"))
    inner = PathAwareFake({DecodePath.FAST: fast, DecodePath.IMPROVED: deep})
    remote = RemoteFake(batch=_b(DecodePath.IMPROVED, _r("CQ JA1ABC")))
    decoder = _qso_cascade(inner, remote)

    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))

    assert remote.calls == 1
    assert decoder.cloud_ok == 1 and decoder.cloud_fallback == 0
    assert {r.text for r in out.results} == {"CQ AAA", "CQ JA1ABC"}
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST], (
        "the local deep path must not run when the cloud answered"
    )
    assert decoder.last_deep_path == "cloud"


def test_a_failed_remote_falls_back_to_the_local_deep_path() -> None:
    fast = _b(DecodePath.FAST, DecodeResult(1, 0.0, -20, 0.1, 900.0, "CQ AAA", 0, 0.0, 0))
    deep = _b(DecodePath.IMPROVED, _r("CQ BBB"))
    inner = PathAwareFake({DecodePath.FAST: fast, DecodePath.IMPROVED: deep})
    remote = RemoteFake(error=CloudUnavailable("hub down"))
    decoder = _qso_cascade(inner, remote)

    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))

    assert remote.calls == 1
    assert decoder.cloud_fallback == 1 and decoder.cloud_ok == 0
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST, DecodePath.IMPROVED]
    assert {r.text for r in out.results} == {"CQ AAA", "CQ BBB"}
    assert out.escalated is True
    assert decoder.last_deep_path == "local"


def test_a_remote_failure_lands_on_the_same_budget_guard() -> None:
    """A remote failure means today's 'budget too tight': no local run, one count."""

    fast = _b(DecodePath.FAST, _r("CQ AAA"))
    deep = _b(DecodePath.IMPROVED, _r("CQ BBB"))
    inner = PathAwareFake({DecodePath.FAST: fast, DecodePath.IMPROVED: deep})
    remote = RemoteFake(error=CloudUnavailable("hub down"))
    decoder = _qso_cascade(inner, remote, clock=lambda: 102.5)

    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))

    assert decoder.cloud_fallback == 1
    assert decoder.deadline_risk == 1, (
        "exactly once — not once for the cloud and again for the box"
    )
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST]
    assert {r.text for r in out.results} == {"CQ AAA"}


class SleepingRemote:
    async def decode_path(self, slot_id, samples, path, *, threads=None):
        await asyncio.sleep(5)
        raise AssertionError("unreachable")


def test_a_remote_that_never_answers_is_abandoned_at_the_deadline() -> None:
    fast = _b(DecodePath.FAST, _r("CQ AAA"))
    deep = _b(DecodePath.IMPROVED, _r("CQ BBB"))
    inner = PathAwareFake({DecodePath.FAST: fast, DecodePath.IMPROVED: deep})
    decoder = _qso_cascade(inner, SleepingRemote(), cloud_deadline_s=0.05)

    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))

    assert decoder.cloud_fallback == 1
    assert [p for p, _t, _c in inner.seen] == [DecodePath.FAST, DecodePath.IMPROVED]


def test_a_cloud_result_a_few_hertz_off_is_not_a_duplicate() -> None:
    """The same signal lands at slightly different freq/dt on the two paths."""

    fast = _b(DecodePath.FAST, DecodeResult(1, 0.0, -20, 0.10, 1500.0, "CQ AAA", 0, 0.0, 0))
    remote = RemoteFake(batch=_b(DecodePath.IMPROVED,
                                 DecodeResult(1, 0.0, -21, 0.12, 1500.4, "CQ AAA", 0, 0.0, 0)))
    inner = PathAwareFake({DecodePath.FAST: fast})
    decoder = _qso_cascade(inner, remote)

    out = asyncio.run(decoder.decode(1, b"\x00" * 360_000))

    assert [r.text for r in out.results] == ["CQ AAA"], "deep wins, no duplicate row"
    assert out.results[0].snr == -21
