"""Nature Remo Cloud API クライアント。

- 室温・湿度の取得（センサー補正つき）
- エアコンの現在設定と、機種が受け付ける温度/風量の一覧を取得
- 設定の送信（機種の対応値に丸めてから送る）
"""
import logging
from dataclasses import dataclass, field

import httpx

from . import config

log = logging.getLogger(__name__)

API = "https://api.nature.global/1"


class RemoError(Exception):
    pass


@dataclass
class AirconState:
    appliance_id: str
    nickname: str
    power_on: bool
    mode: str                 # 現在の運転モード (cool など)
    target_temp: str          # 現在の設定温度（文字列のまま保持）
    air_volume: str           # 現在の風量
    temp_options: list[str] = field(default_factory=list)  # 機種が受ける温度一覧
    vol_options: list[str] = field(default_factory=list)   # 機種が受ける風量一覧


@dataclass
class Snapshot:
    room_temp: float | None
    humidity: float | None
    aircon: AirconState | None


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.NATURE_ACCESS_TOKEN}",
        "accept": "application/json",
    }


async def fetch_snapshot(client: httpx.AsyncClient) -> Snapshot:
    """室温・湿度とエアコン状態をまとめて取得する。"""
    room_temp = None
    humidity = None
    try:
        res = await client.get(f"{API}/devices", headers=_headers(), timeout=config.HTTP_TIMEOUT)
        res.raise_for_status()
        for d in res.json():
            events = d.get("newest_events", {})
            if "te" in events and room_temp is None:
                room_temp = float(events["te"]["val"]) + config.ROOM_TEMP_OFFSET
            if "hu" in events and humidity is None:
                humidity = float(events["hu"]["val"])
    except Exception:
        log.exception("Remoデバイス情報の取得に失敗")

    aircon = None
    try:
        res = await client.get(f"{API}/appliances", headers=_headers(), timeout=config.HTTP_TIMEOUT)
        res.raise_for_status()
        for a in res.json():
            if a.get("type") not in ("AC", "AIRCON"):
                continue
            if config.APPLIANCE_ID and a["id"] != config.APPLIANCE_ID:
                continue
            settings = a.get("settings") or {}
            cool = (a.get("aircon", {}).get("range", {}).get("modes", {}).get("cool", {}))
            aircon = AirconState(
                appliance_id=a["id"],
                nickname=a.get("nickname", "エアコン"),
                power_on=settings.get("button", "") != "power-off",
                mode=settings.get("mode", ""),
                target_temp=str(settings.get("temp", "")),
                air_volume=str(settings.get("vol", "")),
                temp_options=[str(t) for t in cool.get("temp", []) if str(t)],
                vol_options=[str(v) for v in cool.get("vol", []) if str(v)],
            )
            break
    except Exception:
        log.exception("Remo家電情報の取得に失敗")

    return Snapshot(room_temp=room_temp, humidity=humidity, aircon=aircon)


def nearest_temp(target: float, options: list[str]) -> str:
    """希望温度を、機種が受け付ける温度のうち最も近い値に丸める。"""
    numeric = []
    for o in options:
        try:
            numeric.append((abs(float(o) - target), o))
        except ValueError:
            continue
    if not numeric:
        return str(int(target))
    numeric.sort(key=lambda x: x[0])
    return numeric[0][1]


def pick_volume(pref: str, options: list[str]) -> str:
    """希望風量('auto'=自動/強め, 'min'=弱め)を機種の対応値から選ぶ。"""
    if not options:
        return ""
    if pref == "auto":
        if "auto" in options:
            return "auto"
        # autoが無ければ数値の最大（最強）
        nums = sorted((o for o in options if o.replace(".", "").isdigit()), key=float)
        return nums[-1] if nums else options[-1]
    # 弱め: 数値の最小
    nums = sorted((o for o in options if o.replace(".", "").isdigit()), key=float)
    return nums[0] if nums else options[0]


async def apply_settings(
    client: httpx.AsyncClient,
    aircon: AirconState,
    power: str,               # "on" / "off"
    target_temp: str | None = None,
    air_volume: str | None = None,
) -> None:
    """エアコンへ設定を送信する。offなら電源オフのみ送る。"""
    url = f"{API}/appliances/{aircon.appliance_id}/aircon_settings"
    if power == "off":
        payload = {"button": "power-off"}
    else:
        payload = {"operation_mode": "cool", "button": ""}
        if target_temp:
            payload["temperature"] = target_temp
        if air_volume:
            payload["air_volume"] = air_volume
    res = await client.post(url, headers=_headers(), data=payload, timeout=config.HTTP_TIMEOUT)
    if res.status_code >= 400:
        raise RemoError(f"エアコン操作に失敗: HTTP {res.status_code} {res.text[:200]}")
    log.info("エアコン操作: power=%s temp=%s vol=%s", power, target_temp, air_volume)
