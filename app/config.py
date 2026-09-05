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


def _b(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


# --- 必須 ---
NATURE_ACCESS_TOKEN = os.environ.get("NATURE_ACCESS_TOKEN", "")

# --- 任意 ---
# 自宅の座標（Open-Meteo で外気温を取得。既定は東京駅）
LATITUDE = _f("LATITUDE", 35.6812)
LONGITUDE = _f("LONGITUDE", 139.7671)
# 複数エアコンがある場合に対象を固定したいとき（未指定なら最初の AIRCON）
APPLIANCE_ID = os.environ.get("APPLIANCE_ID", "")

CONTROL_INTERVAL_MIN = _i("CONTROL_INTERVAL_MIN", 10)   # 制御ループの間隔
MIN_HOLD_MIN = _i("MIN_HOLD_MIN", 10)                   # 一度操作したら最低これだけ維持
                                                        # (CONTROL_INTERVAL_MIN より長いと毎回は調整できない)
HYSTERESIS = _f("HYSTERESIS", 0.7)                      # 温度帯の切替に必要な余裕(℃)
ROOM_TEMP_OFFSET = _f("ROOM_TEMP_OFFSET", 0.0)          # Remoセンサー補正(自己発熱なら -1.0 など)

# --- 外気冷却モード（free cooling） ---
# 外気が十分涼しいときは冷房をやめて送風に切り替える（送風非対応機なら電源オフ）。
FREE_COOL_ENABLED = _b("FREE_COOL_ENABLED", True)
FREE_COOL_OUT_MAX = _f("FREE_COOL_OUT_MAX", 25.0)       # 突入できる外気温の上限(℃)
FREE_COOL_HUMID_MAX = _f("FREE_COOL_HUMID_MAX", 70.0)   # 突入できる湿度の上限(%)
FREE_COOL_ABORT_ROOM = _f("FREE_COOL_ABORT_ROOM", 30.5) # この室温以上なら冷房へ復帰(℃)
FREE_COOL_RISE_MIN = _f("FREE_COOL_RISE_MIN", 0.5)      # 復帰には送風開始時からこれだけ上昇が必要(℃)
FREE_COOL_LOCKOUT_MIN = _i("FREE_COOL_LOCKOUT_MIN", 45) # 復帰後この時間は再突入しない(分)

# 外気が涼しい(<28℃)のに室温が暑い(≥27℃)ときの冷房目標温度(℃)。
# 外が涼しければ軽く冷やすだけでよく、25℃だと全力運転でエアコン近くが寒くなる
# ROOM_TARGET_ENABLED=true のときは室温追従が優先されるため使われない。
COOL_OUT_HOT_TARGET = _f("COOL_OUT_HOT_TARGET", 27.0)

# --- 室温追従モード（room target） ---
# 「設定温度」ではなく「室温そのもの」を目標帯に入れる。設定温度は結果を見て
# 上下させる。エアコンの設定温度と実際の室温には部屋ごとにずれがあるため、
# 目標値を決め打ちせず実測から追い込む。
ROOM_TARGET_ENABLED = _b("ROOM_TARGET_ENABLED", True)
ROOM_TARGET_LOW = _f("ROOM_TARGET_LOW", 24.0)           # この室温を下回ったら冷房を止める(℃)
ROOM_TARGET_HIGH = _f("ROOM_TARGET_HIGH", 25.5)         # この室温を上回ったら冷房する(℃)
# 目標をどれだけ超えているかに対して、設定温度を何倍下げるか。
# 1.0 なら「3℃オーバーで設定を3℃下げる」。大きいほど速いが行き過ぎやすい
ROOM_TARGET_GAIN = _f("ROOM_TARGET_GAIN", 1.5)
ROOM_TARGET_SET_MIN = _f("ROOM_TARGET_SET_MIN", 16.0)   # 設定温度の下限(℃)
ROOM_TARGET_SET_MAX = _f("ROOM_TARGET_SET_MAX", 30.0)   # 設定温度の上限(℃)
# 目標帯にどれだけ近づいたら設定温度を動かさないか(℃)。
# 毎回動かすと制御が振動するので、この幅に入っていれば据え置く
ROOM_TARGET_DEADBAND = _f("ROOM_TARGET_DEADBAND", 0.3)
# 目標をこれ以上超えていたら風量を最強にする(℃)。
# 弱い冷気で長く回すより、能力を出しきって早く目標へ入れる
ROOM_TARGET_MAX_VOL_OVER = _f("ROOM_TARGET_MAX_VOL_OVER", 1.0)

# 室温が目標帯を下回ったときに暖房するのは、外気温がこの値より低いときだけ(℃)。
# 夏の朝など「たまたま室温が帯を下回った」だけで暖房しないためのガード
HEAT_OUT_MAX = _f("HEAT_OUT_MAX", 20.0)

DB_PATH = os.environ.get("DB_PATH", str(BASE_DIR / "data" / "aircon.db"))
HOST = os.environ.get("HOST", "0.0.0.0")
PORT = _i("PORT", 8000)

HTTP_TIMEOUT = _f("HTTP_TIMEOUT", 10.0)
