# 目標帯の Web UI 設定 + 暖房対応 実装プラン

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 目標室温の帯を Web ダッシュボードから変更できるようにし、室温が帯を下回ったら自動で暖房する（仕様: `docs/superpowers/specs/2026-09-05-web-settings-heating-design.md`）。

**Architecture:** 既存の室温追従モード（`logic.room_target_setpoint`）を冷暖対称に拡張し、返り値に運転モードを加える。暖房は「room < low かつ 外気温 < HEAT_OUT_MAX」で `warm`、帯に入ったら（`room ≥ min(low + HYSTERESIS, high)`）電源オフ。目標帯は SQLite の state のオーバーライド → `.env` の順で解決し、`GET/POST /api/settings` で読み書きする。

**Tech Stack:** Python 3 / FastAPI / SQLite / pytest。実行はすべて `uv run`（このリポジトリは uv venv 必須。開発サーバーはポート 8010 を使う。8000 は Coolify が使用中）。

**実行済みの前提知識（エンジニア向け）:**
- テスト実行: `uv run pytest -q`（リポジトリルートで）
- `logic.decide` は純粋関数。設定値はすべて controller から引数で渡す流儀
- `store.get_state/set_state` は JSON シリアライズされる key-value（SQLite の `state` テーブル）
- `remo.supports_mode(aircon, mode)` / `remo.mode_temp_options` / `remo.nearest_temp` / `remo.pick_volume` が機種対応値への丸めを担う
- 既存テストの流儀: 関数名は日本語、controller の結合テストは `tests/test_controller_free_cool.py` の `env` fixture パターン（Remo・天気をモック、一時DB）

---

### Task 1: room_target_setpoint の暖房対応

`room_target_setpoint` の返り値を `(電源, 設定温度, 理由)` から `(電源, 設定温度, 理由, 運転モード)` の4要素に変え、暖房分岐を追加する。

**Files:**
- Modify: `app/logic.py:79-130`（`room_target_setpoint`）
- Test: `tests/test_room_target.py`

- [ ] **Step 1: 既存テストのヘルパーを4要素返却に合わせ、暖房のテストを追加する**

`tests/test_room_target.py` の `setpoint` ヘルパー（12〜15行目）を次に置き換える（末尾に `[:3]` を付けるだけ。既存テスト本体は変更しない）:

```python
def setpoint(room, current_set):
    return logic.room_target_setpoint(
        room, current_set, LOW, HIGH, GAIN, SET_MIN, SET_MAX, DEAD
    )[:3]
```

その直後（`test_ずれが大きいほど大きく下げる` の前）に4要素版ヘルパーを追加する:

```python
def setpoint4(room, current_set, **kw):
    return logic.room_target_setpoint(
        room, current_set, LOW, HIGH, GAIN, SET_MIN, SET_MAX, DEAD, **kw
    )
```

`# --- decide への組み込み ---` の直前に暖房のテストを追加する:

```python
# --- 暖房 ---

def test_外気が冷たく目標を下回れば暖房する():
    # 2.0℃不足 × gain1.5 = 3 → low(24) + 3 = 27℃
    power, temp, _, mode = setpoint4(room=22.0, current_set=None, heat_allowed=True)
    assert (power, temp, mode) == ("on", 27.0, "warm")


def test_暖房を許可しなければ従来どおり停止する():
    power, temp, _, mode = setpoint4(room=22.0, current_set=None, heat_allowed=False)
    assert (power, temp, mode) == ("off", None, None)


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
```

- [ ] **Step 2: テストが失敗することを確認する**

Run: `uv run pytest tests/test_room_target.py -q`
Expected: FAIL（新テストは `TypeError: ... unexpected keyword argument 'heat_allowed'`、既存テストも `[:3]` のスライスは通るが実装が3要素返却のままなので通らないものが出る）

- [ ] **Step 3: room_target_setpoint を実装する**

`app/logic.py` の `room_target_setpoint`（79〜130行目）全体を次に置き換える:

```python
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

    if room < low or (heating_now and room < heat_off):
        # 寒い側。外気が暖かいのに室温が低いだけなら暖房はしない
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

    if heating_now:
        # 帯に入ったので暖房はここで終わり
        return "off", None, (
            f"室温{room:.1f}℃が目標帯({low:.1f}〜{high:.1f}℃)に入ったので暖房を停止"
        ), None

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
```

`BLOW_MODE = "blow"`（27行目）の直後に定数を追加する:

```python
# 暖房で使う運転モード名（Nature Remo の標準名）
WARM_MODE = "warm"
```

この時点では `decide` はまだ3要素アンパック（`power, temp, reason = room_target_setpoint(...)`）のままなので、`decide` 側のアンパックを一時的に4要素へ最小修正しておく（Task 2 で本対応）。`decide` 内の該当箇所（196〜200行目）:

```python
        power, temp, reason, _rt_mode = room_target_setpoint(
            room, current_set_temp, room_target_low, room_target_high,
            room_target_gain, room_target_set_min, room_target_set_max,
            room_target_deadband,
        )
```

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run pytest tests/test_room_target.py tests/test_logic.py -q`
Expected: PASS（全件）

- [ ] **Step 5: コミット**

```bash
git add app/logic.py tests/test_room_target.py
git commit -m "室温追従の setpoint を冷暖対称にし、暖房分岐を追加"
```

---

### Task 2: decide への暖房組み込み

`decide` に `heat_out_max` / `heating_now` を追加し、運転モード・風量・外気冷却モードのガードを暖房対応にする。

**Files:**
- Modify: `app/logic.py:147-266`（`decide`）
- Test: `tests/test_room_target.py`

- [ ] **Step 1: decide レベルのテストを追加する**

`tests/test_room_target.py` の末尾（`# --- 風量 ---` セクションの後）に追加する。`call` ヘルパーは既存のまま使える（`**kw` が decide へ素通しされる）:

```python
# --- decide の暖房 ---

def test_decideで外気が冷たく目標を下回れば暖房する():
    d = call(outdoor=10.0, room=22.0)
    # 2.0℃不足 × gain1.5 = 3 → 24+3=27℃。不足2.0 ≥ 1.0 なので風量max
    assert (d.power, d.mode, d.target_temp, d.volume_pref) == ("on", "warm", 27.0, "max")


def test_不足が小さければ暖房の風量はauto():
    # 0.5℃不足 × gain1.5 = 0.75 → 最低1℃動かして 25℃。不足0.5 < 1.0 なので auto
    d = call(outdoor=10.0, room=23.5)
    assert (d.power, d.mode, d.target_temp, d.volume_pref) == ("on", "warm", 25.0, "auto")


def test_外気が暖かければ暖房せず停止する():
    # 外気25℃ ≥ HEAT_OUT_MAX(既定20℃) → 夏の朝の誤暖房ガード
    d = call(outdoor=25.0, room=22.0)
    assert d.power == "off"


def test_暖房判定では外気冷却モードに入らない():
    # 外気15℃は free_cool_out_max(25)以下だが、暖房を送風で上書きしない
    d = call(outdoor=15.0, room=22.0, free_cool_enabled=True, humidity=50)
    assert (d.power, d.mode) == ("on", "warm")
    assert d.free_cool is False


def test_decideでも暖房は帯に少し入ってから切る():
    d = call(outdoor=10.0, room=24.3, current_set=26.0, heating_now=True)
    assert (d.power, d.mode, d.target_temp) == ("on", "warm", 26.0)

    d = call(outdoor=10.0, room=24.8, current_set=26.0, heating_now=True)
    assert d.power == "off"


def test_冬に帯内で停止中なら停止のまま():
    d = call(outdoor=10.0, room=24.5)
    assert d.power == "off"
```

- [ ] **Step 2: テストが失敗することを確認する**

Run: `uv run pytest tests/test_room_target.py -q`
Expected: FAIL（`decide() got an unexpected keyword argument 'heating_now'` と、暖房にならず `off` になるアサーション失敗）

- [ ] **Step 3: decide を実装する**

`app/logic.py` の `decide` を修正する。

(1) シグネチャの `current_set_temp: float | None = None,` の直後にパラメータを追加:

```python
    current_set_temp: float | None = None,
    heat_out_max: float = 20.0,
    heating_now: bool = False,
) -> Decision:
```

(2) docstring の末尾（`目標室温は外気で変えない。外気は「冷房か送風か」の判断にだけ使う。` の後）に追記:

```python
    室温が room_target_low を下回り、かつ外気温が heat_out_max より低ければ
    暖房(warm)する。heating_now は「いま暖房で運転中か」で、暖房オフの
    ヒステリシス判定に使う。暖房は室温追従モード限定（マトリクスは冷房専用）。
```

(3) `if room_target_enabled:` ブロック（193〜209行目）を次に置き換える:

```python
    if room_target_enabled:
        # 室温そのものを目標帯に入れる。設定温度は結果を見て上下させるので、
        # 外気は「冷房か送風か」「暖房してよいか」の判断にだけ使う
        power, temp, reason, rt_mode = room_target_setpoint(
            room, current_set_temp, room_target_low, room_target_high,
            room_target_gain, room_target_set_min, room_target_set_max,
            room_target_deadband,
            heat_allowed=outdoor < heat_out_max,
            heating_now=heating_now,
            hyst=hyst,
        )
        # 目標から大きく外れている間は能力を出しきる。
        # 目標帯に近づいたら auto に戻して静かにする
        if power != "on":
            vol = None
        elif rt_mode == WARM_MODE:
            vol = "max" if room_target_low - room >= room_target_max_vol_over else "auto"
        elif room - room_target_high >= room_target_max_vol_over:
            vol = "max"
        else:
            vol = "auto"
        reason = f"外気{outdoor:.1f}℃ / {reason}"
    else:
        power, temp, vol = MATRIX[(out_tier, room_tier)]
        rt_mode = "cool"
        if cool_out_hot_target is not None and (out_tier, room_tier) == (2, 0):
            temp = cool_out_hot_target
        reason = (
            f"外気{OUT_LABELS[out_tier]}({outdoor:.1f}℃)"
            f" × 室内{ROOM_LABELS[room_tier]}({room:.1f}℃)"
        )
    mode = rt_mode if power == "on" else None
```

（元の `mode = "cool" if power == "on" else None` の行はこの置き換えに含めて削除する）

(4) 外気冷却モードの突入条件（`elif power == "on" and out_ok and ...`）に冷房限定のガードを足す:

```python
        elif power == "on" and mode == "cool" and out_ok and humid_ok and room_ok and not lockout_active:
```

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run pytest -q`
Expected: PASS（全テストスイート。既存の `test_logic.py` / `test_controller_free_cool.py` の回帰もここで確認する）

- [ ] **Step 5: コミット**

```bash
git add app/logic.py tests/test_room_target.py
git commit -m "decide に暖房判定を組み込み、外気冷却モードを冷房限定にする"
```

---

### Task 3: controller の暖房対応と目標帯の解決

目標帯を「state のオーバーライド → .env」の順で解決するヘルパーを作り、run_cycle を warm 対応にする。

**Files:**
- Modify: `app/config.py`（`HEAT_OUT_MAX` 追加）
- Modify: `app/controller.py`
- Create: `tests/test_controller_heating.py`

- [ ] **Step 1: 結合テストを書く**

`tests/test_controller_heating.py` を新規作成する:

```python
"""制御サイクル（app/controller.py）の暖房と目標帯オーバーライドの結合テスト。

Remo と天気 API をモックし、SQLite は一時ファイルを使う。
"""
import asyncio

import pytest

from app import config, controller, remo, store


class FakeClient:
    """httpx.AsyncClient の代わり。remo.apply_settings もモックするので何も持たない。"""


WARM_TEMPS = [str(t) for t in range(18, 31)]


def _aircon(power_on: bool, mode: str, temp: str = "20",
            modes=("cool", "warm", "blow")) -> remo.AirconState:
    mode_options = {
        "cool": {"temp": ["25", "26", "27", "28"], "vol": ["1", "2", "auto"]},
        "warm": {"temp": WARM_TEMPS, "vol": ["1", "2", "auto"]},
        "blow": {"temp": [], "vol": ["1", "2"]},
    }
    mode_options = {m: v for m, v in mode_options.items() if m in modes}
    return remo.AirconState(
        appliance_id="a1",
        nickname="test",
        power_on=power_on,
        mode=mode,
        target_temp=temp,
        air_volume="1",
        temp_options=mode_options.get("cool", {}).get("temp", []),
        vol_options=mode_options.get("cool", {}).get("vol", []),
        modes=list(mode_options.keys()),
        mode_options=mode_options,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    # 一時DBに差し替え（モジュール内の接続キャッシュもリセット）
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    store._conn = None
    monkeypatch.setattr(config, "MIN_HOLD_MIN", 0)
    monkeypatch.setattr(config, "ROOM_TARGET_ENABLED", True)
    monkeypatch.setattr(config, "ROOM_TARGET_LOW", 20.0)
    monkeypatch.setattr(config, "ROOM_TARGET_HIGH", 22.0)
    monkeypatch.setattr(config, "ROOM_TARGET_GAIN", 1.0)
    monkeypatch.setattr(config, "FREE_COOL_ENABLED", False)
    monkeypatch.setattr(config, "HEAT_OUT_MAX", 20.0)

    calls: list[dict] = []

    async def fake_apply(client, aircon, power, temp, vol, mode="cool"):
        calls.append({"power": power, "temp": temp, "vol": vol, "mode": mode})

    monkeypatch.setattr(remo, "apply_settings", fake_apply)

    state = {"aircon": _aircon(False, ""), "room": 18.0, "humidity": 40, "outdoor": 8.0}

    async def fake_snapshot(client):
        return remo.Snapshot(
            room_temp=state["room"], humidity=state["humidity"], aircon=state["aircon"]
        )

    async def fake_outdoor(client):
        return state["outdoor"]

    monkeypatch.setattr(remo, "fetch_snapshot", fake_snapshot)
    monkeypatch.setattr(controller.weather, "get_outdoor_temp", fake_outdoor)
    return state, calls


def run():
    return asyncio.run(controller.run_cycle(FakeClient()))


def test_目標帯はオーバーライドが無ければenvの値(env):
    low, high, source = controller.effective_target_band()
    assert (low, high, source) == (20.0, 22.0, "env")


def test_目標帯はstateのオーバーライドが優先される(env):
    state, calls = env
    # env相当は20〜22℃。オーバーライドで24〜25.5℃に上げると室温23℃でも暖房する
    store.set_state("room_target_low", 24.0)
    store.set_state("room_target_high", 25.5)
    assert controller.effective_target_band() == (24.0, 25.5, "override")

    state["room"] = 23.0
    run()
    assert calls[-1]["mode"] == "warm"
    assert calls[-1]["temp"] == "25"   # 1.0℃不足 × gain1.0 → 24+1=25℃


def test_寒ければ暖房し_設定温度は不足に応じて上がる(env):
    state, calls = env
    # 室温18℃・目標20〜22℃ → 2℃不足 × gain1.0 → 設定 20+2=22℃
    run()
    assert calls[-1]["power"] == "on"
    assert calls[-1]["mode"] == "warm"
    assert calls[-1]["temp"] == "22"


def test_帯に入ったら暖房を停止する(env):
    state, calls = env
    state["aircon"] = _aircon(True, "warm", temp="22")
    state["room"] = 21.0   # オフしきい値 min(20+0.7, 22) = 20.7℃ を超えている
    run()
    assert calls[-1]["power"] == "off"


def test_帯の下端付近では暖房を継続する(env):
    state, calls = env
    ac = _aircon(True, "warm", temp="22")
    ac.air_volume = "auto"
    state["aircon"] = ac
    state["room"] = 20.3   # 20.7℃ 未満なので継続（設定も据え置き → APIコールなし）
    r = run()
    assert calls == []
    assert "現状維持" in r["note"]


def test_外気が暖かければ暖房しない(env):
    state, calls = env
    state["outdoor"] = 22.0   # HEAT_OUT_MAX(20.0) 以上
    state["room"] = 18.0
    run()
    assert calls == []   # 停止中→停止のままなので操作なし


def test_暖房非対応の機種では電源オフにする(env):
    state, calls = env
    state["aircon"] = _aircon(True, "cool", temp="25", modes=("cool", "blow"))
    state["room"] = 18.0
    r = run()
    assert calls[-1]["power"] == "off"
    assert "暖房非対応" in r["note"]
```

- [ ] **Step 2: テストが失敗することを確認する**

Run: `uv run pytest tests/test_controller_heating.py -q`
Expected: FAIL（`config` に `HEAT_OUT_MAX` が無い / `controller` に `effective_target_band` が無い）

- [ ] **Step 3: config.py に HEAT_OUT_MAX を追加する**

`app/config.py` の `# --- 室温追従モード（room target） ---` セクションの末尾（`ROOM_TARGET_MAX_VOL_OVER` の直後）に追加:

```python
# 室温が目標帯を下回ったときに暖房するのは、外気温がこの値より低いときだけ(℃)。
# 夏の朝など「たまたま室温が帯を下回った」だけで暖房しないためのガード
HEAT_OUT_MAX = _f("HEAT_OUT_MAX", 20.0)
```

- [ ] **Step 4: controller.py を実装する**

(1) `free_cool_lockout` の直後にヘルパーを追加:

```python
def effective_target_band() -> tuple[float, float, str]:
    """有効な目標室温の帯 (low, high, 出所) を返す。

    Web UI から保存したオーバーライド（SQLite の state）があればそれを、
    なければ .env の値を使う。出所は "override" / "env"。
    """
    low = store.get_state("room_target_low")
    high = store.get_state("room_target_high")
    if low is not None and high is not None:
        return float(low), float(high), "override"
    return config.ROOM_TARGET_LOW, config.ROOM_TARGET_HIGH, "env"
```

(2) `run_cycle` の current_set 算出部（96〜104行目）を次に置き換える:

```python
        lockout_active, _ = free_cool_lockout()
        rt_low, rt_high, _ = effective_target_band()
        # 室温追従は前回の設定温度を起点に上下させる。冷房・暖房以外(送風など)の
        # ときの値は基準にならないので渡さない
        current_set = None
        heating_now = aircon.power_on and aircon.mode == logic.WARM_MODE
        if aircon.power_on and aircon.mode in ("cool", logic.WARM_MODE) and aircon.target_temp:
            try:
                current_set = float(aircon.target_temp)
            except ValueError:
                pass
```

(3) `logic.decide(...)` 呼び出しのうち目標帯と末尾を変更:

```python
            room_target_low=rt_low,
            room_target_high=rt_high,
```

（`config.ROOM_TARGET_LOW` / `config.ROOM_TARGET_HIGH` を置き換え）、および `current_set_temp=current_set,` の直後に追加:

```python
            heat_out_max=config.HEAT_OUT_MAX,
            heating_now=heating_now,
```

(4) 送風フォールバック部（147〜155行目）を warm も対象にする:

```python
        want_power = d.power
        want_mode = d.mode
        unsupported_mode = None
        if want_power == "on" and want_mode in (logic.BLOW_MODE, logic.WARM_MODE) and not remo.supports_mode(
            aircon, want_mode
        ):
            # 非対応モードの機種では電源オフにフォールバックする
            unsupported_mode = want_mode
            want_power, want_mode = "off", None
```

(5) 電源オフ時の note（199〜202行目、`blow_unsupported` を参照している箇所）を置き換える:

```python
                if want_power == "off":
                    labels = {logic.BLOW_MODE: "送風", logic.WARM_MODE: "暖房"}
                    note = f"{note_prefix}{reason}" + (
                        f"・{labels[unsupported_mode]}非対応のため電源オフ"
                        if unsupported_mode else " → 電源オフ"
                    )
```

- [ ] **Step 5: テストが通ることを確認する**

Run: `uv run pytest -q`
Expected: PASS（全テストスイート）

- [ ] **Step 6: コミット**

```bash
git add app/config.py app/controller.py tests/test_controller_heating.py
git commit -m "controller を暖房対応にし、目標帯を state オーバーライドで解決する"
```

---

### Task 4: /api/settings と /api/status の拡張

**Files:**
- Modify: `app/main.py`
- Create: `tests/test_api_settings.py`

- [ ] **Step 1: API テストを書く**

`tests/test_api_settings.py` を新規作成する:

```python
"""/api/settings の検証・保存と /api/status の目標帯表示のテスト。

TestClient を context manager にせず使うことで lifespan（スケジューラ起動）を
走らせない。制御サイクルはモックする。
"""
import pytest
from fastapi.testclient import TestClient

from app import config, controller, main, store


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", str(tmp_path / "t.db"))
    store._conn = None
    monkeypatch.setattr(config, "ROOM_TARGET_LOW", 24.0)
    monkeypatch.setattr(config, "ROOM_TARGET_HIGH", 25.5)

    async def fake_cycle(client):
        return {"action": "set", "note": "test cycle"}

    monkeypatch.setattr(controller, "run_cycle", fake_cycle)
    return TestClient(main.app)


def test_保存前はenvの値が返る(client):
    body = client.get("/api/settings").json()
    assert body == {"room_target_low": 24.0, "room_target_high": 25.5, "source": "env"}


def test_目標帯を保存するとオーバーライドが有効になり判定も走る(client):
    res = client.post("/api/settings", json={"low": 20.0, "high": 22.0})
    assert res.status_code == 200
    j = res.json()
    assert (j["room_target_low"], j["room_target_high"], j["source"]) == (20.0, 22.0, "override")
    assert j["cycle"]["note"] == "test cycle"
    assert store.get_state("room_target_low") == 20.0
    assert store.get_state("room_target_high") == 22.0

    body = client.get("/api/settings").json()
    assert body == {"room_target_low": 20.0, "room_target_high": 22.0, "source": "override"}


@pytest.mark.parametrize("low,high", [
    (15.0, 22.0),    # low が下限16℃未満
    (20.0, 31.0),    # high が上限30℃超
    (23.0, 21.0),    # 上下逆
    (22.0, 22.4),    # 帯の幅が0.5℃未満
])
def test_不正な目標帯は422で保存されない(client, low, high):
    res = client.post("/api/settings", json={"low": low, "high": high})
    assert res.status_code == 422
    assert store.get_state("room_target_low") is None
```

- [ ] **Step 2: テストが失敗することを確認する**

Run: `uv run pytest tests/test_api_settings.py -q`
Expected: FAIL（`GET /api/settings` が 404）

- [ ] **Step 3: main.py を実装する**

(1) `AirconBody` の直後にモデルを追加:

```python
class SettingsBody(BaseModel):
    low: float
    high: float
```

(2) `/api/status` のレスポンスに目標帯を追加する。`"free_cool": {...},` の直後に挿入:

```python
        "room_target": {
            "enabled": config.ROOM_TARGET_ENABLED,
            **dict(zip(("low", "high", "source"), controller.effective_target_band())),
        },
```

（`dict(zip(...))` が読みにくければ、関数冒頭で `rt_low, rt_high, rt_source = controller.effective_target_band()` と受けて `{"enabled": ..., "low": rt_low, "high": rt_high, "source": rt_source}` でもよい。挙動は同じ）

(3) `run_now` の直前にエンドポイントを追加:

```python
@app.get("/api/settings")
async def get_settings():
    low, high, source = controller.effective_target_band()
    return {"room_target_low": low, "room_target_high": high, "source": source}


@app.post("/api/settings")
async def set_settings(body: SettingsBody):
    """目標室温の帯を保存し、すぐ判定サイクルを回して反映する。"""
    if not (16.0 <= body.low and body.high <= 30.0 and body.low + 0.5 <= body.high):
        raise HTTPException(
            422, "目標帯は 16〜30℃ の範囲で、下限+0.5℃ ≦ 上限 にしてください"
        )
    store.set_state("room_target_low", body.low)
    store.set_state("room_target_high", body.high)
    cycle = await controller.run_cycle(_client)
    low, high, source = controller.effective_target_band()
    return {
        "room_target_low": low,
        "room_target_high": high,
        "source": source,
        "cycle": cycle,
    }
```

- [ ] **Step 4: テストが通ることを確認する**

Run: `uv run pytest -q`
Expected: PASS（全テストスイート）

- [ ] **Step 5: コミット**

```bash
git add app/main.py tests/test_api_settings.py
git commit -m "目標帯を読み書きする /api/settings を追加し status にも載せる"
```

---

### Task 5: ダッシュボード UI

目標室温カードの追加と、暖房の表示対応。自動テストは無し（このリポジトリの UI は素の HTML/JS でテスト基盤が無い）。手動確認する。

**Files:**
- Modify: `app/static/index.html`

- [ ] **Step 1: 目標室温カードを追加する**

操作パネルの `</section>`（204行目）と記録グラフの `<section>` の間に挿入する:

```html
<!-- 目標室温 -->
<section class="card">
  <div class="panel-head">
    <h2>目標室温</h2>
    <span class="chip" id="targetSource" style="display:none">.env の既定値</span>
  </div>
  <div class="ctrl-grid">
    <div class="ctrl">
      <div class="label">下限（下回ると暖房）</div>
      <div class="stepper">
        <button class="step" id="lowDown">−</button>
        <div class="val mono" id="lowVal">--℃</div>
        <button class="step" id="lowUp">＋</button>
      </div>
    </div>
    <div class="ctrl">
      <div class="label">上限（上回ると冷房）</div>
      <div class="stepper">
        <button class="step" id="highDown">−</button>
        <div class="val mono" id="highVal">--℃</div>
        <button class="step" id="highUp">＋</button>
      </div>
    </div>
  </div>
  <div class="btn-row">
    <button class="primary" id="saveTargetBtn">目標を保存して今すぐ判定</button>
  </div>
</section>
```

- [ ] **Step 2: JS を追加・修正する**

(1) 状態変数の宣言部（`let histRows = [];` の直後）に追加:

```js
let targetBand = null;    // 目標室温カードの編集中の値 {low, high}
```

(2) `renderStatus()` 内の powerChip 設定（257〜261行目）を暖房対応にする:

```js
    powerChip.textContent = !ac.power_on
      ? `${ac.nickname} 停止中`
      : blowing
        ? `${ac.nickname} 送風`
        : `${ac.nickname} ${ac.mode === "warm" ? "暖房" : "冷房"} ${ac.target_temp}℃`;
```

(3) `renderStatus()` の末尾（風量セレクトの同期の後）に追加:

```js
  // 目標室温カード（未編集のときだけサーバー値で初期化する）
  const rt = state.room_target;
  if (rt) {
    if (targetBand === null) {
      targetBand = { low: rt.low, high: rt.high };
      renderTarget();
    }
    $("targetSource").style.display = rt.source === "env" ? "" : "none";
  }
```

(4) `renderStatus` 関数の直後にレンダラーと操作を追加:

```js
function renderTarget() {
  if (!targetBand) return;
  $("lowVal").textContent = targetBand.low.toFixed(1) + "℃";
  $("highVal").textContent = targetBand.high.toFixed(1) + "℃";
}

function stepTarget(key, delta) {
  if (!targetBand) return;
  let v = Math.round((targetBand[key] + delta) * 2) / 2;   // 0.5刻み
  v = Math.min(30, Math.max(16, v));
  // 帯の幅は最低0.5℃を保つ（サーバー側の検証と同じ）
  if (key === "low") v = Math.min(v, targetBand.high - 0.5);
  else v = Math.max(v, targetBand.low + 0.5);
  targetBand[key] = v;
  renderTarget();
}
$("lowDown").addEventListener("click", () => stepTarget("low", -0.5));
$("lowUp").addEventListener("click", () => stepTarget("low", +0.5));
$("highDown").addEventListener("click", () => stepTarget("high", -0.5));
$("highUp").addEventListener("click", () => stepTarget("high", +0.5));

$("saveTargetBtn").addEventListener("click", async () => {
  if (!targetBand) return;
  $("saveTargetBtn").disabled = true;
  try {
    const res = await fetch("/api/settings", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(targetBand),
    });
    if (!res.ok) {
      const j = await res.json().catch(() => ({}));
      return toast(j.detail || "保存に失敗しました");
    }
    const j = await res.json();
    toast(`目標 ${j.room_target_low}〜${j.room_target_high}℃ を保存しました`);
    await loadStatus();
    await loadHistory();
  } finally {
    $("saveTargetBtn").disabled = false;
  }
});
```

(5) グラフのツールチップ（413行目付近の `afterBody`）を暖房対応にする:

```js
          const label = r.power !== "on" ? "OFF"
            : r.mode === "blow" ? "送風"
            : r.mode === "warm" ? "暖房" : "冷房";
```

- [ ] **Step 3: 手動確認する**

Run: `uv run uvicorn app.main:app --port 8010`（8000 は Coolify が使うので必ず 8010）

ブラウザ（または `curl`）で確認する:
1. `curl -s localhost:8010/api/settings` → env の値と `"source": "env"` が返る
2. ダッシュボードで目標帯を変更して保存 → トーストが出て、`curl -s localhost:8010/api/settings` が `"source": "override"` になる
3. `curl -s -X POST localhost:8010/api/settings -H 'Content-Type: application/json' -d '{"low": 25, "high": 25.2}'` → 422
4. ステッパーが 16〜30℃・幅0.5℃の制約内でしか動かないこと

（実機のエアコンが暖房を始めても構わない設定値で試すこと。確認後は目標帯を普段の値に戻す）

- [ ] **Step 4: コミット**

```bash
git add app/static/index.html
git commit -m "ダッシュボードに目標室温の設定カードと暖房表示を追加"
```

---

### Task 6: README 更新と仕上げ

**Files:**
- Modify: `README.md`

- [ ] **Step 1: README を更新する**

以下を反映する（README の該当セクションを読んで、既存の文体・表形式に合わせて編集する）:

(1) API 表に2行追加:

```markdown
| GET | `/api/settings` | 目標室温の帯（`room_target_low` / `room_target_high`）と出所（`source`: `override`=UIで保存済み / `env`=.envの既定値） |
| POST | `/api/settings` `{"low": 20.0, "high": 22.0}` | 目標室温の帯を保存（16〜30℃・下限+0.5℃≦上限）。保存後すぐ判定サイクルを実行 |
```

また `/api/status` の行の説明に「目標室温の帯（`room_target`）」を追記する。

(2) 「制御ロジック > 室温追従モード」セクションに暖房の説明を追加:

```markdown
### 暖房（室温追従モードのみ）

室温が目標帯の下限を下回り、かつ外気温が `HEAT_OUT_MAX`（既定 20℃）より低ければ暖房（`warm`）する。設定温度は冷房と対称に「不足分 × gain」だけ上げる。室温が帯に入ったら（下限 + `HYSTERESIS`、ただし上限を超えない）電源を切り、再び下限を下回ったら暖房し直す。外気温の条件は、夏の朝など「たまたま室温が帯を下回った」だけで暖房しないためのガード。暖房非対応の機種では電源オフにフォールバックする。

目標帯は Web ダッシュボードの「目標室温」カードから変更できる。UI で保存した値は SQLite に残り、`.env` の `ROOM_TARGET_LOW` / `ROOM_TARGET_HIGH` より優先される（`.env` は初期値扱い）。
```

(3) `.env` の設定例（README にあれば）へ `HEAT_OUT_MAX=20.0` を追記する。

- [ ] **Step 2: 全テストを流して確認する**

Run: `uv run pytest -q`
Expected: PASS（全件）

- [ ] **Step 3: コミット**

```bash
git add README.md
git commit -m "README: 目標帯のWeb UI設定と暖房対応を反映"
```

---

## 完了条件

- `uv run pytest -q` が全件 PASS
- ダッシュボードから目標帯を変更でき、再起動後も保持される
- 室温が帯を下回り外気が冷たいとき暖房になる（ロジックはテストで担保。実機確認は冬まで保留でよい）
- 既存の冷房・送風・外気冷却の挙動に回帰がない（既存テストが全部通ること）
