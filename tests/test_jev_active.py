import asyncio

import pytest

from app import config, controller, jev, logic, remo, store


def recommendation(choice, confidence=.9, probability=.9):
    return {"status": "ok", "choice": choice, "confidence": confidence,
            "probabilities": {key: probability if key == choice else (1-probability)/5 for key in jev.OPTIONS},
            "applied": False}


def ac(on=True, mode="cool"):
    options = {key: {"temp": [str(t) for t in range(16, 31)], "vol": ["1", "auto"]}
               for key in ("cool", "warm")}
    options["blow"] = {"temp": [], "vol": ["1", "auto"]}
    return remo.AirconState("test", "test", on, mode, "23", "auto",
                           options["cool"]["temp"], ["1", "auto"], list(options), options)


@pytest.fixture
def active(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JEV_MODE", "active")
    monkeypatch.setattr(config, "ROOM_TARGET_ENABLED", True)
    monkeypatch.setattr(config, "ROOM_TARGET_LOW", 21.)
    monkeypatch.setattr(config, "ROOM_TARGET_HIGH", 24.)
    monkeypatch.setattr(config, "FREE_COOL_ENABLED", False)
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "test.db"))
    store._conn = None
    yield
    if store._conn:
        store._conn.close()
    store._conn = None


@pytest.mark.parametrize("choice,room,outdoor,power,mode", [
    ("wait", 22, 19, "off", None), ("maintain", 22, 19, "off", None),
    ("cool", 26, 28, "on", "cool"), ("heat", 19, 5, "on", "warm"),
    ("blow", 25, 19, "on", "blow")])
def test_active_maps_operations(active, choice, room, outdoor, power, mode):
    baseline = logic.decide(outdoor, room, None, None, .7)
    d, selection = jev.select_decision(baseline, recommendation(choice),
                                      aircon=ac(False), room=room, outdoor=outdoor, low=21, high=24)
    assert selection == "accepted"
    assert (d.power, d.mode) == (power, mode)
    if mode == "warm": assert d.target_temp <= 24
    if mode == "cool": assert 16 <= d.target_temp <= 30
    if mode == "blow": assert d.target_temp is None


@pytest.mark.parametrize("choice,kwargs,reason", [
    ("heat", {"room": 22}, "heating_room_limit"),
    ("heat", {"room": 19, "outdoor": 25}, "heating_outdoor_limit"),
    ("heat", {"room": 19, "recent_mode": "cool"}, "switch_wait"),
    ("cool", {"recent_mode": "warm"}, "switch_wait"),
    ("cool", {"room": 20}, "cooling_room_limit"),
    ("wait", {"room": 31}, "temperature_limit"),
    ("wait", {"room": 19}, "temperature_limit"),
    ("blow", {"lockout": True}, "fan_limit"),
    ("wait", {"holding": True}, "holding"),
    ("wait", {"aircon": remo.AirconState('a','a',False,'cool','23','auto')}, "accepted"),
    ("heat", {"room":19,"aircon":remo.AirconState('a','a',False,'cool','23','auto',modes=['cool'])}, "unsupported_mode"),
])
def test_existing_limits_are_enforced(active, choice, kwargs, reason):
    args = dict(aircon=ac(), room=25, outdoor=10, low=21, high=24)
    args.update(kwargs)
    baseline = logic.decide(args['outdoor'],args['room'],None,None,.7)
    d, actual = jev.select_decision(baseline,recommendation(choice),**args)
    assert actual == reason
    if reason != "accepted": assert d is baseline


@pytest.mark.parametrize("result", [recommendation("wait",confidence=.15),
                                  recommendation("wait",probability=.44),
                                  recommendation("uncertain"), {"status":"unavailable"}])
def test_uncertainty_falls_back(active, result):
    baseline = logic.decide(10,22,None,None,.7)
    d, selection = jev.select_decision(baseline,result,aircon=ac(),room=22,outdoor=10,low=21,high=24)
    assert d is baseline
    assert selection != "accepted"


def test_maintain_preserves_temperature_and_fan(active):
    appliance=ac()
    baseline=logic.decide(10,22,None,None,.7)
    d, selection=jev.select_decision(baseline,recommendation('maintain'),
                                   aircon=appliance,room=22,outdoor=10,low=21,high=24)
    assert selection=='accepted'
    assert (d.power,d.mode,d.target_temp,d.volume_pref)==('on','cool',23,None)


def test_maintain_cannot_keep_excessive_heating_setpoint(active):
    appliance=ac(mode='warm')
    appliance.target_temp='29'
    baseline=logic.decide(5,20,None,None,.7)
    d, selection=jev.select_decision(baseline,recommendation('maintain'),
                                   aircon=appliance,room=20,outdoor=5,low=21,high=24)
    assert d is baseline
    assert selection=='current_temperature_limit'


@pytest.mark.parametrize("choice,room,on,expected_power,expected_mode", [
    ("wait",22,True,"off","cool"), ("cool",26,False,"on","cool"),
    ("heat",19,False,"on","warm"), ("blow",25,True,"on","blow")])
def test_controller_sends_and_records_active_operation(active, monkeypatch, choice, room, on, expected_power, expected_mode):
    appliance = ac(on)
    calls=[]
    async def snapshot(client): return remo.Snapshot(room,55,appliance)
    async def weather(client): return 10
    async def evaluate(client,state): return recommendation(choice)
    async def apply(client,aircon,power,temp,vol,mode): calls.append((power,mode,temp))
    monkeypatch.setattr(remo,"fetch_snapshot",snapshot)
    monkeypatch.setattr(controller.weather,"get_outdoor_temp",weather)
    monkeypatch.setattr(jev,"evaluate",evaluate)
    monkeypatch.setattr(remo,"apply_settings",apply)
    asyncio.run(controller.run_cycle(None))
    assert calls[0][:2] == (expected_power,expected_mode)
    result=store.get_state('jev_last')
    assert result['accepted'] and result['applied']
    assert result['outcome']=='set'
    assert store.get_jev_history()[0]['result']['applied']


@pytest.mark.parametrize("interruption", ["manual", "hold", "send_error", "manual_during_api"])
def test_no_false_applied_records(active, monkeypatch, interruption):
    appliance=ac()
    async def snapshot(client): return remo.Snapshot(22,55,appliance)
    async def weather(client): return 19
    async def evaluate(client,state):
        if interruption=='manual_during_api': store.set_auto_state('off')
        return recommendation('wait')
    calls=[]
    async def apply(*args,**kwargs):
        calls.append(args)
        if interruption=='send_error': raise remo.RemoError('simulated failure')
    monkeypatch.setattr(remo,'fetch_snapshot',snapshot)
    monkeypatch.setattr(controller.weather,'get_outdoor_temp',weather)
    monkeypatch.setattr(jev,'evaluate',evaluate)
    monkeypatch.setattr(remo,'apply_settings',apply)
    if interruption=='manual': store.set_auto_state('off')
    if interruption=='hold': store.set_state('last_command_ts',store.now_jst().isoformat())
    asyncio.run(controller.run_cycle(None))
    result=store.get_state('jev_last')
    assert not result or not result['applied']
    if interruption!='send_error': assert calls==[]
