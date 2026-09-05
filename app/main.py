"""FastAPI サーバー: ダッシュボード配信 + REST API + 定期制御ループ。"""
import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import config, controller, logic, remo, store

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
    mode: str | None = None         # "cool"(既定) / "blow" など


class SettingsBody(BaseModel):
    low: float
    high: float


@app.get("/api/status")
async def status():
    snap = await remo.fetch_snapshot(_client)
    latest = store.latest_reading()
    aircon = snap.aircon
    lockout_active, lockout_until = controller.free_cool_lockout()
    rt_low, rt_high, rt_source = controller.effective_target_band()
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
            "modes": aircon.modes,
            "supports_blow": remo.supports_mode(aircon, logic.BLOW_MODE),
        } if aircon else None,
        "free_cool": {
            "enabled": config.FREE_COOL_ENABLED,
            "active": bool(store.get_state("free_cool_active", False)),
            "lockout_until": (
                lockout_until.isoformat(timespec="seconds")
                if lockout_active and lockout_until else None
            ),
        },
        "room_target": {
            "enabled": config.ROOM_TARGET_ENABLED,
            "low": rt_low,
            "high": rt_high,
            "source": rt_source,
        },
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
        store.set_state("free_cool_active", False)
    return {"auto": store.auto_enabled(), "auto_state": store.auto_state()}


@app.post("/api/aircon")
async def manual_control(body: AirconBody):
    """手動操作。実行すると自動制御は一時停止する（トグルで再開）。"""
    snap = await remo.fetch_snapshot(_client)
    if snap.aircon is None:
        raise HTTPException(502, "エアコンが見つかりません")
    if body.power not in ("on", "off"):
        raise HTTPException(400, "power は on / off を指定してください")

    mode = body.mode or "cool"
    if body.power == "on" and snap.aircon.modes and mode not in snap.aircon.modes:
        raise HTTPException(400, f"この機種は運転モード {mode} に対応していません")

    was_auto = store.auto_enabled()
    try:
        await remo.apply_settings(
            _client, snap.aircon, body.power, body.target_temp, body.air_volume,
            mode=mode,
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
        + ("（送風）" if body.power == "on" and mode == logic.BLOW_MODE else "")
        + ("（自動制御を一時停止しました）" if was_auto else ""),
        mode=mode if body.power == "on" else None,
    )
    return {"ok": True, "auto": False, "auto_paused": was_auto}


@app.get("/api/settings")
async def get_settings():
    low, high, source = controller.effective_target_band()
    return {"room_target_low": low, "room_target_high": high, "source": source}


@app.post("/api/settings")
async def set_settings(body: SettingsBody):
    """目標室温の帯を保存し、すぐ判定サイクルを回して反映する。"""
    if not (16.0 <= body.low and body.high <= 30.0 and body.low + 0.5 <= body.high):
        raise HTTPException(
            422, "目標帯は 16〜30℃ の範囲で、下限+0.5℃ ≦ 上限 にしてください"
        )
    store.set_state("room_target_low", body.low)
    store.set_state("room_target_high", body.high)
    cycle = await controller.run_cycle(_client)
    low, high, source = controller.effective_target_band()
    return {
        "room_target_low": low,
        "room_target_high": high,
        "source": source,
        "cycle": cycle,
    }


@app.post("/api/run-now")
async def run_now():
    """制御サイクルを即時実行（動作確認用）。"""
    result = await controller.run_cycle(_client)
    return result
