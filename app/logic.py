"""制御判定ロジック。

外気温と室温をそれぞれ「温度帯（tier）」に分け、その組み合わせで
目標温度・風量・電源を決める。温度帯の切り替えにはヒステリシス
（一定幅を超えて動かないと帯が変わらない仕組み）を入れて、
境界付近で設定が行ったり来たりするのを防ぐ。

さらに、外気が十分涼しいときは冷房を止めて送風で済ませる
「外気冷却モード(free cooling)」を、マトリクス判定の後段の上書きとして
適用する。送風が使えるかどうかは機種依存なので、ここでは判断せず
controller/remo 側で「送風非対応なら電源オフ」に落とす。
"""
from dataclasses import dataclass

# 外気温の帯: 0=猛暑(>=33) 1=夏日(>=28) 2=涼しい
OUT_THRESHOLDS = [33.0, 28.0]
# 室温の帯: 0=暑い(>=27) 1=ぬるい(>=25) 2=快適
ROOM_THRESHOLDS = [27.0, 25.0]

OUT_LABELS = ["猛暑", "夏日", "涼しい"]
ROOM_LABELS = ["暑い", "ぬるい", "快適"]

# 室温の「快適」帯（この帯では送風にせず従来どおり電源オフ）
COMFORT_ROOM_TIER = len(ROOM_THRESHOLDS)

# 外気冷却モードで使う運転モード名（Nature Remo の標準名）
BLOW_MODE = "blow"


@dataclass
class Decision:
    power: str            # "on" / "off"
    target_temp: float | None
    volume_pref: str | None   # "auto"(エアコン任せ) / "max"(最強) / "min"(弱め)
    out_tier: int
    room_tier: int
    reason: str
    mode: str | None = "cool"      # "cool" / "blow" / None(=電源オフ)
    free_cool: bool = False        # 外気冷却モードで動いているか
    free_cool_abort: bool = False  # 室温上昇で冷房へ復帰した回か（ロックアウト開始の合図）


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


def free_cool_out_ok(
    outdoor: float, out_max: float, last_free_cool: bool, hyst: float
) -> bool:
    """外気温が外気冷却モードの条件を満たすか（ヒステリシス付き）。

    tier_with_hysteresis と同じ思想で、入るときと出るときのしきい値をずらす。
    既定値(out_max=25.0, hyst=0.7)なら「入るには ≤25.0、出るには >25.7」。
    """
    limit = out_max + hyst if last_free_cool else out_max
    return outdoor <= limit


# (外気帯, 室温帯) → (電源, 目標温度, 風量)
MATRIX: dict[tuple[int, int], tuple[str, float | None, str | None]] = {
    (0, 0): ("on", 19.0, "max"),    # 猛暑×暑い: ここだけ最強風量で一気に冷やす
    (0, 1): ("on", 23.0, "auto"),
    (0, 2): ("on", 26.0, "auto"),
    (1, 0): ("on", 22.0, "auto"),
    (1, 1): ("on", 24.0, "auto"),
    (1, 2): ("on", 26.0, "auto"),
    (2, 0): ("on", 27.0, "auto"),   # 外が涼しくても、まだ暑いなら風量は絞らない
    (2, 1): ("on", 26.0, "auto"),
    (2, 2): ("off", None, None),    # 外も中も涼しければ停止
}


def decide(
    outdoor: float,
    room: float,
    last_out_tier: int | None,
    last_room_tier: int | None,
    hyst: float,
    humidity: float | None = None,
    last_free_cool: bool = False,
    lockout_active: bool = False,
    free_cool_enabled: bool = True,
    free_cool_out_max: float = 25.0,
    free_cool_humid_max: float = 70.0,
    free_cool_abort_room: float = 30.5,
    free_cool_start_room: float | None = None,
    free_cool_rise_min: float = 0.5,
    cool_out_hot_target: float | None = None,
) -> Decision:
    """外気温・室温・湿度から運転内容を決める（純粋関数）。

    設定値は呼び出し側（controller）から渡す。外気冷却モードの判定には
    前回それが有効だったか(last_free_cool)と、復帰直後のロックアウト中か
    (lockout_active)を使う。

    冷房への復帰は「室温が free_cool_abort_room 以上」かつ「送風を始めた
    ときの室温(free_cool_start_room)より free_cool_rise_min 以上上がった」
    ときだけ行う。室温が高くても横ばい〜下降なら、外が涼しい限り送風のまま。
    free_cool_start_room が None（旧状態・情報なし）なら絶対値だけで判定する。

    cool_out_hot_target を渡すと (涼しい×暑い) の冷房目標温度を上書きする。
    """
    out_tier = tier_with_hysteresis(outdoor, OUT_THRESHOLDS, last_out_tier, hyst)
    room_tier = tier_with_hysteresis(room, ROOM_THRESHOLDS, last_room_tier, hyst)
    power, temp, vol = MATRIX[(out_tier, room_tier)]
    if cool_out_hot_target is not None and (out_tier, room_tier) == (2, 0):
        temp = cool_out_hot_target
    mode = "cool" if power == "on" else None
    reason = f"外気{OUT_LABELS[out_tier]}({outdoor:.1f}℃) × 室内{ROOM_LABELS[room_tier]}({room:.1f}℃)"

    free_cool = False
    free_cool_abort = False

    if free_cool_enabled:
        # 湿度が取れないときは条件を満たすものとして扱う
        humid_ok = humidity is None or humidity <= free_cool_humid_max
        too_hot = room >= free_cool_abort_room
        risen = (
            free_cool_start_room is None
            or room >= free_cool_start_room + free_cool_rise_min
        )
        out_ok = free_cool_out_ok(outdoor, free_cool_out_max, last_free_cool, hyst)
        # 新規に送風へ入るときは復帰しきい値未満であること。
        # すでに送風中なら、室温が高くても「上がっていない」限り続ける。
        room_ok = (not too_hot) or last_free_cool

        if last_free_cool and too_hot and risen:
            # 室温が上がり続けているので冷房へ復帰。以後しばらくは再突入しない
            free_cool_abort = True
            reason += f" → 室温{room:.1f}℃まで上昇したため冷房に復帰"
        elif power == "on" and out_ok and humid_ok and room_ok and not lockout_active:
            if room_tier == COMFORT_ROOM_TIER:
                # 室内も快適な帯なら送風すら不要（従来どおり電源オフ）
                power, temp, vol, mode = "off", None, None, None
                reason += " → 外気も室内も涼しいので停止"
            else:
                power, temp, vol, mode = "on", None, "min", BLOW_MODE
                free_cool = True
                reason += " → 外気が涼しいので冷房不要"

    return Decision(
        power=power,
        target_temp=temp,
        volume_pref=vol,
        out_tier=out_tier,
        room_tier=room_tier,
        reason=reason,
        mode=mode,
        free_cool=free_cool,
        free_cool_abort=free_cool_abort,
    )
