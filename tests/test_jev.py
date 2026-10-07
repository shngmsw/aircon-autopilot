import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import config, controller, jev, logic, main, remo, store


def answer(choice="wait"):
    return {"model": "jev-1.13.0", "answers": {"operation": {
        "type": "choice", "choice": choice, "confidence": 0.9,
        "probabilities": {key: 1.0 if key == choice else 0.0 for key in jev.OPTIONS}}}}


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JEV_MODE", "shadow")
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", "test-key")
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    store._conn = None
    yield
    if store._conn:
        store._conn.close()
    store._conn = None


@pytest.mark.parametrize("choice", list(jev.OPTIONS))
def test_typed_choices(configured, choice):
    def handler(request):
        payload = json.loads(request.content)
        assert set(payload["questions"]["operation"]["criteria"]) == set(jev.OPTIONS)
        assert request.headers["Authorization"] == "Bearer test-key"
        return httpx.Response(200, json=answer(choice))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await jev.evaluate(client, {"comfort": "hot"})
    result = asyncio.run(run())
    assert result["choice"] == choice
    assert result["applied"] is False


@pytest.mark.parametrize("failure", ["timeout", "401", "429", "500", "bad_json", "bad_choice", "nan", "bad_sum"])
def test_failures_are_redacted(configured, failure):
    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("test-key", request=request)
        if failure.isdigit():
            return httpx.Response(int(failure), text="test-key")
        if failure == "bad_json":
            return httpx.Response(200, text="test-key")
        body = answer()
        a = body["answers"]["operation"]
        if failure == "bad_choice": a["choice"] = "invented"
        if failure == "nan": a["confidence"] = "NaN"
        if failure == "bad_sum": a["probabilities"]["cool"] = 1.0
        return httpx.Response(200, json=body)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await jev.evaluate(client, {})
    result = asyncio.run(run())
    assert result["status"] == "unavailable"
    assert "test-key" not in json.dumps(result)


@pytest.mark.parametrize("mode,key,reason", [
    ("off", "test-key", None), ("shadow", "", "missing_api_key"),
    ("invalid", "test-key", "invalid_mode")])
def test_no_request_when_disabled_or_unconfigured(monkeypatch, mode, key, reason):
    monkeypatch.setattr(config, "JEV_MODE", mode)
    monkeypatch.setattr(config, "TYPESAFE_API_KEY", key)
    result = asyncio.run(jev.evaluate(None, {}))
    assert result.get("reason") == reason


def test_passive_trend_excludes_cooling():
    now = store.now_jst()
    ac = remo.AirconState("a", "test", False, "cool", "25", "auto", [], [])
    d = logic.decide(10, 26, None, None, .7)
    def row(minutes, room, power):
        return {"ts": (now - timedelta(minutes=minutes)).isoformat(), "room": room,
                "power": power, "mode": "cool" if power == "on" else None}
    kwargs = dict(room=26, outdoor=10, humidity=50, low=24, high=25.5,
                  aircon=ac, decision=d, now=now)
    state = jev.build_state(**kwargs, history=[row(20, 28, "on"), row(10, 26.5, "off")])
    assert state["passive_change_c"] == -0.5
    assert state["passive_window_minutes"] == 10
    assert state["passive_trend"] == "falling"
    ac.power_on = True
    assert jev.build_state(**kwargs, history=[row(10, 27, "on")])["passive_trend"] == "unknown"


@pytest.mark.parametrize("recommendation", ["wait", "heat", "cool", "blow", "maintain", "uncertain", "failure"])
def test_shadow_keeps_baseline_control_and_records(configured, monkeypatch, recommendation):
    ac = remo.AirconState("a", "test", False, "cool", "25", "auto",
                          ["24", "25", "26"], ["auto"], modes=["cool"])
    async def snapshot(client): return remo.Snapshot(26, 80, ac)
    async def weather(client): return 10
    calls = []
    async def apply(*args, **kwargs): calls.append((args, kwargs))
    async def evaluate(client, state):
        assert state["comfort"] == "hot"
        return ({"status": "unavailable", "reason": "http_500"} if recommendation == "failure"
                else {"status": "ok", "choice": recommendation, "confidence": .9, "applied": False})
    monkeypatch.setattr(remo, "fetch_snapshot", snapshot)
    monkeypatch.setattr(controller.weather, "get_outdoor_temp", weather)
    monkeypatch.setattr(remo, "apply_settings", apply)
    monkeypatch.setattr(jev, "evaluate", evaluate)
    monkeypatch.setattr(config, "FREE_COOL_ENABLED", False)
    monkeypatch.setattr(config, "ROOM_TARGET_ENABLED", True)
    monkeypatch.setattr(config, "MIN_HOLD_MIN", 0)
    asyncio.run(controller.run_cycle(None))
    assert calls[0][0][2] == "on"
    assert calls[0][1]["mode"] == "cool"
    rows = TestClient(main.app).get('/api/jev/history').json()["rows"]
    assert len(rows) == 1
    assert rows[0]["state"]["baseline"]["mode"] == "cool"
    assert store.get_state("jev_last")["status"] == rows[0]["result"]["status"]
    status = TestClient(main.app).get('/api/status').json()
    assert status["jev"]["mode"] == "shadow"
    assert status["jev"]["last"]["status"] == rows[0]["result"]["status"]
    store.set_auto_state("off")
    asyncio.run(controller.run_cycle(None))
    assert len(store.get_jev_history()) == 1


def test_existing_database_records_survive_upgrade(configured):
    store.add_reading(outdoor=10, room=26, humidity=50, set_temp=None,
                      air_volume=None, power="off", auto=True, action="none", note="existing")
    store.set_state("room_target_low", 23)
    store._conn.execute("DROP TABLE jev_evaluations")
    store._conn.commit()
    store._conn.close()
    store._conn = None
    store.add_jev_evaluation({"comfort": "hot"}, {"status": "unavailable"})
    assert store.latest_reading()["note"] == "existing"
    assert store.get_state("room_target_low") == 23
    assert len(store.get_jev_history()) == 1
