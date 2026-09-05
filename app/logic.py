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

# 暖房で使う運転モード名（Nature Remo の標準名）
WARM_MODE = "warm"


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


def room_target_setpoint(
    room: float,
    current_set: float | None,
    low: float,
    high: float,
    gain: float,
    set_min: float,
    set_max: float,
    deadband: float,
    heat_allowed: bool = False,
    heating_now: bool = False,
    hyst: float = 0.7,
) -> tuple[str, float | None, str, str | None]:
    """室温を low〜high に収める (電源, 設定温度, 理由, 運転モード) を返す。

    エアコンの設定温度と実際の室温にはずれがあり、その幅は部屋や季節で
    変わる。そこで目標値を決め打ちせず、室温を見て設定温度を上下させる。
    動かす幅は「目標からどれだけずれているか」に比例させる（gain 倍）。
    1℃ずつ様子見だと目標に届くまで何時間もかかるため、大きくずれている
    ときは一気に動かす。

    - 室温が high を超えている → 冷房。超過分×gain だけ設定を下げる（set_min まで）
    - 室温が low を下回った    → heat_allowed なら暖房。不足分×gain だけ設定を
      上げる（set_max まで）。heat_allowed でなければ従来どおり停止
    - 目標帯の中               → 冷房運転中なら現状維持。暖房中は
      room ≥ min(low + hyst, high) で電源オフ（low ちょうどでの振動を防ぐため
      帯に少し入ってから切る。帯が hyst より狭くても上端は超えない）。
      停止中で外気も冷たい(heat_allowed)なら停止のまま

    current_set が None（今オフなど）のときは、冷房は high・暖房は low を
    初期値として始める。heating_now は「いま暖房で運転中か」（controller が
    実機の状態から渡す）。
    """
    heat_off = min(low + hyst, high)

    if heating_now and room >= heat_off:
        # 暖房中に帯へ入った（room > high の場合を含む）。ここで暖房終了。
        # 冷房が要るかどうかは次回の呼び出しで room > high から改めて判断する
        return "off", None, (
            f"室温{room:.1f}℃が目標帯({low:.1f}〜{high:.1f}℃)に入ったので暖房を停止"
        ), None

    if room > high:
        # まだ暑い。冷房で下げる。暖房中だった場合の設定温度は基準にならない
        base = current_set if (current_set is not None and not heating_now) else high
        over = room - high
        drop = max(1.0, round(over * gain))
        want = max(set_min, base - drop)
        if want >= base:
            return "on", set_min, (
                f"室温{room:.1f}℃が目標{high:.1f}℃を超えているが"
                f"設定は下限{set_min:.0f}℃。これ以上下げられない"
            ), "cool"
        return "on", want, (
            f"室温{room:.1f}℃が目標{high:.1f}℃を{over:.1f}℃超過"
            f" → 設定を{base:.0f}℃から{want:.0f}℃へ下げる"
        ), "cool"

    if room < low or heating_now:
        # 寒い側（heating_now ならここに来る時点で room < heat_off が確定している）。
        # 外気が暖かいのに室温が低いだけなら暖房はしない
        # （夏の朝の誤暖房防止。ガードは呼び出し側が heat_allowed で判断）
        if not heat_allowed:
            return "off", None, f"室温{room:.1f}℃は目標{low:.1f}℃を下回るので停止", None
        base = current_set if (heating_now and current_set is not None) else low
        under = low - room
        if under > 0:
            raise_by = max(1.0, round(under * gain))
            want = min(set_max, base + raise_by)
            if want <= base:
                return "on", set_max, (
                    f"室温{room:.1f}℃が目標{low:.1f}℃を下回るが"
                    f"設定は上限{set_max:.0f}℃。これ以上上げられない"
                ), WARM_MODE
            return "on", want, (
                f"室温{room:.1f}℃が目標{low:.1f}℃を{under:.1f}℃不足"
                f" → 設定を{base:.0f}℃から{want:.0f}℃へ上げる"
            ), WARM_MODE
        return "on", base, f"室温{room:.1f}℃は帯の下端付近なので暖房を継続", WARM_MODE

    if current_set is None and heat_allowed:
        # 停止中で外気も冷たい。帯内なら冷房を始める必要はない
        return "off", None, f"室温{room:.1f}℃は目標帯内。外気も冷たいので停止のまま", None

    # 冷房運転中（または夏に停止中）の帯内。上端に近すぎなければ据え置く
    base = current_set if current_set is not None else high
    if high - room <= deadband:
        return "on", base, (
            f"室温{room:.1f}℃は目標帯の上端付近なので設定{base:.0f}℃を維持"
        ), "cool"
    return "on", base, f"室温{room:.1f}℃は目標帯({low:.1f}〜{high:.1f}℃)内", "cool"


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
    room_target_enabled: bool = False,
    room_target_low: float = 24.0,
    room_target_high: float = 25.0,
    room_target_gain: float = 1.5,
    room_target_set_min: float = 18.0,
    room_target_set_max: float = 30.0,
    room_target_deadband: float = 0.3,
    room_target_max_vol_over: float = 1.0,
    current_set_temp: float | None = None,
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

    room_target_enabled=True のときはマトリクスを使わず、室温そのものを
    room_target_low〜high に入れるよう設定温度を上下させる（室温追従モード）。
    目標室温は外気で変えない。外気は「冷房か送風か」の判断にだけ使う。
    """
    out_tier = tier_with_hysteresis(outdoor, OUT_THRESHOLDS, last_out_tier, hyst)
    room_tier = tier_with_hysteresis(room, ROOM_THRESHOLDS, last_room_tier, hyst)

    if room_target_enabled:
        # 室温そのものを目標帯に入れる。設定温度は結果を見て上下させるので、
        # 外気帯は「冷房か送風か」の判断にだけ使う（目標室温は外気で変えない）
        power, temp, reason, _rt_mode = room_target_setpoint(
            room, current_set_temp, room_target_low, room_target_high,
            room_target_gain, room_target_set_min, room_target_set_max,
            room_target_deadband,
        )
        # 目標から大きく外れている間は能力を出しきる。
        # 目標帯に近づいたら auto に戻して静かにする
        if power != "on":
            vol = None
        elif room - room_target_high >= room_target_max_vol_over:
            vol = "max"
        else:
            vol = "auto"
        reason = f"外気{outdoor:.1f}℃ / {reason}"
    else:
        power, temp, vol = MATRIX[(out_tier, room_tier)]
        if cool_out_hot_target is not None and (out_tier, room_tier) == (2, 0):
            temp = cool_out_hot_target
        reason = (
            f"外気{OUT_LABELS[out_tier]}({outdoor:.1f}℃)"
            f" × 室内{ROOM_LABELS[room_tier]}({room:.1f}℃)"
        )
    mode = "cool" if power == "on" else None

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
            # 室温追従では「目標帯を下回ったか」で判断する（そのときは power が
            # すでに off なのでここには来ない）。帯の中なら送風で維持を狙う
            cool_enough = (
                room < room_target_low if room_target_enabled
                else room_tier == COMFORT_ROOM_TIER
            )
            if cool_enough:
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
