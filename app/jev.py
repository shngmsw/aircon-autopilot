"""Jev の運転方針を比較記録する。実機の判断は変更しない。"""
import math
from datetime import datetime

import httpx

from . import config

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
    if config.JEV_MODE != "shadow":
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
                    "Respect supported_modes and constraints; do not override manual suspension or switching/hold restrictions. "
                    "Prefer avoiding unnecessary energy use for small overshoots. "
                    "Cold outdoor air alone does not prove passive cooling. "
                    "A decline while cooling was on is not evidence of passive cooling. "
                    "Use the precomputed differences and passive_trend; do not predict numeric temperatures. "
                    "Choose uncertain when evidence is insufficient. This is advisory only.",
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
