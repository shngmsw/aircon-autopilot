import asyncio

import pytest

from scripts.simulate_control import SCENARIOS, replay, simulate


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.name)
def test_closed_loop_scenarios(scenario):
    baseline = asyncio.run(simulate(scenario, False))
    changed = asyncio.run(simulate(scenario, True))
    assert len(changed["samples"]) == 48
    assert changed["metrics"]["safety_violations"] == 0
    if scenario.name == "passive_cooling":
        assert changed["metrics"]["starts"] == 0
        assert changed["metrics"]["outside_band_minutes"] == 0
        assert changed["samples"][0]["power"] == "off"
    if scenario.name in ("insulated_room", "solar_gain"):
        assert changed["metrics"]["max_deviation_c"] < .15
        assert changed["metrics"]["starts"] > 0
    if scenario.name in ("winter_heating", "warm_outdoors"):
        assert baseline["metrics"] == changed["metrics"]
    if scenario.name == "heating_afterglow":
        assert not any(c["power"] == "on" and c["mode"] == "cool" and c["cycle"] < 6
                       for c in changed["commands"])
    if scenario.manual_at is not None:
        assert all(s["power"] == "off" for s in changed["samples"][scenario.manual_at:])
    if scenario.missing_at is not None:
        assert not any(c["cycle"] == scenario.missing_at for c in changed["commands"])


def test_replay_does_not_use_future_target_changes():
    def row(ts, note):
        return {"ts": ts, "note": note, "power": "on", "mode": "cool", "set_temp": 23,
                "room": 22.1, "outdoor": 19.3}
    rows = [row("2026-10-07T00:00:00+09:00", "目標帯(21.0〜24.0℃)"),
            row("2026-10-07T00:10:00+09:00", "目標帯(21.0〜24.0℃)"),
            row("2026-10-07T00:20:00+09:00", "目標帯(23.0〜25.0℃)")]
    assert replay(rows[:2]) == replay(rows)[:1]
    assert replay(rows)[0]["changed"]["power"] == "off"
