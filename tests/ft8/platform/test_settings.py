from __future__ import annotations

import dataclasses

import pytest

from ft8.platform.settings import Ft8Settings, Ft8SettingsStore


def test_defaults_are_usable_without_a_file(tmp_path):
    store = Ft8SettingsStore(tmp_path / "settings.json")
    s = store.load()
    assert s == Ft8Settings(my_call="", my_grid="", rig_mode="DATA-U",
                            tx_audio_frequency=1500, settings_revision=0)


def test_round_trip_and_validation(tmp_path):
    path = tmp_path / "settings.json"
    store = Ft8SettingsStore(path)
    store.save(Ft8Settings(my_call="BA4EG", my_grid="PM01", rig_mode="USB",
                           tx_audio_frequency=1200))
    again = store.load()
    assert again.my_call == "BA4EG" and again.rig_mode == "USB"
    assert again.tx_audio_frequency == 1200


def test_bad_rig_mode_is_rejected_on_save(tmp_path):
    store = Ft8SettingsStore(tmp_path / "settings.json")
    with pytest.raises(ValueError):
        store.save(Ft8Settings(rig_mode="FM"))


def test_out_of_range_audio_frequency_is_rejected(tmp_path):
    store = Ft8SettingsStore(tmp_path / "settings.json")
    with pytest.raises(ValueError):
        store.save(Ft8Settings(tx_audio_frequency=4000))


def test_corrupt_file_degrades_to_defaults(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text("{not json")
    assert Ft8SettingsStore(path).load() == Ft8Settings()


def test_policy_settings_round_trip() -> None:
    from ft8.platform.settings import Ft8Settings

    base = Ft8Settings()          # 全部字段都有默认值
    # Ft8Settings 是 frozen dataclass，**没有** .replace() —— 用模块级 dataclasses.replace。
    updated = dataclasses.replace(base, escalate_on_weak=False, min_peak_snr=-18)
    assert updated.escalate_on_weak is False
    assert updated.min_peak_snr == -18


def test_new_policy_defaults_match_cascade_policy() -> None:
    """「从未存过设置」与「存了一份默认值」不能走出两条不同的升级策略。"""

    from ft8.engine.cascade import CascadePolicy
    from ft8.platform.settings import Ft8Settings

    settings = Ft8Settings()
    policy = CascadePolicy()
    assert settings.escalate_on_ap_context is policy.escalate_on_ap_context
    assert settings.escalate_on_weak is policy.escalate_on_weak
    assert settings.min_decodes == policy.min_decodes
    assert settings.min_peak_snr == policy.min_peak_snr
    assert settings.escalate_on_request is policy.escalate_on_request


@pytest.mark.parametrize("bad", [{"min_peak_snr": 1}, {"min_peak_snr": -200},
                                 {"min_decodes": -1}])
def test_out_of_range_policy_settings_are_rejected(bad: dict) -> None:
    """越界值必须在**写**的时候就被拒 —— min_peak_snr 写成正数会让每槽都升级。"""

    from ft8.platform.settings import Ft8Settings

    settings = dataclasses.replace(Ft8Settings(), **bad)
    with pytest.raises(ValueError):
        settings.validate()
