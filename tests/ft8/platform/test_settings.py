from __future__ import annotations

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
