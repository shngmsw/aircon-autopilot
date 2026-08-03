"""SQLite への記録と、制御に必要な状態（前回の帯・最終操作時刻など）の保存。"""
import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import config

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        Path(config.DB_PATH).parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL,
                outdoor REAL,
                room REAL,
                humidity REAL,
                set_temp REAL,
                air_volume TEXT,
                power TEXT,
                auto INTEGER,
                action TEXT,
                note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts);
            CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT);
            """
        )
    return _conn


def now_jst() -> datetime:
    return datetime.now(config.JST)


# ---------- state (key-value) ----------

def get_state(key: str, default=None):
    with _lock:
        row = _connect().execute("SELECT v FROM state WHERE k=?", (key,)).fetchone()
    if row is None:
        return default
    return json.loads(row["v"])


def set_state(key: str, value) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            "INSERT INTO state(k, v) VALUES(?, ?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (key, json.dumps(value)),
        )
        conn.commit()


_VALID_AUTO_STATES = ("on", "off", "paused_external")


def auto_state() -> str:
    """自動制御の3値状態を返す（"on" / "off" / "paused_external"）。

    既存DBの `auto_enabled`（bool）とも後方互換を保つ:
    - "auto_state" が保存されていればそれを使う
    - 無ければ旧 "auto_enabled" の値から読み替える（True→"on", False→"off"）
    """
    raw = get_state("auto_state")
    if raw in _VALID_AUTO_STATES:
        return raw
    return "on" if bool(get_state("auto_enabled", True)) else "off"


def set_auto_state(state: str) -> None:
    if state not in _VALID_AUTO_STATES:
        raise ValueError(f"invalid auto_state: {state!r}")
    set_state("auto_state", state)
    # 旧フィールドとの互換も維持しておく
    set_state("auto_enabled", state == "on")


def auto_enabled() -> bool:
    return auto_state() == "on"


# ---------- readings ----------

def add_reading(
    *,
    outdoor: float | None,
    room: float | None,
    humidity: float | None,
    set_temp: float | None,
    air_volume: str | None,
    power: str | None,
    auto: bool,
    action: str,
    note: str = "",
) -> None:
    with _lock:
        conn = _connect()
        conn.execute(
            """INSERT INTO readings
               (ts, outdoor, room, humidity, set_temp, air_volume, power, auto, action, note)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                now_jst().isoformat(timespec="seconds"),
                outdoor, room, humidity, set_temp, air_volume, power,
                1 if auto else 0, action, note,
            ),
        )
        conn.commit()


def get_history(hours: float) -> list[dict]:
    since = (now_jst() - timedelta(hours=hours)).isoformat(timespec="seconds")
    with _lock:
        rows = _connect().execute(
            "SELECT * FROM readings WHERE ts >= ? ORDER BY ts ASC", (since,)
        ).fetchall()
    return [dict(r) for r in rows]


def latest_reading() -> dict | None:
    with _lock:
        row = _connect().execute(
            "SELECT * FROM readings ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None
