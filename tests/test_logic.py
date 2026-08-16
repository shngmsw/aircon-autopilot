"""判定ロジック（app/logic.py）のテスト。

外気冷却モードの突入・離脱・ヒステリシス・ロックアウトと、
従来の温度帯ヒステリシスが壊れていないことを確認する。
"""
from app import logic

HYST = 0.7


def call(
    outdoor,
    room,
    humidity=None,
    last_out_tier=None,
    last_room_tier=None,
    last_free_cool=False,
    lockout_active=False,
    **kw,
):
    """テストを読みやすくするための decide() の薄いラッパー。"""
    return logic.decide(
        outdoor,
        room,
        last_out_tier,
        last_room_tier,
        HYST,
        humidity=humidity,
        last_free_cool=last_free_cool,
        lockout_active=lockout_active,
        **kw,
    )


# ---------- 外気冷却モードの突入 ----------

def test_外気が涼しく室内が暑ければ送風になる():
    d = call(outdoor=23.7, room=27.3, humidity=53)
    assert d.free_cool is True
    assert d.mode == "blow"
    assert d.power == "on"
    assert d.target_temp is None      # 送風は温度指定なし
    assert d.volume_pref == "min"     # 風量は弱め
    assert "外気が涼しいので冷房不要" in d.reason


def test_湿度が取得できないときは条件を満たすものとして扱う():
    d = call(outdoor=23.7, room=27.3, humidity=None)
    assert d.free_cool is True
    assert d.mode == "blow"


def test_外気が閾値を超えていれば従来どおり冷房():
    d = call(outdoor=26.0, room=27.3, humidity=53)
    assert d.free_cool is False
    assert d.mode == "cool"
    assert d.power == "on"
    # (外気=涼しい, 室温=暑い) のマトリクス値がそのまま出る
    assert d.target_temp == 25.0
    assert d.volume_pref == "min"


def test_湿度が高ければ送風にしない():
    d = call(outdoor=23.7, room=27.3, humidity=75)
    assert d.free_cool is False
    assert d.mode == "cool"
    assert d.target_temp == 25.0


def test_湿度が上限ちょうどなら突入できる():
    d = call(outdoor=23.7, room=27.3, humidity=70.0)
    assert d.free_cool is True


def test_室内も快適なら送風ではなく電源オフ():
    d = call(outdoor=23.0, room=24.0, humidity=50)
    assert d.power == "off"
    assert d.mode is None
    assert d.free_cool is False


def test_無効化していれば常に冷房マトリクスどおり():
    d = call(outdoor=23.7, room=27.3, humidity=53, free_cool_enabled=False)
    assert d.free_cool is False
    assert d.mode == "cool"
    assert d.target_temp == 25.0


# ---------- 離脱（冷房復帰）とロックアウト ----------

def test_室温が上がりきったら冷房に復帰する():
    d = call(outdoor=23.7, room=29.5, humidity=53, last_free_cool=True)
    assert d.free_cool is False
    assert d.mode == "cool"
    assert d.power == "on"
    assert d.free_cool_abort is True          # ロックアウト開始の合図
    assert "冷房に復帰" in d.reason


def test_復帰の閾値ちょうどでも冷房に戻る():
    d = call(outdoor=23.7, room=29.0, humidity=53, last_free_cool=True)
    assert d.free_cool is False
    assert d.free_cool_abort is True


def test_送風中でなければ復帰フラグは立たない():
    d = call(outdoor=23.7, room=29.5, humidity=53, last_free_cool=False)
    assert d.free_cool is False
    assert d.free_cool_abort is False


def test_ロックアウト中は外気が涼しくても冷房のまま():
    d = call(outdoor=23.7, room=27.3, humidity=53, lockout_active=True)
    assert d.free_cool is False
    assert d.mode == "cool"
    assert d.target_temp == 25.0


def test_ロックアウトが明ければまた送風に入れる():
    d = call(outdoor=23.7, room=27.3, humidity=53, lockout_active=False)
    assert d.free_cool is True


# ---------- 外気温のヒステリシス ----------

def test_突入は閾値ちょうどまで():
    assert call(outdoor=25.0, room=27.3).free_cool is True
    assert call(outdoor=25.1, room=27.3).free_cool is False


def test_送風中は閾値プラスヒステリシスまで抜けない():
    # 25.0 で入り、25.3 ではまだ抜けず、25.8 で抜ける
    assert call(outdoor=25.0, room=27.3, last_free_cool=True).free_cool is True
    assert call(outdoor=25.3, room=27.3, last_free_cool=True).free_cool is True
    assert call(outdoor=25.7, room=27.3, last_free_cool=True).free_cool is True
    d = call(outdoor=25.8, room=27.3, last_free_cool=True)
    assert d.free_cool is False
    assert d.mode == "cool"


def test_free_cool_out_okの単体挙動():
    assert logic.free_cool_out_ok(25.0, 25.0, False, 0.7) is True
    assert logic.free_cool_out_ok(25.1, 25.0, False, 0.7) is False
    assert logic.free_cool_out_ok(25.7, 25.0, True, 0.7) is True
    assert logic.free_cool_out_ok(25.8, 25.0, True, 0.7) is False


# ---------- 既存挙動の回帰テスト ----------

def test_温度帯のヒステリシスが壊れていない():
    # 前回の帯が無ければ素直な判定
    assert logic.tier_with_hysteresis(33.0, logic.OUT_THRESHOLDS, None, HYST) == 0
    assert logic.tier_with_hysteresis(28.0, logic.OUT_THRESHOLDS, None, HYST) == 1
    assert logic.tier_with_hysteresis(27.9, logic.OUT_THRESHOLDS, None, HYST) == 2
    # 猛暑(0)から出るには 33-0.7=32.3 を下回る必要がある
    assert logic.tier_with_hysteresis(32.5, logic.OUT_THRESHOLDS, 0, HYST) == 0
    assert logic.tier_with_hysteresis(32.2, logic.OUT_THRESHOLDS, 0, HYST) == 1
    # 夏日(1)から猛暑(0)に入るには 33+0.7=33.7 を超える必要がある
    assert logic.tier_with_hysteresis(33.5, logic.OUT_THRESHOLDS, 1, HYST) == 1
    assert logic.tier_with_hysteresis(33.8, logic.OUT_THRESHOLDS, 1, HYST) == 0


def test_マトリクスの意味が変わっていない():
    assert logic.MATRIX[(0, 0)] == ("on", 19.0, "auto")
    assert logic.MATRIX[(1, 1)] == ("on", 24.0, "auto")
    assert logic.MATRIX[(2, 0)] == ("on", 25.0, "min")
    assert logic.MATRIX[(2, 2)] == ("off", None, None)


def test_猛暑時は外気冷却モードに入らず従来どおり():
    d = call(outdoor=35.0, room=28.0, humidity=50)
    assert d.free_cool is False
    assert d.mode == "cool"
    assert (d.power, d.target_temp, d.volume_pref) == ("on", 19.0, "auto")


def test_理由文字列の基本形式は従来どおり():
    d = call(outdoor=30.0, room=27.3, humidity=50)
    assert d.reason == "外気夏日(30.0℃) × 室内暑い(27.3℃)"


def test_帯の記憶が引き継がれる():
    d = call(outdoor=32.5, room=26.0, humidity=50, last_out_tier=0, last_room_tier=1)
    assert d.out_tier == 0
    assert d.room_tier == 1
    assert d.mode == "cool"
