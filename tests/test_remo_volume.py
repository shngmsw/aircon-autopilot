"""風量の選び方（app/remo.pick_volume）のテスト。

'auto' はエアコン任せ、'max' は最強、'min' は弱め。
機種によって受け付ける値が違うので、対応値の中から選べていることを確認する。
"""
from app import remo

# 実機（Nature Remo 経由）が返す cool 用の風量一覧
OPTIONS = ["1", "2", "3", "4", "5", "auto"]


def test_autoはエアコン任せを選ぶ():
    assert remo.pick_volume("auto", OPTIONS) == "auto"


def test_maxは最強を選ぶ():
    # 猛暑×暑いのときに自動任せにせず一気に冷やすため
    assert remo.pick_volume("max", OPTIONS) == "5"


def test_minは最弱を選ぶ():
    assert remo.pick_volume("min", OPTIONS) == "1"


def test_autoが無い機種ではautoも最強で代用する():
    nums = ["1", "2", "3"]
    assert remo.pick_volume("auto", nums) == "3"
    assert remo.pick_volume("max", nums) == "3"


def test_数値が二桁でも文字列比較にならない():
    # "10" > "9" を文字列で比べると "10" が小さく扱われてしまう
    assert remo.pick_volume("max", ["1", "9", "10"]) == "10"
    assert remo.pick_volume("min", ["1", "9", "10"]) == "1"


def test_候補が空なら空文字を返す():
    assert remo.pick_volume("auto", []) == ""
    assert remo.pick_volume("max", []) == ""


# --- 暖房用の温度の丸め（app/remo.floor_temp）---

def test_暖房は希望以下で最も高い温度に丸める():
    # 上に丸めると暑くなりすぎるので 25.5℃ は 25℃ にする
    assert remo.floor_temp(25.5, ["24", "25", "26"]) == "25"


def test_0_5刻みの機種ならその値を選ぶ():
    assert remo.floor_temp(25.5, ["25", "25.5", "26"]) == "25.5"


def test_希望以下の候補が無ければ最小の候補を選ぶ():
    assert remo.floor_temp(17.0, ["18", "19"]) == "18"


def test_候補が無ければ希望温度をそのまま返す():
    assert remo.floor_temp(25.0, []) == "25"
