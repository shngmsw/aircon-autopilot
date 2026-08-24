"""制御サイクル: センサー取得 → 判定 → （必要なら）エアコン操作 → 記録。"""
import logging
from datetime import datetime, timedelta

import httpx

from . import config, logic, remo, store, weather

log = logging.getLogger(__name__)


def _hold_active() -> tuple[bool, str]:
    """最終操作から MIN_HOLD_MIN 経っていないか判定する。"""
    last = store.get_state("last_command_ts")
    if not last:
        return False, ""
    last_dt = datetime.fromisoformat(last)
    until = last_dt + timedelta(minutes=config.MIN_HOLD_MIN)
    now = store.now_jst()
    if now < until:
        return True, until.strftime("%H:%M")
    return False, ""


def free_cool_lockout() -> tuple[bool, datetime | None]:
    """外気冷却モードのロックアウト（冷房復帰後の再突入禁止）の状態を返す。

    (ロックアウト中か, 解除時刻) を返す。解除時刻が未設定なら (False, None)。
    """
    raw = store.get_state("free_cool_lockout_until")
    if not raw:
        return False, None
    try:
        until = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return False, None
    return store.now_jst() < until, until


async def run_cycle(client: httpx.AsyncClient) -> dict:
    """1回分の制御サイクルを実行し、結果の概要を返す。"""
    snap = await remo.fetch_snapshot(client)
    outdoor = await weather.get_outdoor_temp(client)

    action = "none"
    note = ""
    note_prefix = ""
    skip_hold_check = False
    stop_here = False

    aircon = snap.aircon
    room = snap.room_temp

    if aircon is not None:
        expected_power = store.get_state("expected_power")
        actual_power = "on" if aircon.power_on else "off"
        state = store.auto_state()

        if expected_power is None:
            # 初回のキャリブレーション: 実機の現状で expected_power を初期化する
            store.set_state("expected_power", actual_power)
        elif state == "on" and expected_power == "on" and actual_power == "off":
            # 外部オフ検知: 自動制御を一時停止する（エアコンは操作しない）
            # expected_power も実態に合わせておかないと、トグル再開直後に再検知してしまう
            store.set_auto_state("paused_external")
            store.set_state("expected_power", "off")
            action = "pause"
            note = "外部操作でオフを検知 → 自動制御を一時停止（オンで自動再開）"
            stop_here = True
        elif state == "paused_external" and actual_power == "on":
            # 外部オン検知: 自動再開し、同一サイクルで通常判定を続行する
            store.set_auto_state("on")
            store.set_state("last_out_tier", None)
            store.set_state("last_room_tier", None)
            store.set_state("free_cool_active", False)
            store.set_state("free_cool_start_room", None)
            skip_hold_check = True
            note_prefix = "外部オンを検知 → 自動制御を再開。"
        elif state == "paused_external" and actual_power == "off":
            note = "外部オフにより一時停止中（オンで自動再開）"
            stop_here = True

    auto = store.auto_enabled()

    if stop_here:
        pass
    elif aircon is None:
        action = "error"
        note = "エアコンが見つかりません（トークン・登録を確認）"
    elif not auto:
        note = "自動制御オフ（記録のみ）"
    elif outdoor is None or room is None:
        action = "error"
        note = "外気温または室温が取得できず、操作を見送り"
    else:
        lockout_active, _ = free_cool_lockout()
        d = logic.decide(
            outdoor,
            room,
            store.get_state("last_out_tier"),
            store.get_state("last_room_tier"),
            config.HYSTERESIS,
            humidity=snap.humidity,
            last_free_cool=bool(store.get_state("free_cool_active", False)),
            lockout_active=lockout_active,
            free_cool_enabled=config.FREE_COOL_ENABLED,
            free_cool_out_max=config.FREE_COOL_OUT_MAX,
            free_cool_humid_max=config.FREE_COOL_HUMID_MAX,
            free_cool_abort_room=config.FREE_COOL_ABORT_ROOM,
            free_cool_start_room=store.get_state("free_cool_start_room"),
            free_cool_rise_min=config.FREE_COOL_RISE_MIN,
            cool_out_hot_target=config.COOL_OUT_HOT_TARGET,
        )
        store.set_state("last_out_tier", d.out_tier)
        store.set_state("last_room_tier", d.room_tier)
        was_free_cool = bool(store.get_state("free_cool_active", False))
        store.set_state("free_cool_active", d.free_cool)
        if d.free_cool and not was_free_cool:
            # 送風開始時の室温を覚えておき、復帰判定の基準にする
            store.set_state("free_cool_start_room", room)
        elif not d.free_cool:
            store.set_state("free_cool_start_room", None)
        reason = d.reason
        if d.free_cool_abort:
            # 室温上昇で冷房へ復帰した回。しばらく送風へ戻さない
            lockout_end = store.now_jst() + timedelta(minutes=config.FREE_COOL_LOCKOUT_MIN)
            store.set_state("free_cool_lockout_until", lockout_end.isoformat())
            reason += f"（{config.FREE_COOL_LOCKOUT_MIN}分は送風にしない）"

        want_power = d.power
        want_mode = d.mode
        blow_unsupported = False
        if want_power == "on" and want_mode == logic.BLOW_MODE and not remo.supports_mode(
            aircon, logic.BLOW_MODE
        ):
            # 送風非対応の機種では電源オフにフォールバックする
            want_power, want_mode = "off", None
            blow_unsupported = True

        # 目標を「そのモードで」機種が受け付ける値に丸める
        temp_opts = remo.mode_temp_options(aircon, want_mode) if want_mode else []
        vol_opts = remo.mode_vol_options(aircon, want_mode) if want_mode else []
        want_temp = (
            remo.nearest_temp(d.target_temp, temp_opts)
            if d.target_temp is not None else None
        )
        want_vol = remo.pick_volume(d.volume_pref, vol_opts) if d.volume_pref else None

        # 現状と同じなら何もしない（API回数の節約）
        # モードも比較しないと、送風中に冷房へ戻せなくなる
        same = (
            (want_power == "off" and not aircon.power_on)
            or (
                want_power == "on"
                and aircon.power_on
                and aircon.mode == want_mode
                and (want_temp is None or aircon.target_temp == want_temp)
                and (not want_vol or aircon.air_volume == want_vol)
            )
        )

        holding, until = _hold_active()
        if skip_hold_check:
            holding = False

        if same:
            # 現状追認でも expected_power を実態に同期する（放置すると stale になり
            # 外部オフを検知できなくなる）
            store.set_state("expected_power", want_power)
            note = f"{note_prefix}{reason} → 現状維持"
        elif holding:
            note = f"{note_prefix}{reason} → 変更したいが {until} まで保持中"
        else:
            try:
                await remo.apply_settings(
                    client, aircon, want_power, want_temp, want_vol,
                    mode=want_mode or "cool",
                )
                store.set_state("last_command_ts", store.now_jst().isoformat())
                store.set_state("expected_power", want_power)
                action = "set"
                if want_power == "off":
                    note = f"{note_prefix}{reason}" + (
                        "・送風非対応のため電源オフ" if blow_unsupported else " → 電源オフ"
                    )
                elif want_mode == logic.BLOW_MODE:
                    note = f"{note_prefix}{reason}・送風に切替" + (
                        f"（風量{want_vol}）" if want_vol else ""
                    )
                else:
                    note = f"{note_prefix}{reason} → {want_temp}℃ / 風量{want_vol}"
                # 記録には送信後の値を残す
                aircon.power_on = want_power == "on"
                aircon.mode = want_mode or aircon.mode
                if want_temp:
                    aircon.target_temp = want_temp
                if want_vol:
                    aircon.air_volume = want_vol
            except Exception as e:
                log.exception("エアコン操作に失敗")
                action = "error"
                note = f"操作失敗: {e}"

    set_temp = None
    if aircon and aircon.target_temp:
        try:
            set_temp = float(aircon.target_temp)
        except ValueError:
            pass

    store.add_reading(
        outdoor=outdoor,
        room=room,
        humidity=snap.humidity,
        set_temp=set_temp,
        air_volume=aircon.air_volume if aircon else None,
        power=("on" if aircon.power_on else "off") if aircon else None,
        auto=auto,
        action=action,
        note=note,
        mode=(aircon.mode if aircon and aircon.power_on else None),
    )
    log.info("サイクル完了: %s", note or action)
    return {"action": action, "note": note}
