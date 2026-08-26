"""制御サイクル（app/controller.py）の外気冷却まわりの結合テスト。

Remo と天気 API をモックし、SQLite は一時ファイルを使う。
「送風開始時の室温を覚え、横ばいなら復帰せず、上がれば復帰してロックアウトする」
流れを1本通す。
"""
import asyncio

import pytest

from app import config, controller, remo, store


class FakeClient:
    """httpx.AsyncClient の代わり。remo.apply_settings もモックするので何も持たない。"""


def _aircon(power_on: bool, mode: str, temp: str = "27") -> remo.AirconState:
    return remo.AirconState(
        appliance_id="a1",
        nickname="test",
        power_on=power_on,
        mode=mode,
        target_temp=temp,
        air_volume="1",
        temp_options=["25", "26", "27", "28"],
        vol_options=["1", "2", "auto"],
        modes=["cool", "blow"],
        mode_options={
            "cool": {"temp": ["25", "26", "27", "28"], "vol": ["1", "2", "auto"]},
            "blow": {"temp": [], "vol": ["1", "2"]},
        },
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    # 一時DBに差し替え（モジュール内の接続キャッシュもリセット）
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    store._conn = None
    monkeypatch.setattr(config, "MIN_HOLD_MIN", 0)
    # ここで見たいのは送風の復帰フローなので、目標温度は従来のマトリクスで固定する
    monkeypatch.setattr(config, "ROOM_TARGET_ENABLED", False)
    monkeypatch.setattr(config, "FREE_COOL_ABORT_ROOM", 30.5)
    monkeypatch.setattr(config, "FREE_COOL_RISE_MIN", 0.5)
    monkeypatch.setattr(config, "FREE_COOL_LOCKOUT_MIN", 45)

    calls: list[dict] = []

    async def fake_apply(client, aircon, power, temp, vol, mode="cool"):
        calls.append({"power": power, "temp": temp, "vol": vol, "mode": mode})

    monkeypatch.setattr(remo, "apply_settings", fake_apply)

    state = {"aircon": _aircon(True, "cool"), "room": 30.2, "humidity": 55, "outdoor": 22.5}

    async def fake_snapshot(client):
        return remo.Snapshot(room_temp=state["room"], humidity=state["humidity"], aircon=state["aircon"])

    async def fake_outdoor(client):
        return state["outdoor"]

    monkeypatch.setattr(remo, "fetch_snapshot", fake_snapshot)
    monkeypatch.setattr(controller.weather, "get_outdoor_temp", fake_outdoor)
    return state, calls


def run():
    return asyncio.run(controller.run_cycle(FakeClient()))


def test_送風開始時の室温を記録し_横ばいなら復帰せず_上がれば復帰する(env):
    state, calls = env

    # 1. 外気22.5℃・室温30.2℃（復帰しきい値未満）→ 送風に入る
    r = run()
    assert calls[-1]["mode"] == "blow"
    assert store.get_state("free_cool_active") is True
    assert store.get_state("free_cool_start_room") == 30.2

    # 送風中の実機状態に合わせる
    state["aircon"] = _aircon(True, "blow")

    # 2. 室温30.6℃（しきい値超え、でも上昇0.4）→ 送風のまま・APIコールなし
    state["room"] = 30.6
    n = len(calls)
    r = run()
    assert len(calls) == n
    assert "現状維持" in r["note"]
    assert store.get_state("free_cool_active") is True
    assert store.get_state("free_cool_start_room") == 30.2   # 開始時の値は更新しない

    # 3. 室温31.0℃（上昇0.8）→ 冷房に復帰、ロックアウト開始、開始時室温はクリア
    state["room"] = 31.0
    r = run()
    assert calls[-1]["mode"] == "cool"
    assert calls[-1]["temp"] == "27"        # 涼しい×暑いの目標は27℃
    assert "冷房に復帰" in r["note"]
    assert store.get_state("free_cool_active") is False
    assert store.get_state("free_cool_start_room") is None
    active, _ = controller.free_cool_lockout()
    assert active is True


def test_室温が下がっている限り送風を続ける(env):
    state, calls = env
    run()
    state["aircon"] = _aircon(True, "blow")
    for room in (30.0, 29.8, 29.6):
        state["room"] = room
        run()
        assert store.get_state("free_cool_active") is True
    assert sum(1 for c in calls if c["mode"] == "cool") == 0


def test_室温追従では実際の室温を見て設定温度を下げる(env, monkeypatch):
    """設定温度と室温がずれていても、室温を見て設定を追い込めること。"""
    from app import config

    monkeypatch.setattr(config, "ROOM_TARGET_ENABLED", True)
    monkeypatch.setattr(config, "ROOM_TARGET_LOW", 24.0)
    monkeypatch.setattr(config, "ROOM_TARGET_HIGH", 25.0)
    monkeypatch.setattr(config, "ROOM_TARGET_GAIN", 1.0)
    monkeypatch.setattr(config, "FREE_COOL_ENABLED", False)
    state, calls = env

    # 設定27℃で室温28.0℃。目標を超えているので設定を下げる
    state["aircon"] = _aircon(True, "cool", temp="27")
    state["room"] = 28.0
    state["outdoor"] = 30.0
    run()
    assert calls[-1]["mode"] == "cool"
    # 3.0℃超過 × gain1.0 → 27 から 24℃を狙うが、この機種の下限25℃に丸められる
    assert calls[-1]["temp"] == "25"

    # 室温が目標帯に入れば、それ以上は下げない
    # （風量も追従後の auto に合わせておく。ここで見たいのは設定温度）
    ac = _aircon(True, "cool", temp="25")
    ac.air_volume = "auto"
    state["aircon"] = ac
    state["room"] = 24.5
    n = len(calls)
    r = run()
    assert len(calls) == n              # 追加のAPIコールなし
    assert "現状維持" in r["note"]


def test_室温追従では目標を下回ると停止する(env, monkeypatch):
    from app import config

    monkeypatch.setattr(config, "ROOM_TARGET_ENABLED", True)
    monkeypatch.setattr(config, "ROOM_TARGET_LOW", 24.0)
    monkeypatch.setattr(config, "FREE_COOL_ENABLED", False)
    state, calls = env

    state["aircon"] = _aircon(True, "cool", temp="25")
    state["room"] = 23.5
    state["outdoor"] = 30.0     # 外が暑くても室温が下回れば止める
    run()
    assert calls[-1]["power"] == "off"
