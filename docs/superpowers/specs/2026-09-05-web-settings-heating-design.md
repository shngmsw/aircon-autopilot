# 目標帯の Web UI 設定 + 暖房対応 設計

日付: 2026-09-05

## 背景と目的

目標室温（`ROOM_TARGET_LOW` / `ROOM_TARGET_HIGH`）は現在 `.env` でしか変更できず、変更のたびに再起動が必要になる。また制御は冷房専用で、寒い時期は手動操作に頼るしかない。

本変更で次の 2 点を実現する。

- 目標帯を Web ダッシュボードから変更できるようにする（再起動不要・再起動しても保持）
- 室温が目標帯を下回ったら暖房する。ユーザーは季節を問わず「目標帯を変えるだけ」で室温を保てる

## 決定事項

ブレインストーミングで確認した方針:

| 論点 | 決定 |
|---|---|
| 冷暖の切り替え | 室温と目標帯の関係から自動判定（UI のモードスイッチは設けない） |
| UI から設定できる範囲 | 目標帯（low / high）のみ。gain 等のチューニング系は引き続き `.env` |
| 暖房で帯に入ったとき | 電源オフ（運転継続はしない） |
| 設定の保存先 | SQLite の state（key-value）。`.env` は初期値扱い |

## 1. 目標帯の Web UI 設定

### API

- `GET /api/settings`
  - 返り値: `{"room_target_low": 24.0, "room_target_high": 25.5, "source": "override" | "env"}`
  - `source` はオーバーライドが保存されているかを示す（UI 表示用）。
- `POST /api/settings` ボディ `{"low": 20.0, "high": 22.0}`
  - 検証: 16.0 ≦ low、high ≦ 30.0、low + 0.5 ≦ high（帯の幅が暖房オフのヒステリシスを兼ねるため最低幅を保証）
  - 検証エラーは HTTP 422 で理由を返す。
  - 成功時は state キー `room_target_low` / `room_target_high` に保存し、判定サイクルを即時実行してから結果を返す（変更がすぐエアコンに反映される）。

### 設定の解決順

controller は毎サイクル、次の順で有効値を決める。

1. `store.get_state("room_target_low" / "room_target_high")`
2. なければ `config.ROOM_TARGET_LOW` / `config.ROOM_TARGET_HIGH`

解決はヘルパー関数（例: `controller.effective_target_band()`）にまとめ、API と制御サイクルの両方から使う。

### UI

ダッシュボード（`static/index.html`）に目標帯の設定欄を追加する。

- low / high の数値入力（`step=0.5`、`min=16` / `max=30`）と保存ボタン
- 現在の有効値を表示。`source` が `env` なら「.env の既定値」であることが分かる表示にする
- 保存成功・検証エラーは既存の手動操作 UI と同じ流儀でフィードバックする

## 2. 暖房対応（室温追従モードの拡張）

暖房は室温追従モード（`ROOM_TARGET_ENABLED=true`）でのみ動く。マトリクスモードは冷房専用のまま変更しない。

### room_target_setpoint の拡張

現在の「room < low → 停止」の分岐を暖房に置き換え、冷房と対称にする。関数に運転モードの情報を返させる（返り値に mode を追加するか、Decision 組み立て側で判断する。実装時に自然な方を選ぶ）。

- **room > high** → 冷房（従来どおり）。超過分 × gain だけ設定温度を下げる。
- **帯内** → 冷房で運転中なら従来どおり現状維持。暖房で運転中なら「帯に入った」と判定して電源オフ。
- **room < low かつ暖房許可** → 暖房オン。
  - 設定温度: 不足分 `low - room` × gain だけ上げる（最低 1℃ は動かす）。上限は `ROOM_TARGET_SET_MAX`。基準は現在の暖房設定温度、なければ `low` から始める。
  - 風量: 不足が `ROOM_TARGET_MAX_VOL_OVER` 以上なら `max`、それ以外は `auto`（冷房と対称）。
- **room < low だが暖房不許可**（外気ガードに引っかかる） → 従来どおり停止。

### 暖房オフのヒステリシス

帯の下端ちょうどでオンオフが振動しないよう、しきい値をずらす。

- 暖房オン: `room < low`
- 暖房オフ: `room ≥ min(low + HYSTERESIS, high)`（既定 0.7℃。帯に少し入ってから切る。帯の幅がヒステリシスより狭い場合でも、上端を超えて暖房を続けて冷房判定に飛び込まないよう high で頭打ちにする）

前回暖房中だったかは state（例: `heating_active`）で持ち、controller から decide に渡す。

### 誤暖房ガード（外気温）

夏の朝など、室温がたまたま帯を下回っただけで暖房しないよう、外気温の条件を付ける。

- `.env` に `HEAT_OUT_MAX`（既定 20.0℃）を追加
- 外気温 < `HEAT_OUT_MAX` のときだけ暖房を許可する

### 運転モードとフォールバック

- 暖房の運転モードは Nature Remo 標準の `"warm"`。
- 機種が warm 非対応なら電源オフにフォールバックし、note に残す（送風非対応時と同じパターン）。
- 設定温度・風量は warm モードの対応値一覧（`mode_temp_options` / `mode_vol_options`）に丸めて送る（既存の仕組みをそのまま使う）。
- controller が decide に渡す `current_set_temp` は、現在の運転モードが cool または warm のときに渡す（暖房中の設定温度も追従の基準になる）。

### 外気冷却モードとの関係

暖房判定になった回は free-cool の上書きをスキップする。free-cool の適用条件を「冷房（mode == "cool"）で電源オンの回」に限定する。

## 3. テスト

- `tests/test_logic.py`
  - 暖房オン（room < low、外気が冷たい）で warm・設定温度・風量が正しいこと
  - 不足が大きいときに設定温度が gain 倍で上がり、`ROOM_TARGET_SET_MAX` で頭打ちになること
  - 帯に入ったら（`room ≥ min(low + hyst, high)`）電源オフになること、前回暖房中で `low ≦ room < low + hyst` なら暖房を継続すること
  - 外気ガード: 外気 ≥ `HEAT_OUT_MAX` なら room < low でも停止
  - 暖房判定時に free-cool が発動しないこと
  - 既存の冷房・送風ケースが回帰しないこと
- `tests/test_controller_*.py`（結合テスト、Remo・天気モック）
  - state のオーバーライドが目標帯として使われること（`.env` フォールバック含む）
  - warm 非対応機で電源オフにフォールバックすること
- API テスト
  - `POST /api/settings` の検証エラー（範囲外、low ≧ high、幅不足）が 422 になること
  - 保存後に `GET /api/settings` の `source` が `override` になること

## 変更ファイル

| ファイル | 変更 |
|---|---|
| `app/logic.py` | `room_target_setpoint` の暖房対応、`decide` の暖房分岐と free-cool 適用条件 |
| `app/controller.py` | 目標帯の解決ヘルパー、warm 対応（current_set・フォールバック）、`heating_active` の保存 |
| `app/main.py` | `GET/POST /api/settings` |
| `app/config.py` | `HEAT_OUT_MAX` 追加 |
| `app/static/index.html` | 目標帯の設定 UI |
| `README.md` | API 表・制御ロジック・`.env` の説明を更新 |

`store.py` は変更なしの見込み（既存の key-value をそのまま使う）。

## スコープ外

- gain・デッドバンドなどチューニング系パラメータの UI 化
- マトリクスモードでの暖房
- 暖房版の外気利用（窓開け推奨など）
- 冷暖自動切替のためのモードスイッチ UI
