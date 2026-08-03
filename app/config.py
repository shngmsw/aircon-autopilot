"""環境変数から設定を読み込む。すべて .env で上書き可能。"""
import os
from datetime import timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

JST = timezone(timedelta(hours=9), name="JST")

BASE_DIR = Path(__file__).resolve().parent.parent


def _f(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _i(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


# --- 必須 ---
NATURE_ACCESS_TOKEN = os.environ.get("NATURE_ACCESS_TOKEN", "")

# --- 任意 ---
# アメダス観測所ID（既定: 44132 = 東京。README に主要IDの調べ方あり）
AMEDAS_STATION = os.environ.get("AMEDAS_STATION", "44132")
# 複数エアコンがある場合に対象を固定したいとき（未指定なら最初の AIRCON）
APPLIANCE_ID = os.environ.get("APPLIANCE_ID", "")

CONTROL_INTERVAL_MIN = _i("CONTROL_INTERVAL_MIN", 10)   # 制御ループの間隔
MIN_HOLD_MIN = _i("MIN_HOLD_MIN", 15)                   # 一度操作したら最低これだけ維持
HYSTERESIS = _f("HYSTERESIS", 0.7)                      # 温度帯の切替に必要な余裕(℃)
ROOM_TEMP_OFFSET = _f("ROOM_TEMP_OFFSET", 0.0)          # Remoセンサー補正(自己発熱なら -1.0 など)

DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "data" / "aircon.db"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = _i("PORT", 8000)

HTTP_TIMEOUT = _f("HTTP_TIMEOUT", 10.0)
