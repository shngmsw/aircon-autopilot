"""Offline controller simulation; --jev adds advisory-only TypeSafe calls.

Run from the repository root with .venv/bin/python scripts/simulate_control.py.
The thermal model is hypothetical, not calibrated to a real room or energy meter.
"""
import argparse
import asyncio
import json
import logging
import re
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import config, controller, jev, logic, remo, store


@dataclass(frozen=True)
class Scenario:
    name: str
    room: float = 22.1
    outdoor: float = 19.3
    conductance: float = 0.04  # temperature equilibration per hour
    load: float = 0.2         # internal/solar heat, degrees per hour
    initial_mode: str = "cool"
    missing_at: int | None = None
    manual_at: int | None = None
    heating_tail: float = 0.0


SCENARIOS = [
    Scenario("passive_cooling", conductance=0.08, load=0.15),
    Scenario("insulated_room", conductance=0.01, load=0.4),
    Scenario("solar_gain", load=1.0),
    Scenario("warm_outdoors", outdoor=26, load=0.5),
    Scenario("winter_heating", room=19.5, outdoor=5, initial_mode="warm"),
    Scenario("heating_afterglow", room=24.2, outdoor=10, initial_mode="warm", heating_tail=0.8),
    Scenario("missing_sensor", missing_at=6),
    Scenario("manual_stop", manual_at=6),
]


def appliance(mode="cool"):
    options = {m: {"temp": [str(t) for t in range(16, 31)], "vol": ["1", "auto"]}
               for m in ("cool", "warm")}
    options["blow"] = {"temp": [], "vol": ["1", "auto"]}
    return remo.AirconState("simulated", "simulated", True, mode, "23", "auto",
                           options["cool"]["temp"], ["1", "auto"], list(options), options)


async def simulate(scenario, enabled):
    """Exercise the actual controller, SQLite, holds, switching and rounding.

    Remo reads/writes and weather are replaced before any cycle runs.
    Sampling is every 10 min for 8 hours; dynamics integrate at one minute.
    """
    ac = appliance(scenario.initial_mode)
    start = datetime(2026, 10, 7, tzinfo=config.JST)
    clock = [start]
    room = [scenario.room]
    cycle = [0]
    commands = []
    samples = []
    violations = 0
    outside_minutes = 0
    max_deviation = 0.0
    active_minutes = 0
    heating_tail = scenario.heating_tail

    async def snapshot(client):
        temp = None if cycle[0] == scenario.missing_at else room[0]
        return remo.Snapshot(temp, 55, ac)

    async def weather(client):
        return scenario.outdoor

    async def apply(client, aircon, power, temp, vol, mode="cool"):
        commands.append({"cycle": cycle[0], "power": power, "mode": mode,
                         "start": power == "on" and not aircon.power_on})

    with tempfile.TemporaryDirectory() as tmp:
        previous_conn = store._conn
        store._conn = None
        overrides = dict(DB_PATH=str(Path(tmp) / "aircon.db"), JEV_MODE="off",
                         PASSIVE_WAIT_ENABLED=enabled, ROOM_TARGET_ENABLED=True,
                         ROOM_TARGET_LOW=21., ROOM_TARGET_HIGH=24., HYSTERESIS=.7,
                         FREE_COOL_ENABLED=False, MIN_HOLD_MIN=10,
                         MODE_SWITCH_COOLDOWN_MIN=60, PASSIVE_WAIT_OUT_MAX=20.,
                         PASSIVE_WAIT_MIN_GAP=2., ROOM_TARGET_GAIN=1.5,
                         ROOM_TARGET_SET_MIN=16., ROOM_TARGET_SET_MAX=30.,
                         ROOM_TARGET_DEADBAND=.3, ROOM_TARGET_MAX_VOL_OVER=1.,
                         HEAT_OUT_MAX=20., FREE_COOL_ABORT_ROOM=30.5)
        try:
            with patch.multiple(config, **overrides), patch.object(store, "now_jst", lambda: clock[0]), \
                 patch.object(remo, "fetch_snapshot", snapshot), patch.object(remo, "apply_settings", apply), \
                 patch.object(controller.weather, "get_outdoor_temp", weather):
                for step in range(48):
                    cycle[0] = step
                    if step == scenario.manual_at:
                        ac.power_on = False
                        store.set_auto_state("off")
                    count = len(commands)
                    result = await controller.run_cycle(None)
                    if step == scenario.missing_at and len(commands) != count:
                        violations += 1
                    if scenario.manual_at is not None and step >= scenario.manual_at and len(commands) != count:
                        violations += 1
                    samples.append({"minute": step * 10, "room": round(room[0], 3),
                                    "outdoor": scenario.outdoor, "power": "on" if ac.power_on else "off",
                                    "set_temp": ac.target_temp,
                                    "mode": ac.mode if ac.power_on else None, "note": result["note"]})
                    for _ in range(10):
                        rate = scenario.conductance * (scenario.outdoor - room[0]) + scenario.load + heating_tail
                        if ac.power_on and ac.mode == "cool" and room[0] > float(ac.target_temp):
                            rate -= 1.8
                            active_minutes += 1
                        elif ac.power_on and ac.mode == "warm" and room[0] < float(ac.target_temp):
                            rate += 2.4
                            active_minutes += 1
                        room[0] += rate / 60
                        heating_tail *= 0.96
                        deviation = max(21 - room[0], room[0] - 24, 0)
                        outside_minutes += int(deviation > 0)
                        max_deviation = max(max_deviation, deviation)
                        clock[0] += timedelta(minutes=1)
                on_commands = [c for c in commands if c["power"] == "on"]
                modes = [scenario.initial_mode] + [c["mode"] for c in on_commands]
                switches = sum(a != b for a, b in zip(modes, modes[1:]))
                metrics = {"outside_band_minutes": outside_minutes, "max_deviation_c": round(max_deviation, 2),
                           "modeled_active_minutes": active_minutes, "commands": len(commands),
                           "starts": sum(c["start"] for c in commands),
                           "cool_heat_switches": switches, "safety_violations": violations}
        finally:
            if store._conn:
                store._conn.close()
            store._conn = previous_conn
    return {"metrics": metrics, "samples": samples, "commands": commands}


def replay(rows):
    """One-step recommendations with observed pre-action state, not alternate trajectories.

    Only current and earlier rows are read. Targets are extracted from the logged
    note; unknown hardware, hold and switching state are not reconstructed.
    """
    results = []
    band = None
    for i, row in enumerate(rows):
        match = re.search(r"目標帯\(([\d.]+)〜([\d.]+)℃\)", row.get("note", ""))
        if match:
            band = tuple(map(float, match.groups()))
        low_match = re.search(r"目標([\d.]+)℃を下回る", row.get("note", ""))
        if low_match and band:
            band = (float(low_match[1]), band[1])
        if i == 0 or band is None or row["room"] is None or row["outdoor"] is None:
            continue
        prev = rows[i-1]
        current_set = prev["set_temp"] if prev["power"] == "on" and prev["mode"] in ("cool", "warm") else None
        common = dict(room_target_enabled=True, room_target_low=band[0], room_target_high=band[1],
                      current_set_temp=current_set, heating_now=prev["power"] == "on" and prev["mode"] == "warm",
                      free_cool_enabled=False)
        before = logic.decide(row["outdoor"], row["room"], None, None, .7, **common)
        after = logic.decide(row["outdoor"], row["room"], None, None, .7, **common, passive_wait_enabled=True)
        results.append({"ts": row["ts"], "room": row["room"], "outdoor": row["outdoor"],
                        "target": band, "observed_power": row["power"], "observed_mode": row["mode"],
                        "baseline": {"power": before.power, "mode": before.mode},
                        "changed": {"power": after.power, "mode": after.mode},
                        "different": (before.power, before.mode) != (after.power, after.mode)})
    return results


async def run(args):
    logging.getLogger().setLevel(logging.ERROR)
    report = {"assumptions": {"duration_minutes": 480, "sample_minutes": 10,
               "integration_minutes": 1, "target_band": [21, 24],
               "thermal_model": "hypothetical first-order model, not fitted to real data",
               "cooling_rate_c_per_hour": 1.8, "heating_rate_c_per_hour": 2.4,
               "free_cool": False, "reason": "isolates stop-and-wait from the existing fan policy",
               "jev": "advisory only; recommendations do not drive the thermal model",
               "energy": "modeled active minutes are not measured electricity or compressor runtime"}, "scenarios": {}}
    for scenario in SCENARIOS:
        report["scenarios"][scenario.name] = {
            "parameters": vars(scenario), "baseline": await simulate(scenario, False),
            "changed": await simulate(scenario, True)}
    if args.history:
        rows = json.loads(Path(args.history).read_text())["rows"]
        if any(datetime.fromisoformat(a["ts"]) > datetime.fromisoformat(b["ts"]) for a, b in zip(rows, rows[1:])):
            raise ValueError("History must be sorted by timestamp")
        report["replay"] = replay(rows)
    if args.jev:
        if not config.TYPESAFE_API_KEY:
            raise RuntimeError("TYPESAFE_API_KEY is required for --jev")
        import httpx
        with patch.object(config, "JEV_MODE", "shadow"):
            async with httpx.AsyncClient() as client:
                for scenario in SCENARIOS:
                    if scenario.missing_at is not None or scenario.manual_at is not None:
                        report["scenarios"][scenario.name]["jev"] = {"status": "skipped", "reason": "sensor_missing_or_manual_stop"}
                        continue
                    samples = report["scenarios"][scenario.name]["baseline"]["samples"]
                    sample = samples[6]
                    prev = samples[5]
                    ac = appliance(scenario.initial_mode)
                    ac.power_on = prev["power"] == "on"
                    ac.mode = prev["mode"] or ""
                    ac.target_temp = prev["set_temp"]
                    start = datetime(2026, 10, 7, tzinfo=config.JST)
                    now = start + timedelta(minutes=sample["minute"])
                    history = [{**s, "ts": (start + timedelta(minutes=s["minute"])).isoformat()}
                               for s in samples[:6]]
                    d = logic.Decision(sample["power"], float(sample["set_temp"]), None, 0, 0,
                                       sample["note"], mode=sample["mode"])
                    state = jev.build_state(room=sample["room"], outdoor=scenario.outdoor, humidity=55,
                                            low=21, high=24, aircon=ac, decision=d, history=history, now=now,
                                            constraints={"auto_state": "on", "hold_active": False, "switch_wait": False})
                    result = await jev.evaluate(client, state)
                    report["scenarios"][scenario.name]["jev"] = result
                    if result["status"] != "ok":
                        raise RuntimeError(f"Jev validation failed: {result.get('reason')}")
    Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    for name, data in report["scenarios"].items():
        print(name, json.dumps({k: data[k]["metrics"] for k in ("baseline", "changed")}))
    if "replay" in report:
        print("replay", len(report["replay"]), "rows; changed recommendations", sum(r["different"] for r in report["replay"]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--history", help="Optional uploaded history JSON; never calls the production server")
    parser.add_argument("--jev", action="store_true", help="Make 6 advisory API calls using one hour of synthetic history")
    parser.add_argument("--output", default="/tmp/aircon-simulation.json")
    asyncio.run(run(parser.parse_args()))
