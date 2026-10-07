"""Jev の運転方針と、既存の制約を守る実制御への変換。"""
import math
from dataclasses import replace
from datetime import datetime

import httpx

from . import config, logic, remo

API = "https://api.typesafe.ai/v1/systemone"
OPTIONS = {
    "wait": "Leave the appliance off and observe; passive temperature drift may restore comfort without energy use.",
    "cool": "Use active cooling to restore comfort when waiting or circulation is unlikely to suffice.",
    "heat": "Use heating to restore comfort when the room is too cold and passive warming is unlikely to suffice.",
    "blow": "Use fan-only circulation, without assuming it brings outdoor air inside; only if supported.",
    "maintain": "Keep the current appliance operation unchanged; comfort is acceptable or more observation is needed after a change.",
    "uncertain": "Evidence is missing or conflicting; no reliable recommendation can be made."
}


def build_state(*, room, outdoor, humidity, low, high, aircon, decision, history, now, constraints=None):
    samples = []
    for row in history[-12:]:
        try:
            age = (now - datetime.fromisoformat(row["ts"])).total_seconds() / 60
            temp = float(row["room"])
        except (ValueError, TypeError, KeyError):
            continue
        if not (0 < age <= 60 and math.isfinite(temp)):
            continue
        samples.append({"minutes_ago": round(age, 1), "room_c": temp,
                        "outdoor_c": row.get("outdoor"), "humidity_percent": row.get("humidity"),
                        "set_temp": row.get("set_temp"),
                        "power": row["power"], "mode": row["mode"]})
    # 停止中に得た連続した履歴だけを自然冷却の根拠にする。
    passive = []
    if not aircon.power_on:
        for sample in reversed(samples):
            if sample["power"] != "off":
                break
            passive.append(sample)
    change = room - passive[-1]["room_c"] if passive else None
    overall_change = room - samples[0]["room_c"] if samples else None
    return {
        "room_c": room, "outdoor_c": outdoor, "humidity_percent": humidity,
        "target_band_c": {"low": low, "high": high},
        "overshoot_c": round(room - high, 2),
        "undershoot_c": round(low - room, 2),
        "comfort": "hot" if room > high else "cold" if room < low else "in_band",
        "constraints": constraints or {},
        "outdoor_colder_by_c": round(room - outdoor, 2),
        "aircon": {"power_on": aircon.power_on, "mode": aircon.mode,
                   "supported_modes": aircon.modes, "set_temp": aircon.target_temp},
        "baseline": {"power": decision.power, "mode": decision.mode,
                     "set_temp": decision.target_temp, "reason": decision.reason},
        "recent_samples": samples,
        "recent_change_c": round(overall_change, 2) if overall_change is not None else None,
        "recent_trend": ("unknown" if overall_change is None else
                         "falling" if overall_change < -0.2 else "rising" if overall_change > 0.2 else "stable"),
        "passive_change_c": round(change, 2) if change is not None else None,
        "passive_window_minutes": passive[-1]["minutes_ago"] if passive else None,
        "passive_trend": ("unknown" if change is None else
                          "falling" if change < -0.2 else "rising" if change > 0.2 else "stable"),
    }


def _probability(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("invalid probability")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("invalid probability")
    return value


async def evaluate(client: httpx.AsyncClient, state: dict) -> dict:
    if config.JEV_MODE == "off":
        return {"status": "disabled"}
    if config.JEV_MODE not in ("shadow", "active"):
        return {"status": "unavailable", "reason": "invalid_mode"}
    if not config.TYPESAFE_API_KEY:
        return {"status": "unavailable", "reason": "missing_api_key"}
    try:
        response = await client.post(
            API, headers={"Authorization": f"Bearer {config.TYPESAFE_API_KEY}"},
            json={"model": config.JEV_MODEL, "state": state, "questions": {
                "operation": {"type": "choice", "instructions":
                    "Choose the overall appliance operation: wait, cool, heat, blow, maintain, or uncertain. "
                    "Consider comfort, humidity, recent operation, temperature trends and energy use. "
                    "The baseline is a reference, not the correct answer. "
                    "Comfortable temperature while cooling is on does not prove continued cooling is necessary. "
                    "Consider switching off to observe when the room is comfortable and outdoors is colder; "
                    "avoid starting cooling within the target band unless rising temperature makes it necessary. "
                    "Respect supported_modes and constraints; do not override manual suspension or switching/hold restrictions. "
                    "Prefer avoiding unnecessary energy use for small overshoots. "
                    "Cold outdoor air alone does not prove passive cooling. "
                    "A decline while cooling was on is not evidence of passive cooling. "
                    "Use the precomputed differences and passive_trend; do not predict numeric temperatures. "
                    "Choose uncertain when evidence is insufficient. Code enforces constraints and sets numeric temperatures.",
                    "criteria": OPTIONS}}}, timeout=config.JEV_TIMEOUT,
        )
        response.raise_for_status()
        body = response.json()
        answer = body["answers"]["operation"]
        choice = answer["choice"]
        if answer["type"] != "choice" or choice not in OPTIONS:
            raise ValueError("invalid choice")
        confidence = _probability(answer["confidence"])
        probabilities = {key: _probability(answer["probabilities"][key]) for key in OPTIONS}
        if abs(sum(probabilities.values()) - 1) > 0.02 or not isinstance(body["model"], str):
            raise ValueError("invalid probability sum")
        return {"status": "ok", "choice": choice, "confidence": confidence,
                "probabilities": probabilities, "model": body["model"], "applied": False}
    except httpx.HTTPStatusError as exc:
        return {"status": "unavailable", "reason": f"http_{exc.response.status_code}"}
    except (httpx.RequestError, ValueError, KeyError, TypeError):
        # レスポンスや例外本文には認証情報が含まれ得るため保存・ログ出力しない。
        return {"status": "unavailable", "reason": "request_or_response_error"}


def select_decision(baseline, result, *, aircon, room, outdoor, low, high,
                    holding=False, recent_mode=None, lockout=False):
    """Return the effective decision and why an override was accepted/rejected.

    Numeric setpoints remain deterministic. This function never sends commands.
    """
    if config.JEV_MODE != "active":
        return baseline, "shadow"
    if result.get("status") != "ok" or result.get("choice") == "uncertain":
        return baseline, "unavailable_or_uncertain"
    choice = result["choice"]
    if not (0 <= config.JEV_MIN_CONFIDENCE <= 1 and 0 <= config.JEV_MIN_PROBABILITY <= 1):
        return baseline, "invalid_threshold"
    if (result["confidence"] < config.JEV_MIN_CONFIDENCE
            or result["probabilities"][choice] < config.JEV_MIN_PROBABILITY):
        return baseline, "low_confidence"
    if holding:
        return baseline, "holding"
    if not config.ROOM_TARGET_ENABLED:
        return baseline, "room_target_disabled"

    if choice == "maintain":
        power = "on" if aircon.power_on else "off"
        mode = aircon.mode if aircon.power_on else None
    else:
        power = "off" if choice == "wait" else "on"
        mode = {"cool": "cool", "heat": logic.WARM_MODE, "blow": logic.BLOW_MODE}.get(choice)
    if power == "off":
        if room >= config.FREE_COOL_ABORT_ROOM or room < low - max(0, config.HYSTERESIS):
            return baseline, "temperature_limit"
    else:
        if not remo.supports_mode(aircon, mode):
            return baseline, "unsupported_mode"
        if mode == logic.WARM_MODE:
            if outdoor >= config.HEAT_OUT_MAX:
                return baseline, "heating_outdoor_limit"
            heat_off = min(low + config.HYSTERESIS, high)
            if room >= (heat_off if choice == "maintain" else low):
                return baseline, "heating_room_limit"
            if recent_mode in ("cool", logic.BLOW_MODE):
                return baseline, "switch_wait"
        else:
            if room < low:
                return baseline, "cooling_room_limit"
            if recent_mode == logic.WARM_MODE:
                return baseline, "switch_wait"
            if mode == logic.BLOW_MODE and (lockout or room >= config.FREE_COOL_ABORT_ROOM):
                return baseline, "fan_limit"

    temp = None
    volume = None
    if power == "on":
        if choice == "maintain":
            try:
                temp = float(aircon.target_temp) if mode != logic.BLOW_MODE else None
                if temp is not None and not math.isfinite(temp):
                    return baseline, "invalid_current_temperature"
            except (TypeError, ValueError):
                return baseline, "invalid_current_temperature"
            if temp is not None and (not config.ROOM_TARGET_SET_MIN <= temp <= config.ROOM_TARGET_SET_MAX
                                     or mode == logic.WARM_MODE and temp > high):
                return baseline, "current_temperature_limit"
            volume = None  # Keep the appliance's existing fan setting.
        elif mode == logic.BLOW_MODE:
            volume = "min"
        else:
            try:
                current = float(aircon.target_temp) if aircon.power_on and aircon.mode == mode else None
                if current is not None and not math.isfinite(current):
                    return baseline, "invalid_current_temperature"
            except (TypeError, ValueError):
                current = None
            _, temp, _, _ = logic.room_target_setpoint(
                room, current, low, high, config.ROOM_TARGET_GAIN,
                config.ROOM_TARGET_SET_MIN, config.ROOM_TARGET_SET_MAX,
                config.ROOM_TARGET_DEADBAND, heat_allowed=mode == logic.WARM_MODE,
                heating_now=mode == logic.WARM_MODE and aircon.power_on and aircon.mode == mode,
                hyst=config.HYSTERESIS,
            )
            # Within the band a cooling start uses the upper target, without lowering it.
            if temp is None and mode == "cool":
                temp = high
            volume = "max" if max(room - high, low - room) >= config.ROOM_TARGET_MAX_VOL_OVER else "auto"
    return replace(baseline, power=power, mode=mode, target_temp=temp, volume_pref=volume,
                   free_cool=False, free_cool_abort=False, switch_wait=False, passive_wait=False,
                   reason=f"Jev: {choice}（信頼度{result['confidence']:.0%}）"), "accepted"
