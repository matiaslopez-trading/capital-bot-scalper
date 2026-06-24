"""
main.py - Bot Scalper v6
Flask + APScheduler. Ciclo cada 5 minutos.
"""
import os
import json
import logging
import threading
import traceback
import time
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify
from capital_client import CapitalClient
from apscheduler.schedulers.background import BackgroundScheduler
from data_feed import get_all_ohlcv, CAPITAL_EPICS
from scanner import run_scanner, COOLDOWN_VELAS, TIME_STOP_BARS

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
client = CapitalClient()

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
COOLDOWN_DURATION = timedelta(minutes=COOLDOWN_VELAS * 15)
CANDLE_MINUTES = 15

scanner_state = {}
scanner_lock = threading.Lock()
last_scan_time = None
scan_errors = {}

own_positions = {}
own_positions_lock = threading.Lock()

position_open_time = {}
position_open_time_lock = threading.Lock()

cooldown_until = {}
cooldown_until_lock = threading.Lock()

trailing_state = {}
trailing_state_lock = threading.Lock()


def _now_utc():
    return datetime.now(timezone.utc)


def _bars_in_trade(sym):
    with position_open_time_lock:
        open_time = position_open_time.get(sym)
    if open_time is None:
        return 0
    elapsed_sec = (_now_utc() - open_time).total_seconds()
    return int(elapsed_sec / (CANDLE_MINUTES * 60))


def _manage_open_positions(positions_api, signals):
    now = _now_utc()
    from capital_client import SYMBOL_MAP
    epic_to_sym = {v: k for k, v in SYMBOL_MAP.items()}

    with own_positions_lock:
        own_pos_copy = dict(own_positions)

    for pos in positions_api:
        try:
            market = pos.get("market", {})
            position = pos.get("position", {})

            epic = market.get("epic", "")
            sym = epic_to_sym.get(epic)
            if not sym:
                continue

            deal_id = position.get("dealId", "")
            direction = position.get("direction", "")
            pnl = float(position.get("unrealisedPnl", 0) or 0)
            entry = float(position.get("level", 0) or 0)
            sl = float(position.get("stopLevel", 0) or 0)
            tp = float(position.get("limitLevel", 0) or 0)
            size = float(position.get("size", 0) or 0)

            if own_pos_copy.get(sym) != deal_id:
                continue

            sig = signals.get(sym, {})
            rsi_curr = sig.get("rsi", 50)
            rsi_prev = sig.get("rsi_prev", 50)
            long_tp_rsi = sig.get("long_tp_rsi", 70)
            short_tp_rsi = sig.get("short_tp_rsi", 30)
            fade_level = sig.get("momentum_fade_level", 50)
            bars = _bars_in_trade(sym)

            should_exit = False
            exit_reason = ""

            if bars >= TIME_STOP_BARS:
                should_exit = True
                exit_reason = "time-stop ({} velas)".format(bars)

            if not should_exit:
                if direction == "BUY" and rsi_curr >= long_tp_rsi:
                    should_exit = True
                    exit_reason = "TP RSI={:.1f} >= {}".format(rsi_curr, long_tp_rsi)
                elif direction == "SELL" and rsi_curr <= short_tp_rsi:
                    should_exit = True
                    exit_reason = "TP RSI={:.1f} <= {}".format(rsi_curr, short_tp_rsi)

            if not should_exit:
                if direction == "BUY" and rsi_prev >= fade_level and rsi_curr < fade_level:
                    should_exit = True
                    exit_reason = "momentum fade: RSI cruzo <{} ({:.1f})".format(fade_level, rsi_curr)
                elif direction == "SELL" and rsi_prev <= fade_level and rsi_curr > fade_level:
                    should_exit = True
                    exit_reason = "momentum fade: RSI cruzo >{} ({:.1f})".format(fade_level, rsi_curr)

            if should_exit:
                logger.info("[manage] {} {}: {} | PnL={:+.2f}".format(sym, direction, exit_reason, pnl))
                try:
                    client.close_position(deal_id)
                    with own_positions_lock:
                        own_positions.pop(sym, None)
                    with position_open_time_lock:
                        position_open_time.pop(sym, None)
                    with trailing_state_lock:
                        trailing_state.pop(deal_id, None)
                    with cooldown_until_lock:
                        cooldown_until[sym] = now + COOLDOWN_DURATION
                    logger.info("[manage] {}: cerrado. Cooldown activado.".format(sym))
                except Exception as e:
                    logger.error("[manage] {}: error cerrando {}: {}".format(sym, deal_id, e))
                    if "404" in str(e) or "not found" in str(e).lower():
                        with own_positions_lock:
                            own_positions.pop(sym, None)
                        with position_open_time_lock:
                            position_open_time.pop(sym, None)
                        with trailing_state_lock:
                            trailing_state.pop(deal_id, None)
                continue

            if pnl <= 0 or entry <= 0 or sl <= 0 or tp <= 0 or size <= 0:
                continue

            tp_dist_price = abs(tp - entry)
            if tp_dist_price == 0:
                continue

            tp_usd = tp_dist_price * size

            with trailing_state_lock:
                ts = trailing_state.setdefault(deal_id, {"be_done": False, "lock25_done": False})
                be_done = ts["be_done"]
                lock25_done = ts["lock25_done"]

            if not be_done and pnl >= tp_usd * 0.25:
                if direction == "BUY":
                    new_sl = round(entry * 1.001, 5)
                    if new_sl > sl:
                        result = client.update_sl(deal_id, new_sl)
                        if result is not None:
                            with trailing_state_lock:
                                trailing_state[deal_id]["be_done"] = True
                            logger.info("[manage] {} LONG trailing BE: SL={} (PnL={:+.2f})".format(sym, new_sl, pnl))
                else:
                    new_sl = round(entry * 0.999, 5)
                    if sl == 0 or new_sl < sl:
                        result = client.update_sl(deal_id, new_sl)
                        if result is not None:
                            with trailing_state_lock:
                                trailing_state[deal_id]["be_done"] = True
                            logger.info("[manage] {} SHORT trailing BE: SL={} (PnL={:+.2f})".format(sym, new_sl, pnl))

            if be_done and not lock25_done and pnl >= tp_usd * 0.50:
                if direction == "BUY":
                    new_sl = round(entry + tp_dist_price * 0.25, 5)
                    if new_sl > sl:
                        result = client.update_sl(deal_id, new_sl)
                        if result is not None:
                            with trailing_state_lock:
                                trailing_state[deal_id]["lock25_done"] = True
                            logger.info("[manage] {} LONG trailing lock25%: SL={} (PnL={:+.2f})".format(sym, new_sl, pnl))
                else:
                    new_sl = round(entry - tp_dist_price * 0.25, 5)
                    if sl == 0 or new_sl < sl:
                        result = client.update_sl(deal_id, new_sl)
                        if result is not None:
                            with trailing_state_lock:
                                trailing_state[deal_id]["lock25_done"] = True
                            logger.info("[manage] {} SHORT trailing lock25%: SL={} (PnL={:+.2f})".format(sym, new_sl, pnl))

        except Exception as e:
            logger.error("[manage] Error procesando posicion: {}\n{}".format(e, traceback.format_exc()))


def run_cycle():
    global last_scan_time

    try:
        if not client.cst or not client.x_token:
            logger.info("[main] Re-login necesario...")
            client.login()
            time.sleep(2)
    except Exception as e:
        logger.error("[main] Error en login: {}".format(e))
        return

    try:
        data_15m, data_4h = get_all_ohlcv(client)
    except Exception as e:
        logger.error("[main] Error descargando datos: {}".format(e))
        return

    valid_syms = {sym for sym, rows in data_15m.items() if rows is not None}
    if not valid_syms:
        logger.warning("[main] CICLO ABORTADO - sin datos validos.")
        return

    logger.info("[main] Datos 15m validos: {}/9 activos".format(len(valid_syms)))

    with own_positions_lock:
        open_pos_set = set(own_positions.keys())
    with cooldown_until_lock:
        cd_snapshot = dict(cooldown_until)

    try:
        results = run_scanner(data_15m, data_4h, open_positions=open_pos_set, cooldown_until=cd_snapshot)
    except Exception as e:
        logger.error("[main] Error en scanner: {}".format(e))
        return

    with scanner_lock:
        scanner_state.clear()
        scanner_state.update(results)
        last_scan_time = datetime.utcnow().isoformat() + "Z"

    try:
        positions_api = client.get_positions()
        if positions_api:
            _manage_open_positions(positions_api, results)
    except Exception as e:
        logger.warning("[main] No se pudo gestionar posiciones: {}".format(e))

    now = _now_utc()
    for sym, res in results.items():
        if sym not in valid_syms:
            continue
        signal = res.get("signal", "ESPERAR")
        if signal not in ("LONG", "SHORT"):
            continue

        try:
            with own_positions_lock:
                if sym in own_positions:
                    continue

            entry = res.get("entry", 0)
            sl = res.get("sl", 0)
            tp1 = res.get("tp1", 0)
            score = 2

            if entry and sl and tp1:
                deal = client.open_position(
                    symbol=sym, action=signal,
                    entry=entry, sl=sl, tp1=tp1,
                    score=score, sizing_mult=1.0,
                )
                if deal is None:
                    continue
                deal_id = (
                    deal.get("dealId") or
                    deal.get("dealReference") or
                    deal.get("affectedDeals", [{}])[0].get("dealId")
                )
                if deal_id:
                    with own_positions_lock:
                        own_positions[sym] = deal_id
                    with position_open_time_lock:
                        position_open_time[sym] = now
                    with trailing_state_lock:
                        trailing_state[deal_id] = {"be_done": False, "lock25_done": False}
                    logger.info(
                        "[main] NUEVA POSICION: {} {} @ {} | SL={} TP={} | RSI={} regime={} | deal={}".format(
                            sym, signal, entry, sl, tp1,
                            res.get("rsi", "?"), res.get("regime", "?"), deal_id
                        )
                    )
                else:
                    logger.warning("[main] {}: posicion abierta sin dealId: {}".format(sym, deal))

        except Exception as e:
            err_str = str(e).lower()
            if "position" in err_str and ("closed" in err_str or "not found" in err_str or "404" in err_str):
                with own_positions_lock:
                    own_positions.pop(sym, None)
                with position_open_time_lock:
                    position_open_time.pop(sym, None)
                with cooldown_until_lock:
                    cooldown_until[sym] = now + COOLDOWN_DURATION
                logger.info("[main] {}: SL/cierre externo - cooldown activado".format(sym))
            scan_errors[sym] = str(e)
            logger.error("[main] {}: {}\n{}".format(sym, e, traceback.format_exc()))

    with cooldown_until_lock:
        vencidos = [s for s, t in cooldown_until.items() if now >= t]
        for s in vencidos:
            del cooldown_until[s]
            logger.info("[main] {}: cooldown vencido".format(s))


def start_scheduler():
    retries = 0
    while not client.cst and retries < 10:
        logger.info("[main] Esperando login... {}/10".format(retries + 1))
        time.sleep(3)
        retries += 1
    if not client.cst:
        logger.error("[main] Login fallido. Scheduler no iniciado.")
        return
    logger.info("[main] Login confirmado. Lanzando primer ciclo...")
    threading.Thread(target=run_cycle, daemon=True).start()
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(run_cycle, "interval", minutes=5, id="scalper_cycle")
    scheduler.start()
    logger.info("[main] Scheduler activo - ciclo cada 5 minutos.")


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/", methods=["GET"])
def health():
    with scanner_lock:
        n = sum(1 for r in scanner_state.values() if r.get("signal") in ("LONG", "SHORT"))
    with own_positions_lock:
        pos_copy = dict(own_positions)
    with cooldown_until_lock:
        cd_copy = {s: t.isoformat() for s, t in cooldown_until.items()}
    with trailing_state_lock:
        ts_copy = dict(trailing_state)
    with position_open_time_lock:
        pot_copy = {s: t.isoformat() for s, t in position_open_time.items()}
    return jsonify({
        "status": "ok",
        "bot": "Bot Scalper v6 (RSI adaptativo)",
        "activos": 9,
        "last_scan": last_scan_time,
        "signals_activos": n,
        "own_positions": pos_copy,
        "position_open_time": pot_copy,
        "cooldowns": cd_copy,
        "trailing_state": ts_copy,
        "trading_habilitado": True,
    }), 200


@app.route("/signals", methods=["GET"])
def signals():
    with scanner_lock:
        state_copy = dict(scanner_state)
    with own_positions_lock:
        pos_copy = dict(own_positions)
    with cooldown_until_lock:
        cd_copy = {s: t.isoformat() for s, t in cooldown_until.items()}
    with trailing_state_lock:
        ts_copy = dict(trailing_state)
    with position_open_time_lock:
        bars_copy = {s: _bars_in_trade(s) for s in pos_copy}
    return jsonify({
        "last_scan": last_scan_time,
        "signals": state_copy,
        "errors": scan_errors,
        "own_positions": pos_copy,
        "bars_in_trade": bars_copy,
        "cooldowns": cd_copy,
        "trailing_state": ts_copy,
    }), 200


@app.route("/scan", methods=["GET"])
def scan_now():
    t = threading.Thread(target=run_cycle, daemon=True)
    t.start()
    t.join(timeout=120)
    with scanner_lock:
        state_copy = dict(scanner_state)
    with own_positions_lock:
        pos_copy = dict(own_positions)
    return jsonify({
        "last_scan": last_scan_time,
        "signals": state_copy,
        "errors": scan_errors,
        "own_positions": pos_copy,
    }), 200


@app.route("/stats", methods=["GET"])
def stats():
    try:
        positions = client.get_positions()
        accounts = client.get_accounts()
        activities = client.get_activity_history(days=7)
        with own_positions_lock:
            pos_copy = dict(own_positions)
        with trailing_state_lock:
            ts_copy = dict(trailing_state)
        with position_open_time_lock:
            bars_copy = {s: _bars_in_trade(s) for s in pos_copy}
        return jsonify({
            "bot": "Bot Scalper v6 (RSI adaptativo)",
            "own_positions": pos_copy,
            "bars_in_trade": bars_copy,
            "trailing_state": ts_copy,
            "positions": positions,
            "accounts": accounts,
            "activities": activities,
            "last_scan": last_scan_time,
        }), 200
    except Exception as e:
        logger.error("[stats] Error: {}\n{}".format(e, traceback.format_exc()))
        return jsonify({"error": str(e)}), 500


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = request.get_json(force=True)
        logger.info("[webhook] {}".format(json.dumps(payload)))
        threading.Thread(target=run_cycle, daemon=True).start()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


try:
    client.login()
    logger.info("[main] Login inicial OK.")
except Exception as e:
    logger.error("[main] Error login inicial: {}".format(e))

threading.Thread(target=start_scheduler, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
