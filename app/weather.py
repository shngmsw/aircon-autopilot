"""Open-Meteo API（気象庁MSMモデル）から外気温を取得する（APIキー不要）。"""
import logging

import httpx

from . import config

log = logging.getLogger(__name__)

FORECAST_URL = "https://api.open-meteo.com/v1/jma"


async def get_outdoor_temp(client: httpx.AsyncClient) -> float | None:
    """設定された座標の現在気温(℃)を返す。取得できなければ None。"""
    try:
        res = await client.get(
            FORECAST_URL,
            params={
                "latitude": config.LATITUDE,
                "longitude": config.LONGITUDE,
                "current": "temperature_2m",
                "timezone": "Asia/Tokyo",
            },
            timeout=config.HTTP_TIMEOUT,
        )
        res.raise_for_status()
        data = res.json()

        temp = data.get("current", {}).get("temperature_2m")
        if temp is None:
            log.warning("Open-Meteo のレスポンスに気温データが見つかりません")
            return None
        return float(temp)
    except Exception:
        log.exception("外気温の取得に失敗")
        return None
