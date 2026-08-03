"""制御判定ロジック。

外気温と室温をそれぞれ「温度帯（tier）」に分け、その組み合わせで
目標温度・風量・電源を決める。温度帯の切り替えにはヒステリシス
（一定幅を超えて動かないと帯が変わらない仕組み）を入れて、
境界付近で設定が行ったり来たりするのを防ぐ。
"""
from dataclasses import dataclass

# 外気温の帯: 0=猛暑(>=33) 1=夏日(>=28) 2=涼しい
OUT_THRESHOLDS = [33.0, 28.0]
# 室温の帯: 0=暑い(>=27) 1=ぬるい(>=25) 2=快適
ROOM_THRESHOLDS = [27.0, 25.0]

OUT_LABELS = ["猛暑", "夏日", "涼しい"]
ROOM_LABELS = ["暑い", "ぬるい", "快適"]


@dataclass
class Decision:
    power: str            # "on" / "off"
    target_temp: float | None
    volume_pref: str | None   # "auto"(強め) / "min"(弱め)
    out_tier: int
    room_tier: int
    reason: str


def _raw_tier(value: float, thresholds: list[float]) -> int:
    for i, t in enumerate(thresholds):
        if value >= t:
            return i
    return len(thresholds)


def tier_with_hysteresis(
    value: float, thresholds: list[float], last: int | None, hyst: float
) -> int:
    """前回の帯を考慮して帯を決める。

    帯を変えるには、境界を hyst ℃ぶん超えて動く必要がある。
    """
    if last is None:
        return _raw_tier(value, thresholds)
    adjusted = []
    for i, t in enumerate(thresholds):
        # 前回が境界より上(暑い側, last<=i)なら、出るには t-hyst を下回る必要がある
        # 前回が境界より下(涼しい側)なら、入るには t+hyst を上回る必要がある
        adjusted.append(t - hyst if last <= i else t + hyst)
    return _raw_tier(value, adjusted)


# (外気帯, 室温帯) → (電源, 目標温度, 風量)
MATRIX: dict[tuple[int, int], tuple[str, float | None, str | None]] = {
    (0, 0): ("on", 19.0, "auto"),   # 猛暑×暑い: 最大パワーで一気に冷やす
    (0, 1): ("on", 23.0, "auto"),
    (0, 2): ("on", 26.0, "min"),    # 冷えたら維持運転
    (1, 0): ("on", 22.0, "auto"),
    (1, 1): ("on", 24.0, "auto"),
    (1, 2): ("on", 26.0, "min"),
    (2, 0): ("on", 25.0, "min"),
    (2, 1): ("on", 26.0, "min"),
    (2, 2): ("off", None, None),    # 外も中も涼しければ停止
}


def decide(
    outdoor: float,
    room: float,
    last_out_tier: int | None,
    last_room_tier: int | None,
    hyst: float,
) -> Decision:
    out_tier = tier_with_hysteresis(outdoor, OUT_THRESHOLDS, last_out_tier, hyst)
    room_tier = tier_with_hysteresis(room, ROOM_THRESHOLDS, last_room_tier, hyst)
    power, temp, vol = MATRIX[(out_tier, room_tier)]
    reason = f"外気{OUT_LABELS[out_tier]}({outdoor:.1f}℃) × 室内{ROOM_LABELS[room_tier]}({room:.1f}℃)"
    return Decision(
        power=power,
        target_temp=temp,
        volume_pref=vol,
        out_tier=out_tier,
        room_tier=room_tier,
        reason=reason,
    )
