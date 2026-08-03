"""FastAPI サーバー: ダッシュボード配信 + REST API + 定期制御ループ。"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import config, controller, remo, store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

_client: httpx.AsyncClient | None = None
_next_run_ts: str | None = None


async def _scheduler():
    global _next_run_ts
    interval = config.CONTROL_INTERVAL_MIN * 60
    while True:
        try:
            await controller.run_cycle(_client)
        except Exception:
            log.exception("制御サイクルで予期しないエラー")
        _next_run_ts = (
            store.now_jst().timestamp() + interval
        )
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _client
    if not config.NATURE_ACCESS_TOKEN:
        log.warning("NATURE_ACCESS_TOKEN が未設定です。.env を確認してください。")
    _client = httpx.AsyncClient()
    task = asyncio.create_task(_scheduler())
    yield
    task.cancel()
    await _client.aclose()


app = FastAPI(title="Aircon Autopilot", lifespan=lifespan)


# ---------- 画面 ----------

@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")


# ---------- API ----------

class AutoBody(BaseModel):
    enabled: bool


class AirconBody(BaseModel):
    power: str                      # "on" / "off"
    target_temp: str | None = None
    air_volume: str | None = None


@app.get("/api/status")
async def status():
    snap = await remo.fetch_snapshot(_client)
    latest = store.latest_reading()
    aircon = snap.aircon
    return {
        "now": store.now_jst().isoformat(timespec="seconds"),
        "auto": store.auto_enabled(),
        "auto_state": store.auto_state(),
        "room_temp": snap.room_temp,
        "humidity": snap.humidity,
        "outdoor_temp": latest["outdoor"] if latest else None,
        "aircon": {
            "nickname": aircon.nickname,
            "power_on": aircon.power_on,
            "mode": aircon.mode,
            "target_temp": aircon.target_temp,
            "air_volume": aircon.air_volume,
            "temp_options": aircon.temp_options,
            "vol_options": aircon.vol_options,
        } if aircon else None,
        "last_note": latest["note"] if latest else "",
        "interval_min": config.CONTROL_INTERVAL_MIN,
    }


@app.get("/api/history")
async def history(hours: float = 24):
    hours = max(1, min(hours, 24 * 31))
    return {"rows": store.get_history(hours)}


@app.post("/api/auto")
async def set_auto(body: AutoBody):
    # paused_external からもこのAPIで明示的に上書きできる
    store.set_auto_state("on" if body.enabled else "off")
    if body.enabled:
        # 再開時は帯の記憶をリセットして現況から判定し直す
        store.set_state("last_out_tier", None)
        store.set_state("last_room_tier", None)
    return {"auto": store.auto_enabled(), "auto_state": store.auto_state()}


@app.post("/api/aircon")
async def manual_control(body: AirconBody):
    """手動操作。実行すると自動制御は一時停止する（トグルで再開）。"""
    snap = await remo.fetch_snapshot(_client)
    if snap.aircon is None:
        raise HTTPException(502, "エアコンが見つかりません")
    if body.power not in ("on", "off"):
        raise HTTPException(400, "power は on / off を指定してください")

    was_auto = store.auto_enabled()
    try:
        await remo.apply_settings(
            _client, snap.aircon, body.power, body.target_temp, body.air_volume
        )
    except remo.RemoError as e:
        raise HTTPException(502, str(e))

    store.set_auto_state("off")
    store.set_state("expected_power", body.power)
    store.set_state("last_command_ts", store.now_jst().isoformat())
    store.add_reading(
        outdoor=None,
        room=snap.room_temp,
        humidity=snap.humidity,
        set_temp=float(body.target_temp) if body.target_temp else None,
        air_volume=body.air_volume,
        power=body.power,
        auto=False,
        action="manual",
        note="手動操作"
        + ("（自動制御を一時停止しました）" if was_auto else ""),
    )
    return {"ok": True, "auto": False, "auto_paused": was_auto}


@app.post("/api/run-now")
async def run_now():
    """制御サイクルを即時実行（動作確認用）。"""
    result = await controller.run_cycle(_client)
    return result
