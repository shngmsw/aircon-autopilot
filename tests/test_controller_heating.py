"""制御サイクル（app/controller.py）の暖房と目標帯オーバーライドの結合テスト。

Remo と天気 API をモックし、SQLite は一時ファイルを使う。
"""
import asyncio

import pytest

from app import config, controller, remo, store


class FakeClient:
    """httpx.AsyncClient の代わり。remo.apply_settings もモックするので何も持たない。"""


WARM_TEMPS = [str(t) for t in range(18, 31)]


def _aircon(power_on: bool, mode: str, temp: str = "20",
            modes=("cool", "warm", "blow")) -> remo.AirconState:
    mode_options = {
        "cool": {"temp": ["25", "26", "27", "28"], "vol": ["1", "2", "auto"]},
        "warm": {"temp": WARM_TEMPS, "vol": ["1", "2", "auto"]},
        "blow": {"temp": [], "vol": ["1", "2"]},
    }
    mode_options = {m: v for m, v in mode_options.items() if m in modes}
    return remo.AirconState(
        appliance_id="a1",
        nickname="test",
        power_on=power_on,
        mode=mode,
        target_temp=temp,
        air_volume="1",
        temp_options=mode_options.get("cool", {}).get("temp", []),
        vol_options=mode_options.get("cool", {}).get("vol", []),
        modes=list(mode_options.keys()),
        mode_options=mode_options,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    # 一時DBに差し替え（モジュール内の接続キャッシュもリセット）
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    store._conn = None
    monkeypatch.setattr(config, "MIN_HOLD_MIN", 0)
    monkeypatch.setattr(config, "ROOM_TARGET_ENABLED", True)
    monkeypatch.setattr(config, "ROOM_TARGET_LOW", 20.0)
    monkeypatch.setattr(config, "ROOM_TARGET_HIGH", 22.0)
    monkeypatch.setattr(config, "ROOM_TARGET_GAIN", 1.0)
    monkeypatch.setattr(config, "FREE_COOL_ENABLED", False)
    monkeypatch.setattr(config, "HEAT_OUT_MAX", 20.0)

    calls: list[dict] = []

    async def fake_apply(client, aircon, power, temp, vol, mode="cool"):
        calls.append({"power": power, "temp": temp, "vol": vol, "mode": mode})

    monkeypatch.setattr(remo, "apply_settings", fake_apply)

    state = {"aircon": _aircon(False, ""), "room": 18.0, "humidity": 40, "outdoor": 8.0}

    async def fake_snapshot(client):
        return remo.Snapshot(
            room_temp=state["room"], humidity=state["humidity"], aircon=state["aircon"]
        )

    async def fake_outdoor(client):
        return state["outdoor"]

    monkeypatch.setattr(remo, "fetch_snapshot", fake_snapshot)
    monkeypatch.setattr(controller.weather, "get_outdoor_temp", fake_outdoor)
    return state, calls


def run():
    return asyncio.run(controller.run_cycle(FakeClient()))


def test_目標帯はオーバーライドが無ければenvの値(env):
    low, high, source = controller.effective_target_band()
    assert (low, high, source) == (20.0, 22.0, "env")


def test_目標帯が壊れていればenvにフォールバックする(env):
    store.set_state("room_target_low", "abc")
    store.set_state("room_target_high", 25.5)
    assert controller.effective_target_band() == (20.0, 22.0, "env")


def test_目標帯の上下が逆ならenvにフォールバックする(env):
    store.set_state("room_target_low", 26.0)
    store.set_state("room_target_high", 22.0)
    assert controller.effective_target_band() == (20.0, 22.0, "env")


def test_目標帯はstateのオーバーライドが優先される(env):
    state, calls = env
    # env相当は20〜22℃。オーバーライドで24〜25.5℃に上げると室温23℃でも暖房する
    store.set_state("room_target_low", 24.0)
    store.set_state("room_target_high", 25.5)
    assert controller.effective_target_band() == (24.0, 25.5, "override")

    state["room"] = 23.0
    run()
    assert calls[-1]["mode"] == "warm"
    assert calls[-1]["temp"] == "25"   # 1.0℃不足 × gain1.0 → 24+1=25℃


def test_暖房は目標帯の上限までしか上げない(env):
    state, calls = env
    store.set_state("room_target_low", 24.0)
    store.set_state("room_target_high", 25.5)

    # 2.0℃不足 × gain1.0 → 26℃ にはせず、目標上限25.5℃ → 切り捨てて25℃
    state["room"] = 22.0
    run()
    assert calls[-1]["mode"] == "warm"
    assert calls[-1]["temp"] == "25"


def test_上限を超えた設定で暖房中なら設定を下げる(env):
    state, calls = env
    store.set_state("room_target_low", 24.0)
    store.set_state("room_target_high", 25.5)

    state["aircon"] = _aircon(True, "warm", temp="27")
    state["room"] = 22.0
    run()
    assert calls[-1]["mode"] == "warm"
    assert calls[-1]["temp"] == "25"


def test_寒ければ暖房し_設定温度は不足に応じて上がる(env):
    state, calls = env
    # 室温18℃・目標20〜22℃ → 2℃不足 × gain1.0 → 設定 20+2=22℃
    run()
    assert calls[-1]["power"] == "on"
    assert calls[-1]["mode"] == "warm"
    assert calls[-1]["temp"] == "22"


def test_帯に入ったら暖房を停止する(env):
    state, calls = env
    state["aircon"] = _aircon(True, "warm", temp="22")
    state["room"] = 21.0   # オフしきい値 min(20+0.7, 22) = 20.7℃ を超えている
    run()
    assert calls[-1]["power"] == "off"


def test_帯の下端付近では暖房を継続する(env):
    state, calls = env
    ac = _aircon(True, "warm", temp="22")
    ac.air_volume = "auto"
    state["aircon"] = ac
    state["room"] = 20.3   # 20.7℃ 未満なので継続（設定も据え置き → APIコールなし）
    r = run()
    assert calls == []
    assert "現状維持" in r["note"]


def test_外気が暖かければ暖房しない(env):
    state, calls = env
    state["outdoor"] = 22.0   # HEAT_OUT_MAX(20.0) 以上
    state["room"] = 18.0
    run()
    assert calls == []   # 停止中→停止のままなので操作なし


def test_暖房非対応の機種では電源オフにする(env):
    state, calls = env
    state["aircon"] = _aircon(True, "cool", temp="25", modes=("cool", "blow"))
    state["room"] = 18.0
    r = run()
    assert calls[-1]["power"] == "off"
    assert "暖房非対応" in r["note"]


def test_暖房の温度指定が無い機種では温度を送らず現状維持できる(env):
    state, calls = env
    ac = _aircon(True, "warm", temp="")
    ac.mode_options["warm"] = {"temp": [], "vol": ["1", "2", "auto"]}
    ac.air_volume = "auto"
    state["aircon"] = ac
    state["room"] = 18.0
    run()
    n = len(calls)
    r = run()   # 2サイクル目: 実機状態は変わらないが、温度を比較対象にしないので現状維持
    assert len(calls) == n
    assert "現状維持" in r["note"]
