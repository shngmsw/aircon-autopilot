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
    temp_options: list[str] = field(default_factory=list)  # 機種が受ける温度一覧(cool用・後方互換)
    vol_options: list[str] = field(default_factory=list)   # 機種が受ける風量一覧(cool用・後方互換)
    modes: list[str] = field(default_factory=list)         # 機種が対応する運転モード名
    # モード別の対応値: {"cool": {"temp": [...], "vol": [...]}, "blow": {...}}
    mode_options: dict[str, dict[str, list[str]]] = field(default_factory=dict)


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


def _parse_mode_options(appliance: dict) -> dict[str, dict[str, list[str]]]:
    """家電情報の range.modes を全モード分パースする。

    返り値は {"cool": {"temp": [...], "vol": [...]}, "blow": {...}, ...}。
    送風(blow)は温度を持たないことが多く、その場合 temp は空リストになる。
    """
    raw = (appliance.get("aircon") or {}).get("range", {}).get("modes", {}) or {}
    out: dict[str, dict[str, list[str]]] = {}
    for name, spec in raw.items():
        if not isinstance(spec, dict):
            continue
        out[str(name)] = {
            "temp": [str(t) for t in spec.get("temp", []) if str(t)],
            "vol": [str(v) for v in spec.get("vol", []) if str(v)],
        }
    return out


def supports_mode(aircon: AirconState, mode: str) -> bool:
    """機種がその運転モードに対応しているか。"""
    return mode in aircon.modes


def mode_temp_options(aircon: AirconState, mode: str) -> list[str]:
    """指定モードで機種が受け付ける温度一覧。"""
    opts = aircon.mode_options.get(mode)
    if opts is None:
        # モード情報が取れなかった場合は従来どおり cool用の一覧で代用する
        return aircon.temp_options if mode == "cool" else []
    return opts.get("temp", [])


def mode_vol_options(aircon: AirconState, mode: str) -> list[str]:
    """指定モードで機種が受け付ける風量一覧。"""
    opts = aircon.mode_options.get(mode)
    if opts is None:
        return aircon.vol_options if mode == "cool" else []
    return opts.get("vol", [])


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
            mode_options = _parse_mode_options(a)
            cool = mode_options.get("cool", {})
            aircon = AirconState(
                appliance_id=a["id"],
                nickname=a.get("nickname", "エアコン"),
                power_on=settings.get("button", "") != "power-off",
                mode=settings.get("mode", ""),
                target_temp=str(settings.get("temp", "")),
                air_volume=str(settings.get("vol", "")),
                temp_options=cool.get("temp", []),
                vol_options=cool.get("vol", []),
                modes=list(mode_options.keys()),
                mode_options=mode_options,
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
    """希望風量('auto'=エアコン任せ, 'max'=最強, 'min'=弱め)を機種の対応値から選ぶ。"""
    if not options:
        return ""
    nums = sorted((o for o in options if o.replace(".", "").isdigit()), key=float)
    if pref == "auto":
        if "auto" in options:
            return "auto"
        # autoが無い機種では最強で代用する
        return nums[-1] if nums else options[-1]
    if pref == "max":
        # 一気に冷やしたいときは自動任せにせず最強を指定する
        return nums[-1] if nums else options[-1]
    # 弱め: 数値の最小
    return nums[0] if nums else options[0]


async def apply_settings(
    client: httpx.AsyncClient,
    aircon: AirconState,
    power: str,               # "on" / "off"
    target_temp: str | None = None,
    air_volume: str | None = None,
    mode: str = "cool",       # "cool" / "blow" など
) -> None:
    """エアコンへ設定を送信する。offなら電源オフのみ送る。

    送風など温度を持たないモードでは temperature を送らない（機種によっては
    受け付けずエラーになる）。モード情報が取れていない場合は従来どおり送る。
    """
    url = f"{API}/appliances/{aircon.appliance_id}/aircon_settings"
    if power == "off":
        payload = {"button": "power-off"}
    else:
        payload = {"operation_mode": mode, "button": ""}
        known = aircon.mode_options.get(mode)
        accepts_temp = known is None or bool(known.get("temp"))
        if target_temp and accepts_temp:
            payload["temperature"] = target_temp
        if air_volume:
            payload["air_volume"] = air_volume
    res = await client.post(url, headers=_headers(), data=payload, timeout=config.HTTP_TIMEOUT)
    if res.status_code >= 400:
        raise RemoError(f"エアコン操作に失敗: HTTP {res.status_code} {res.text[:200]}")
    log.info(
        "エアコン操作: power=%s mode=%s temp=%s vol=%s", power, mode, target_temp, air_volume
    )
