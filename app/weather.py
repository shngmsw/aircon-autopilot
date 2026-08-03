"""気象庁アメダスの公開JSONから外気温を取得する（APIキー不要・10分更新）。"""
import logging
from datetime import datetime

import httpx

from . import config

log = logging.getLogger(__name__)

LATEST_TIME_URL = "https://www.jma.go.jp/bosai/amedas/data/latest_time.txt"
MAP_URL = "https://www.jma.go.jp/bosai/amedas/data/map/{key}.json"


async def get_outdoor_temp(client: httpx.AsyncClient) -> float | None:
    """設定された観測所の最新気温(℃)を返す。取得できなければ None。"""
    try:
        res = await client.get(LATEST_TIME_URL, timeout=config.HTTP_TIMEOUT)
        res.raise_for_status()
        latest = datetime.fromisoformat(res.text.strip())
        key = latest.strftime("%Y%m%d%H%M%S")

        res = await client.get(MAP_URL.format(key=key), timeout=config.HTTP_TIMEOUT)
        res.raise_for_status()
        data = res.json()

        station = data.get(config.AMEDAS_STATION)
        if not station or "temp" not in station:
            log.warning("観測所 %s の気温データが見つかりません", config.AMEDAS_STATION)
            return None
        # temp は [値, 品質フラグ] の形式。フラグ0が正常値
        val, flag = station["temp"][0], station["temp"][1]
        if flag != 0:
            log.warning("観測所 %s の気温は品質フラグ %s（参考値として使用）", config.AMEDAS_STATION, flag)
        return float(val)
    except Exception:
        log.exception("外気温の取得に失敗")
        return None
