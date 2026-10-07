import pytest

from app import logic


def decide(room=22.1, outdoor=19.3, **overrides):
    args = dict(room_target_enabled=True, room_target_low=21, room_target_high=24,
                current_set_temp=23, free_cool_enabled=False, passive_wait_enabled=True)
    args.update(overrides)
    return logic.decide(outdoor, room, None, None, .7, **args)


def test_observed_nighttime_case_stops_cooling():
    d = decide()
    assert (d.power, d.mode, d.target_temp, d.volume_pref) == ("off", None, None, None)
    assert d.passive_wait


def test_stop_precedes_free_cooling_fan_override():
    d = decide(free_cool_enabled=True)
    assert d.power == "off"
    assert not d.free_cool


def test_restart_requires_room_to_exceed_upper_margin():
    assert not decide(room=24.1).passive_wait
    assert decide(room=24.0, last_passive_wait=True).passive_wait
    d = decide(room=24.01, last_passive_wait=True)
    assert not d.passive_wait
    assert d.mode == "cool"
    assert not decide(room=23.4).passive_wait
    assert decide(room=23.3).passive_wait


def test_outdoor_and_gap_have_hysteresis():
    assert not decide(room=22.1, outdoor=20.2).passive_wait
    assert decide(room=22.1, outdoor=20.2, last_passive_wait=True).passive_wait
    assert not decide(room=22.1, outdoor=20.8, last_passive_wait=True).passive_wait
    assert not decide(room=21.1, outdoor=20).passive_wait


def test_heating_and_hot_room_are_not_suppressed():
    assert decide(room=19, outdoor=5).mode == "warm"
    assert not decide(room=22, outdoor=5, heating_now=True).passive_wait
    assert not decide(room=30.5, room_target_high=30.5, last_passive_wait=True).passive_wait


def test_disable_preserves_previous_behavior():
    d = decide(passive_wait_enabled=False)
    assert d.mode == "cool"
    assert not d.passive_wait


def test_warm_outdoors_does_not_trigger_passive_wait():
    assert not decide(room=22.3, outdoor=24.3).passive_wait
