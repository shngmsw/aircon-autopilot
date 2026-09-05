"""室温追従モード（app/logic.room_target_setpoint と decide）のテスト。

設定温度ではなく室温そのものを目標帯に入れる。設定温度と室温のずれは
部屋ごとに違うので、実測を見て設定を上下させて追い込む。
"""
from app import logic

LOW, HIGH = 24.0, 25.5
GAIN, SET_MIN, SET_MAX, DEAD = 1.5, 18.0, 30.0, 0.3


def setpoint(room, current_set):
    return logic.room_target_setpoint(
        room, current_set, LOW, HIGH, GAIN, SET_MIN, SET_MAX, DEAD
    )[:3]


def test_ずれが大きいほど大きく下げる():
    # 2.3℃オーバー × gain1.5 = 3.45 → 3℃下げる
    power, temp, _ = setpoint(room=27.8, current_set=25.0)
    assert power == "on"
    assert temp == 22.0


def test_ずれが小さければ下げ幅も小さい():
    # 0.5℃オーバー × gain1.5 = 0.75 だが、最低1℃は動かす
    power, temp, _ = setpoint(room=26.0, current_set=24.0)
    assert (power, temp) == ("on", 23.0)


def test_下げても届かなければさらに下げる():
    # 1.5℃オーバー × gain1.5 = 2.25 → 24 から 22℃ へ
    power, temp, _ = setpoint(room=27.0, current_set=24.0)
    assert (power, temp) == ("on", 22.0)


def test_設定温度は下限で止まる():
    power, temp, reason = setpoint(room=28.0, current_set=18.0)
    assert (power, temp) == ("on", 18.0)
    assert "下限" in reason


def test_室温が目標を下回ったら停止する():
    power, temp, _ = setpoint(room=23.5, current_set=20.0)
    assert power == "off"
    assert temp is None


def test_目標帯の中なら設定を動かさない():
    power, temp, _ = setpoint(room=24.5, current_set=22.0)
    assert (power, temp) == ("on", 22.0)


def test_上端付近でも設定を動かさない():
    # 25.3℃ は high(25.5) との差が deadband(0.3) 以内
    power, temp, _ = setpoint(room=25.3, current_set=22.0)
    assert (power, temp) == ("on", 22.0)


def test_上限ちょうどなら下げにいかない():
    # 25.5℃ は目標帯の内側。そこから 25℃ まで追い込む必要はない
    power, temp, _ = setpoint(room=HIGH, current_set=24.0)
    assert (power, temp) == ("on", 24.0)


def test_設定温度が不明なら目標上限から始める():
    # 25.5 を起点に、1.5℃オーバー × gain1.5 = 2℃下げる
    power, temp, _ = setpoint(room=27.0, current_set=None)
    assert (power, temp) == ("on", 23.5)


def setpoint4(room, current_set, **kw):
    return logic.room_target_setpoint(
        room, current_set, LOW, HIGH, GAIN, SET_MIN, SET_MAX, DEAD, **kw
    )


# --- 暖房 ---

def test_外気が冷たく目標を下回れば暖房する():
    # 2.0℃不足 × gain1.5 = 3 → low(24) + 3 = 27℃
    power, temp, _, mode = setpoint4(room=22.0, current_set=None, heat_allowed=True)
    assert (power, temp, mode) == ("on", 27.0, "warm")


def test_暖房を許可しなければ従来どおり停止する():
    power, temp, _, mode = setpoint4(room=22.0, current_set=None, heat_allowed=False)
    assert (power, temp, mode) == ("off", None, None)


def test_暖房中に上端を超えたら一旦停止する():
    # 冬の日射などで急上昇。冷房に直行せず一旦オフ（次回判定で冷房を検討）
    power, temp, reason, mode = setpoint4(
        room=27.0, current_set=26.0, heat_allowed=True, heating_now=True
    )
    assert (power, temp, mode) == ("off", None, None)
    assert "超えたので暖房を停止" in reason


def test_暖房の設定温度は上限で止まる():
    power, temp, _, mode = setpoint4(
        room=18.0, current_set=29.0, heat_allowed=True, heating_now=True
    )
    assert (power, temp, mode) == ("on", 30.0, "warm")


def test_暖房は帯に少し入ってから切る():
    # 暖房オフのしきい値は min(low + hyst, high) = min(24.7, 25.5) = 24.7℃
    power, temp, _, mode = setpoint4(
        room=24.3, current_set=26.0, heat_allowed=True, heating_now=True
    )
    assert (power, temp, mode) == ("on", 26.0, "warm")   # 24.3 < 24.7 → 継続

    power, temp, _, mode = setpoint4(
        room=24.8, current_set=26.0, heat_allowed=True, heating_now=True
    )
    assert (power, temp, mode) == ("off", None, None)     # 24.8 ≥ 24.7 → 停止


def test_帯が狭くても暖房オフのしきい値は上端を超えない():
    # low=21.0, high=21.5 だと low + hyst(0.7) = 21.7 が high を超えるので 21.5 で切る
    power, _, _, mode = logic.room_target_setpoint(
        21.6, 23.0, 21.0, 21.5, GAIN, SET_MIN, SET_MAX, DEAD,
        heat_allowed=True, heating_now=True,
    )
    assert (power, mode) == ("off", None)


def test_冬は帯内で停止中なら停止のまま():
    # 外気が冷たい(heat_allowed)とき、帯内で冷房を始めたりしない
    power, temp, _, mode = setpoint4(room=24.5, current_set=None, heat_allowed=True)
    assert (power, temp, mode) == ("off", None, None)


def test_冷房運転中の帯内維持は従来どおり():
    # 冷房で運転中（current_set あり・暖房中でない）なら、外気が冷たくても帯内は維持
    power, temp, _, mode = setpoint4(room=24.5, current_set=22.0, heat_allowed=True)
    assert (power, temp, mode) == ("on", 22.0, "cool")


# --- decide への組み込み ---

def call(outdoor, room, current_set=None, **kw):
    kw.setdefault("free_cool_enabled", False)
    return logic.decide(
        outdoor, room, None, None, 0.7,
        room_target_enabled=True,
        room_target_low=LOW, room_target_high=HIGH,
        room_target_gain=GAIN, room_target_set_min=SET_MIN,
        room_target_set_max=SET_MAX, room_target_deadband=DEAD,
        room_target_max_vol_over=1.0,
        current_set_temp=current_set, **kw,
    )


def test_目標室温は外気温で変わらない():
    # 同じ室温なら、外が猛暑でも涼しくても同じ設定温度になる
    hot = call(outdoor=35.0, room=27.0, current_set=25.0)
    cool = call(outdoor=20.0, room=27.0, current_set=25.0)
    assert hot.target_temp == cool.target_temp == 23.0


def test_室温追従では風量を絞らない():
    # 目標超過が大きければ max、近ければ auto。どちらにせよ min にはしない
    for room in (27.0, 25.5, 24.5):
        d = call(outdoor=30.0, room=room, current_set=25.0)
        assert d.volume_pref in ("auto", "max")


def test_目標を下回れば外気が暑くても停止する():
    d = call(outdoor=35.0, room=23.0, current_set=20.0)
    assert d.power == "off"


def test_外気が涼しく室温が目標帯なら送風を使う():
    d = call(outdoor=22.0, room=24.6, current_set=24.0,
             free_cool_enabled=True, humidity=50)
    assert d.free_cool is True
    assert d.mode == "blow"


def test_外気が涼しくても室温が目標を下回れば停止():
    d = call(outdoor=22.0, room=23.0, current_set=24.0,
             free_cool_enabled=True, humidity=50)
    assert d.power == "off"
    assert d.free_cool is False


# --- 風量 ---

def test_目標から大きく外れていれば風量を最強にする():
    # 29.3℃ は目標上限25.5℃を3.8℃超過
    d = call(outdoor=33.0, room=29.3, current_set=18.0)
    assert d.volume_pref == "max"


def test_目標帯に近づいたら風量をautoに戻す():
    # 25.5℃ の超過は0.5℃で、しきい値1.0℃未満
    d = call(outdoor=33.0, room=25.5, current_set=20.0)
    assert d.volume_pref == "auto"


def test_目標帯の中なら風量はauto():
    d = call(outdoor=33.0, room=24.5, current_set=20.0)
    assert d.volume_pref == "auto"
