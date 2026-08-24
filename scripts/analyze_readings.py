#!/usr/bin/env python3
"""readings テーブルを分析して「快適帯で冷房が回り続けている問題」の実態を出す。

標準ライブラリだけで動く（pandas 不要）。ホスト上でそのまま実行できる。

    python scripts/analyze_readings.py --db data/aircon.db > docs/analysis.md

出すもの:
  1. データ概要（期間・件数・サンプリング間隔）
  2. 外気帯×室温帯ごとの滞在時間と、その中で冷房ONだった時間
  3. 快適帯(室温<25℃)で冷房ONだった時間の内訳（時間帯別・室温分布）
  4. 冷房を止めたあと、室温が 25.0℃ / 25.7℃ に戻るまでの時間（外気帯別）
  5. 簡易熱モデルによるサーモスタット方式のシミュレーション
     （停止しきい値を変えたとき ON時間・切替回数がどうなるか）
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import statistics
import sys
from collections import defaultdict
from datetime import datetime

OUT_THRESHOLDS = [33.0, 28.0]
ROOM_THRESHOLDS = [27.0, 25.0]
OUT_LABELS = ["猛暑", "夏日", "涼しい"]
ROOM_LABELS = ["暑い", "ぬるい", "快適"]
COMFORT = 2


def tier(v: float, th: list[float]) -> int:
    for i, t in enumerate(th):
        if v >= t:
            return i
    return len(th)


def load(db: str, days: float | None) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    q = "SELECT ts, outdoor, room, humidity, set_temp, air_volume, power, auto, mode FROM readings"
    args: tuple = ()
    if days:
        q += " WHERE ts >= datetime('now', 'localtime', ?)"
        args = (f"-{days} days",)
    q += " ORDER BY ts ASC"
    rows = []
    for r in conn.execute(q, args):
        if r["outdoor"] is None or r["room"] is None:
            continue
        d = dict(r)
        d["t"] = datetime.fromisoformat(d["ts"])
        # 冷房で電源ONか（送風・停止は「冷房ではない」扱い）
        d["cooling"] = d["power"] == "on" and (d["mode"] in (None, "", "cool"))
        rows.append(d)
    return rows


def fmt_h(minutes: float) -> str:
    return f"{minutes / 60:.1f}h"


# ---------- 1. 概要 ----------

def section_overview(rows: list[dict]) -> list[str]:
    gaps = [(b["t"] - a["t"]).total_seconds() / 60 for a, b in zip(rows, rows[1:])]
    gaps = [g for g in gaps if 0 < g < 120]
    med = statistics.median(gaps) if gaps else 0
    auto_ratio = sum(1 for r in rows if r["auto"]) / len(rows) * 100
    return [
        "## 1. データ概要", "",
        f"- 期間: {rows[0]['ts']} 〜 {rows[-1]['ts']}",
        f"- 件数: {len(rows)} 行（外気・室温が揃っているもの）",
        f"- サンプリング間隔（中央値）: {med:.0f} 分",
        f"- 自動制御ONの割合: {auto_ratio:.0f}%",
        "",
    ]


# ---------- 2. 帯ごとの滞在時間 ----------

def section_matrix(rows: list[dict], step: float) -> list[str]:
    stay = defaultdict(float)
    cool = defaultdict(float)
    for r in rows:
        k = (tier(r["outdoor"], OUT_THRESHOLDS), tier(r["room"], ROOM_THRESHOLDS))
        stay[k] += step
        if r["cooling"]:
            cool[k] += step
    out = ["## 2. 外気帯×室温帯ごとの滞在時間と冷房ON時間", "",
           "| 外気 | 室温 | 滞在 | 冷房ON | ON率 |", "|---|---|---|---|---|"]
    for o in range(3):
        for ro in range(3):
            s, c = stay[(o, ro)], cool[(o, ro)]
            rate = f"{c / s * 100:.0f}%" if s else "-"
            mark = " ←無駄候補" if ro == COMFORT and s and c / s > 0.5 else ""
            out.append(f"| {OUT_LABELS[o]} | {ROOM_LABELS[ro]} | {fmt_h(s)} | {fmt_h(c)} | {rate}{mark} |")
    total_cool = sum(cool.values())
    comfort_cool = sum(v for k, v in cool.items() if k[1] == COMFORT)
    out += ["",
            f"- 冷房ON合計: {fmt_h(total_cool)}",
            f"- うち室温「快適」帯でのON: {fmt_h(comfort_cool)}"
            f"（{comfort_cool / total_cool * 100:.0f}%）" if total_cool else "",
            ""]
    return out


# ---------- 3. 快適帯で冷房ONの内訳 ----------

def section_comfort_detail(rows: list[dict], step: float) -> list[str]:
    sel = [r for r in rows if r["cooling"] and tier(r["room"], ROOM_THRESHOLDS) == COMFORT]
    out = ["## 3. 快適帯で冷房ONだった時間の内訳", ""]
    if not sel:
        return out + ["該当なし", ""]
    temps = sorted(r["room"] for r in sel)
    q = statistics.quantiles(temps, n=10) if len(temps) >= 10 else temps
    out += [f"- 合計 {fmt_h(len(sel) * step)}",
            f"- 室温: 最低 {temps[0]:.1f} / 10%点 {q[0]:.1f} / 中央 {statistics.median(temps):.1f} / 最高 {temps[-1]:.1f}",
            "", "時間帯別（1日の中でいつ無駄が出ているか）:", "",
            "| 時 | 快適帯でON |", "|---|---|"]
    by_hour = defaultdict(float)
    for r in sel:
        by_hour[r["t"].hour] += step
    for h in range(24):
        if by_hour[h]:
            out.append(f"| {h:02d} | {fmt_h(by_hour[h])} |")
    settemp = defaultdict(int)
    for r in sel:
        settemp[r["set_temp"]] += 1
    out += ["", "設定温度の内訳: " + ", ".join(f"{k}℃×{v}" for k, v in sorted(settemp.items(), key=lambda x: (x[0] is None, x[0]))), ""]
    return out


# ---------- 4. 停止後の室温戻り時間 ----------

def section_recovery(rows: list[dict]) -> list[str]:
    """冷房ON→OFF に切り替わった地点から、室温が目標に達するまでの分数。"""
    targets = [25.0, 25.7]
    results = {t: defaultdict(list) for t in targets}
    censored = defaultdict(int)  # 冷房再開まで戻らなかった回数
    for i in range(1, len(rows)):
        if not (rows[i - 1]["cooling"] and not rows[i]["cooling"]):
            continue
        start = rows[i]
        if start["room"] >= 25.0:
            continue
        o = tier(start["outdoor"], OUT_THRESHOLDS)
        reached = {t: None for t in targets}
        for j in range(i, len(rows)):
            r = rows[j]
            if r["cooling"]:
                break
            m = (r["t"] - start["t"]).total_seconds() / 60
            if m > 24 * 60:
                break
            for t in targets:
                if reached[t] is None and r["room"] >= t:
                    reached[t] = m
        for t in targets:
            if reached[t] is not None:
                results[t][o].append(reached[t])
            else:
                censored[(t, o)] += 1
    out = ["## 4. 冷房停止後、室温が戻るまでの時間", "",
           "停止した時点の外気帯ごと。「未到達」は戻る前に冷房が再開した／データが切れた回数。", "",
           "| 外気 | 目標 | 回数 | 中央値 | 最短 | 最長 | 未到達 |", "|---|---|---|---|---|---|---|"]
    for o in range(3):
        for t in targets:
            v = results[t][o]
            if v:
                out.append(f"| {OUT_LABELS[o]} | {t}℃ | {len(v)} | {statistics.median(v):.0f}分 | {min(v):.0f}分 | {max(v):.0f}分 | {censored[(t, o)]} |")
            else:
                out.append(f"| {OUT_LABELS[o]} | {t}℃ | 0 | - | - | - | {censored[(t, o)]} |")
    out.append("")
    return out


# ---------- 5. 簡易熱モデル + サーモスタット・シミュレーション ----------

def fit_rate(rows: list[dict], cooling: bool) -> tuple[float, float, int]:
    """dRoom/dt(℃/h) = a*(outdoor-room) + b を最小二乗で当てはめる。"""
    xs, ys = [], []
    for a, b in zip(rows, rows[1:]):
        if a["cooling"] != cooling or b["cooling"] != cooling:
            continue
        dt = (b["t"] - a["t"]).total_seconds() / 3600
        if not (0 < dt < 1):
            continue
        xs.append(a["outdoor"] - a["room"])
        ys.append((b["room"] - a["room"]) / dt)
    n = len(xs)
    if n < 20:
        return 0.0, 0.0, n
    mx, my = statistics.mean(xs), statistics.mean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    a = sxy / sxx if sxx else 0.0
    return a, my - a * mx, n


def simulate(rows: list[dict], off_at: float, on_at: float,
             rate_off: tuple[float, float], rate_on: tuple[float, float]) -> tuple[float, int]:
    """外気系列はデータ通り、室温はモデルで進める。ON時間(分)と切替回数を返す。"""
    room = rows[0]["room"]
    cooling = rows[0]["cooling"]
    on_min, switches = 0.0, 0
    for a, b in zip(rows, rows[1:]):
        dt_h = (b["t"] - a["t"]).total_seconds() / 3600
        if not (0 < dt_h < 1):
            room = b["room"]
            continue
        # 暑い側（室温≥27）は現状マトリクスが冷やしているので、そこは常にON扱い
        if room >= on_at and not cooling:
            cooling, switches = True, switches + 1
        elif room < off_at and cooling:
            cooling, switches = False, switches + 1
        a_, b_ = rate_on if cooling else rate_off
        room += (a_ * (a["outdoor"] - room) + b_) * dt_h
        if cooling:
            on_min += dt_h * 60
    return on_min, switches


def section_simulation(rows: list[dict], step: float) -> list[str]:
    a_off, b_off, n_off = fit_rate(rows, cooling=False)
    a_on, b_on, n_on = fit_rate(rows, cooling=True)
    out = ["## 5. サーモスタット方式のシミュレーション", "",
           "室温の変化を `dRoom/dt = a×(外気−室温) + b` で近似（℃/時）。",
           "ざっくりした目安で、窓の開閉や在室は考慮していない。", "",
           f"- 冷房OFF時: a={a_off:.3f}, b={b_off:.2f}（{n_off}サンプル）",
           f"- 冷房ON時 : a={a_on:.3f}, b={b_on:.2f}（{n_on}サンプル）", ""]
    if n_off < 20 or n_on < 20:
        return out + ["サンプル不足のためシミュレーションは省略。", ""]
    actual_on = sum(step for r in rows if r["cooling"])
    out += ["現状（実測）の冷房ON: " + fmt_h(actual_on), "",
            "| 停止しきい値 | 再開しきい値 | 冷房ON | 削減 | 切替回数 | 切替/日 |",
            "|---|---|---|---|---|---|"]
    days = max((rows[-1]["t"] - rows[0]["t"]).total_seconds() / 86400, 0.01)
    for off_at in (25.0, 24.5, 24.0, 23.5):
        for hyst in (0.7, 1.5):
            on_at = off_at + hyst
            on_min, sw = simulate(rows, off_at, on_at, (a_off, b_off), (a_on, b_on))
            red = (1 - on_min / actual_on) * 100 if actual_on else 0
            out.append(f"| {off_at}℃ | {on_at}℃ | {fmt_h(on_min)} | {red:+.0f}% | {sw} | {sw / days:.1f} |")
    out += ["", "切替/日が多すぎる（目安 >10）と機器に負担なので、再開しきい値の幅を広げる方向で調整する。", ""]
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=os.environ.get("DB_PATH", "data/aircon.db"))
    p.add_argument("--days", type=float, default=None, help="直近N日だけ（既定: 全期間）")
    args = p.parse_args()

    rows = load(args.db, args.days)
    if len(rows) < 10:
        print(f"データが少なすぎます: {len(rows)} 行", file=sys.stderr)
        sys.exit(1)
    gaps = [(b["t"] - a["t"]).total_seconds() / 60 for a, b in zip(rows, rows[1:])]
    step = statistics.median([g for g in gaps if 0 < g < 120]) or 10.0

    lines = ["# readings 分析", f"生成: {datetime.now().isoformat(timespec='minutes')} / DB: {args.db}", ""]
    lines += section_overview(rows)
    lines += section_matrix(rows, step)
    lines += section_comfort_detail(rows, step)
    lines += section_recovery(rows)
    lines += section_simulation(rows, step)
    print("\n".join(lines))


if __name__ == "__main__":
    main()
