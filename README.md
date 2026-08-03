# Aircon Autopilot

外気温（気象庁アメダス）と室温（Nature Remo）から、エアコンの設定温度・風量・電源を自動制御するアプリです。Webダッシュボードで現在の状況確認・手動操作・気温記録グラフの閲覧ができます。

![ダッシュボード](docs/dashboard.png)

外出時にRemoアプリのオートメーションでオフにされると、自動制御は一時停止します（帰宅オンで自動再開）:

![外出オフ検知で一時停止中のダッシュボード](docs/dashboard-paused.png)

## できること

- 10分ごとに外気温×室温を判定し、エアコンを自動制御（冷房）
- 温度帯の切替にヒステリシス（0.7℃）＋操作後15分の保持時間 → 設定が細かく揺れない
- 機種が対応していない温度・風量は、対応する最も近い値へ自動で丸め
- 設定が変わるときだけAPIを呼ぶ（Nature RemoのAPI回数制限対策）
- ダッシュボード: 外気温/室温/湿度の現在値、操作パネル（電源・温度・風量・自動ON/OFF）、6時間〜7日の温度グラフ
- 手動操作すると自動制御は一時停止（勝手に上書きされない）。トグルで再開
- リモコンやRemoアプリ・オートメーションで外部からオフにされたら自動制御を一時停止し、外部からオンされたら自動再開する
- 全記録をSQLiteに保存

## セットアップ

```bash
cd aircon-autopilot
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# .env を編集して NATURE_ACCESS_TOKEN を設定
```

Nature Remo のトークンは https://home.nature.global で発行できます。

### アメダス観測所IDの調べ方

既定は `44132`（東京）です。最寄りの観測所にする場合は、
https://www.jma.go.jp/bosai/amedas/const/amedastable.json
をブラウザで開き、地名（例: "横浜"）を検索してキー（例: `46106`）を `.env` の `AMEDAS_STATION` に設定してください。

## 起動

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

ブラウザで `http://<サーバーのIP>:8000` を開くとダッシュボードが表示されます。
起動と同時に制御ループが動き始めます（初回は即時実行、以後は `CONTROL_INTERVAL_MIN` 分ごと）。

### Raspberry Pi 等での常駐（systemd）

`/etc/systemd/system/aircon-autopilot.service`:

```ini
[Unit]
Description=Aircon Autopilot
After=network-online.target

[Service]
WorkingDirectory=/home/pi/aircon-autopilot
ExecStart=/home/pi/aircon-autopilot/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now aircon-autopilot
```

### WSL2 での常駐（ユーザーsystemd）

sudo なしで済むユーザー単位のサービスを使う（`loginctl enable-linger` 済みであること）。
リポジトリ直下の `aircon-autopilot.service` を配置して有効化する:

```bash
mkdir -p ~/.config/systemd/user
cp aircon-autopilot.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now aircon-autopilot
```

Windows 再起動後は WSL 自体が止まっているため、スタートアップフォルダ
（`shell:startup`）に `wsl.exe -d Ubuntu --exec /bin/true` を非表示実行する
`.vbs` を置いてログオン時に WSL を起こす（WSL が起動すれば linger 設定により
サービスも自動で立ち上がる）。

## 制御ロジック

| 外気温 ＼ 室温 | 暑い (≥27℃) | ぬるい (25–27℃) | 快適 (<25℃) |
|---|---|---|---|
| **猛暑 (≥33℃)** | 19℃ / 強 | 23℃ / 強 | 26℃ / 弱 |
| **夏日 (28–33℃)** | 22℃ / 強 | 24℃ / 強 | 26℃ / 弱 |
| **涼しい (<28℃)** | 25℃ / 弱 | 26℃ / 弱 | **電源オフ** |

- しきい値をまたいでも ±0.7℃（`HYSTERESIS`）動くまで帯は変わりません
- 一度操作したら `MIN_HOLD_MIN`（既定15分）は次の操作をしません
- リモコン・Remoアプリ・オートメーションなどアプリの外からエアコンをオフにされると、自動制御は `paused_external`（一時停止）状態になり、以後は記録のみで操作しません。外部からオンにすると自動的に再開し、その回の判定は保持時間の制限を受けずに即座に最適化されます
- マトリクスを変えたい場合は `app/logic.py` の `MATRIX` を編集してください

## API

| メソッド | パス | 説明 |
|---|---|---|
| GET | `/api/status` | 現在値・エアコン状態・自動制御の状態（`auto`と`auto_state`: `on`/`off`/`paused_external`） |
| GET | `/api/history?hours=24` | 記録の取得（1〜744時間） |
| POST | `/api/auto` `{"enabled": true}` | 自動制御のON/OFF（`paused_external`中でも明示的に上書き可） |
| POST | `/api/aircon` `{"power":"on","target_temp":"24","air_volume":"2"}` | 手動操作（自動は一時停止） |
| POST | `/api/run-now` | 判定サイクルの即時実行 |

## 記録データ

- 保存先は SQLite（`data/aircon.db`、`DB_PATH` 環境変数で変更可）。`readings` テーブルに1サイクル1行（ts, outdoor, room, humidity, set_temp, air_volume, power, auto, action, note）
- 自動削除はなく**無期限に蓄積**されます。10分間隔で年間約5万行・数MB程度なので実用上問題になりません
- ダッシュボードのグラフと `/api/history` が参照できるのは直近31日分まで（データ自体は残ります）

## 注意事項

- **認証はありません。** 自宅LAN内での利用を前提にしています。外出先から使う場合はVPN（Tailscale等）経由にするか、リバースプロキシでBasic認証をかけてください。ポート開放での直公開はしないでください。
- Remo本体の温度センサーは自己発熱でやや高めに出ることがあります。実際の室温計と比べて `ROOM_TEMP_OFFSET`（例: `-1.0`）で補正してください。
- 冷房専用のロジックです。暖房対応は `logic.py` に冬用マトリクスを足す形で拡張できます。

## 構成

```
app/
├── main.py        # FastAPI + 定期実行スケジューラ + API
├── controller.py  # 制御サイクル（取得→判定→操作→記録）
├── logic.py       # 判定マトリクス + ヒステリシス
├── remo.py        # Nature Remo クライアント（対応値への丸め含む）
├── weather.py     # 気象庁アメダスから外気温取得
├── store.py       # SQLite（記録 + 状態保存）
├── config.py      # .env 設定
└── static/index.html  # ダッシュボード
```
