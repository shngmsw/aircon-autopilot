"""/api/settings の検証・保存と /api/status の目標帯表示のテスト。

TestClient を context manager にせず使うことで lifespan（スケジューラ起動）を
走らせない。制御サイクルはモックする。
"""
import pytest
from fastapi.testclient import TestClient

from app import config, controller, main, store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    store._conn = None
    monkeypatch.setattr(config, "ROOM_TARGET_LOW", 24.0)
    monkeypatch.setattr(config, "ROOM_TARGET_HIGH", 25.5)

    async def fake_cycle(client):
        return {"action": "set", "note": "test cycle"}

    monkeypatch.setattr(controller, "run_cycle", fake_cycle)
    return TestClient(main.app)


def test_保存前はenvの値が返る(client):
    body = client.get("/api/settings").json()
    assert body == {"room_target_low": 24.0, "room_target_high": 25.5, "source": "env"}


def test_目標帯を保存するとオーバーライドが有効になり判定も走る(client):
    res = client.post("/api/settings", json={"low": 20.0, "high": 22.0})
    assert res.status_code == 200
    j = res.json()
    assert (j["room_target_low"], j["room_target_high"], j["source"]) == (20.0, 22.0, "override")
    assert j["cycle"]["note"] == "test cycle"
    assert store.get_state("room_target_low") == 20.0
    assert store.get_state("room_target_high") == 22.0

    body = client.get("/api/settings").json()
    assert body == {"room_target_low": 20.0, "room_target_high": 22.0, "source": "override"}


@pytest.mark.parametrize("low,high", [
    (15.0, 22.0),    # low が下限16℃未満
    (20.0, 31.0),    # high が上限30℃超
    (23.0, 21.0),    # 上下逆
    (22.0, 22.4),    # 帯の幅が0.5℃未満
])
def test_不正な目標帯は422で保存されない(client, low, high):
    res = client.post("/api/settings", json={"low": low, "high": high})
    assert res.status_code == 422
    assert store.get_state("room_target_low") is None
