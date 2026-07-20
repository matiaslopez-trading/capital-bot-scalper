"""
main.py — Bot Scalper v7
Flask + APScheduler. Ciclo cada 5 minutos, velas de 5 minutos.
9 activos, mean-reversion pura (sin filtro de tendencia 4H).

Objetivo (mandato del usuario): MUCHAS operaciones de calidad por día.
No importa long o short — lo que importa es que haya más aciertos que
desaciertos. R:R cercano a 1:1 (ATR SLx1.0 / TPx1.3), hasta 2 posiciones
simultáneas por activo.

Cuenta DEMO — dinero ficticio.
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
from scanner import run_scanner, COOLDOWN_VELAS, TIME_STOP_BARS, MAX_POS_PER_SYM

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app    = Flask(__name__)
client = CapitalClient()

WEBHOOK_SECRET    = os.environ.get("WEBHOOK_SECRET", "")
COOLDOWN_DURATION = timedelta(minutes=COOLDOWN_VELAS * 5)
CANDLE_MINUTES    = 5

scanner_state  = {}
scanner_lock   = threading.Lock()
last_scan_time = None
scan_errors    = {}

# own_positions[sym] = [ {deal_id, direction, open_time, entry, be_done, lock_done}, ... ]
# hasta MAX_POS_PER_SYM entradas por símbolo
own_positions      = {}
own_positions_lock = threading.Lock()

cooldown_until      = {}
cooldown_until_lock = threading.Lock()


def _now_utc():
    return datetime.now(timezone.utc)


def _bars_since(open_time):
    if open_time is None:
        return 0
    elapsed_sec = (_now_utc() - open_time).total_seconds()
    return int(elapsed_sec / (CANDLE_MINUTES * 60))


def _manage_open_positions(positions_api, signals):
    """
    Gestiona TP/SL nativos (ya los pone la plataforma), salida anticipada
    por RSI de vuelta a zona neutral, y time-stop. Además aplica trailing
    a breakeven cuando la operación progresa a favor.
    """
    now = _now_utc()
    from capital_client import SYMBOL_MAP
    epic_to_sym = {v: k for k, v in SYMBOL_MAP.items()}

    # Mapa deal_id -> datos de posición viva en la API (para PnL/entry/sl/tp)
    live_by_deal = {}
    for pos in positions_api:
        position = pos.get("position", {})
        deal_id  = position.get("dealId")
        if deal_id:
            live_by_deal[deal_id] = {
                "market":   pos.get("market", {}),
                "position": position,
            }

    with own_positions_lock:
        own_pos_copy = {s: list(v) for s, v in own_positions.items()}

    for sym, entries in own_pos_copy.items():
        sig          = signals.get(sym, {})
        rsi_curr     = sig.get("rsi", 50)
        rsi_prev     = sig.get("rsi_prev", 50)
        exit_long    = sig.get("exit_long_rsi", 55)
        exit_short   = sig.get("exit_short_rsi", 45)

        for entry_data in entries:
            deal_id   = entry_data["deal_id"]
            direction = entry_data["direction"]
            open_time = entry_data.get("open_time")
            bars      = _bars_since(open_time)

            live = live_by_deal.get(deal_id)
            if live is None:
                # Ya no está en la API -> se cerró por TP/SL nativo o externamente
                with own_positions_lock:
                    if sym in own_positions:
                        own_positions[sym] = [
                            e for e in own_positions[sym] if e["deal_id"] != deal_id
                        ]
                        if not own_positions[sym]:
                            del own_positions[sym]
                with cooldown_until_lock:
                    cooldown_until[sym] = now + COOLDOWN_DURATION
                logger.info(f"[manage] {sym} deal={deal_id}: ya no está abierta (TP/SL nativo). Cooldown.")
                continue

            position = live["position"]
            pnl      = float(position.get("unrealisedPnl", 0) or 0)
            entry    = float(position.get("level", 0) or 0)
            sl       = float(position.get("stopLevel", 0) or 0)
            tp       = float(position.get("limitLevel", 0) or 0)
            size     = float(position.get("size", 0) or 0)

            should_exit = False
            exit_reason = ""

            if bars >= TIME_STOP_BARS:
                should_exit = True
                exit_reason = f"time-stop ({bars} velas / {bars*CANDLE_MINUTES}min)"

            if not should_exit:
                if direction == "BUY" and rsi_prev < exit_long <= rsi_curr:
                    should_exit = True
                    exit_reason = f"RSI volvio a zona neutral+ ({rsi_curr:.1f} >= {exit_long})"
                elif direction == "SELL" and rsi_prev > exit_short >= rsi_curr:
                    should_exit = True
                    exit_reason = f"RSI volvio a zona neutral- ({rsi_curr:.1f} <= {exit_short})"

            if should_exit:
                logger.info(f"[manage] {sym} {direction} deal={deal_id}: {exit_reason} | PnL={pnl:+.2f}")
                try:
                    client.close_position(deal_id)
                    with own_positions_lock:
                        if sym in own_positions:
                            own_positions[sym] = [
                                e for e in own_positions[sym] if e["deal_id"] != deal_id
                            ]
                            if not own_positions[sym]:
                                del own_positions[sym]
                    with cooldown_until_lock:
                        cooldown_until[sym] = now + COOLDOWN_DURATION
                except Exception as e:
                    logger.error(f"[manage] {sym}: error cerrando {deal_id}: {e}")
                    if "404" in str(e) or "not found" in str(e).lower():
                        with own_positions_lock:
                            if sym in own_positions:
                                own_positions[sym] = [
                                    e for e in own_positions[sym] if e["deal_id"] != deal_id
                                ]
                                if not own_positions[sym]:
                                    del own_positions[sym]
                continue

            # Trailing a breakeven al 40% del camino al TP
            if pnl <= 0 or entry <= 0 or sl <= 0 or tp <= 0 or size <= 0:
                continue

            tp_dist_price = abs(tp - entry)
            if tp_dist_price == 0:
                continue
            tp_usd = tp_dist_price * size

            if not entry_data.get("be_done") and pnl >= tp_usd * 0.40:
                if direction == "BUY":
                    new_sl = round(entry * 1.0005, 5)
                    if new_sl > sl:
                        result = client.update_sl(deal_id, new_sl)
                        if result is not None:
                            entry_data["be_done"] = True
                            logger.info(f"[manage] {sym} LONG BE: SL={new_sl} (PnL={pnl:+.2f})")
                else:
                    new_sl = round(entry * 0.9995, 5)
                    if sl == 0 or new_sl < sl:
                        result = client.update_sl(deal_id, new_sl)
                        if result is not None:
                            entry_data["be_done"] = True
                            logger.info(f"[manage] {sym} SHORT BE: SL={new_sl} (PnL={pnl:+.2f})")

    # Persistir flags be_done actualizados
    with own_positions_lock:
        for sym, entries in own_pos_copy.items():
            if sym in own_positions:
                by_id = {e["deal_id"]: e for e in entries}
                for e in own_positions[sym]:
                    if e["deal_id"] in by_id:
                        e["be_done"] = by_id[e["deal_id"]].get("be_done", False)


def run_cycle():
    global last_scan_time

    try:
        if not client.cst or not client.x_token:
            logger.info("[main] Re-login necesario...")
            client.login()
            time.sleep(2)
    except Exception as e:
        logger.error(f"[main] Error en login: {e}")
        return

    try:
        data_5m = get_all_ohlcv(client)
    except Exception as e:
        logger.error(f"[main] Error descargando datos: {e}")
        return

    valid_syms = {sym for sym, rows in data_5m.items() if rows is not None}
    if not valid_syms:
        logger.warning("[main] CICLO ABORTADO - sin datos validos.")
        return

    logger.info(f"[main] Datos 5m validos: {len(valid_syms)}/{len(CAPITAL_EPICS)} activos")

    with own_positions_lock:
        open_counts = {s: len(v) for s, v in own_positions.items()}
    with cooldown_until_lock:
        cd_snapshot = dict(cooldown_until)

    try:
        results = run_scanner(
            data_5m,
            open_positions_count=open_counts,
            cooldown_until=cd_snapshot,
        )
    except Exception as e:
        logger.error(f"[main] Error en scanner: {e}")
        return

    with scanner_lock:
        scanner_state.clear()
        scanner_state.update(results)
        last_scan_time = datetime.utcnow().isoformat() + "Z"

    try:
        positions_api = client.get_positions()
        _manage_open_positions(positions_api or [], results)
    except Exception as e:
        logger.warning(f"[main] No se pudo gestionar posiciones: {e}")

    now = _now_utc()
    for sym, res in results.items():
        if sym not in valid_syms:
            continue

        signal = res.get("signal", "ESPERAR")
        if signal not in ("LONG", "SHORT"):
            continue

        try:
            with own_positions_lock:
                n_abiertas = len(own_positions.get(sym, []))
                if n_abiertas >= MAX_POS_PER_SYM:
                    continue

            entry = res.get("entry", 0)
            sl    = res.get("sl", 0)
            tp1   = res.get("tp1", 0)
            score = 2

            if entry and sl and tp1:
                deal = client.open_position(
                    symbol=sym, action=signal,
                    entry=entry, sl=sl, tp1=tp1,
                    score=score, sizing_mult=1.0,
                )
                if deal is None:
                    continue
                deal_id = deal.get("dealId")
                if deal_id:
                    with own_positions_lock:
                        own_positions.setdefault(sym, []).append({
                            "deal_id":   deal_id,
                            "direction": "BUY" if signal == "LONG" else "SELL",
                            "open_time": now,
                            "entry":     entry,
                            "be_done":   False,
                        })
                    logger.info(
                        f"[main] NUEVA POSICION: {sym} {signal} @ {entry} | "
                        f"SL={sl} TP={tp1} | RSI={res.get('rsi','?')} | deal={deal_id}"
                    )
                else:
                    logger.warning(f"[main] {sym}: posicion abierta sin dealId confirmado: {deal}")

        except Exception as e:
            err_str = str(e).lower()
            if "position" in err_str and ("closed" in err_str or "not found" in err_str or "404" in err_str):
                with cooldown_until_lock:
                    cooldown_until[sym] = now + COOLDOWN_DURATION
                logger.info(f"[main] {sym}: SL/cierre externo - cooldown activado")
            scan_errors[sym] = str(e)
            logger.error(f"[main] {sym}: {e}\n{traceback.format_exc()}")

    with cooldown_until_lock:
        vencidos = [s for s, t in cooldown_until.items() if now >= t]
        for s in vencidos:
            del cooldown_until[s]
            logger.info(f"[main] {s}: cooldown vencido")


def start_scheduler():
    retries = 0
    while not client.cst and retries < 10:
        logger.info(f"[main] Esperando login... {retries+1}/10")
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
    logger.info("[main] Scheduler activo - ciclo cada 5 minutos (velas de 5min).")


@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"]  = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return response


@app.route("/", methods=["GET"])
def health():
    with scanner_lock:
        n = sum(1 for r in scanner_state.values() if r.get("signal") in ("LONG", "SHORT"))
    with own_positions_lock:
        pos_copy = {s: list(v) for s, v in own_positions.items()}
        total_pos = sum(len(v) for v in pos_copy.values())
    with cooldown_until_lock:
        cd_copy = {s: t.isoformat() for s, t in cooldown_until.items()}
    return jsonify({
        "status":             "ok",
        "bot":                "Bot Scalper v7 (mean-reversion 5min, alto volumen)",
        "activos":            len(CAPITAL_EPICS),
        "last_scan":          last_scan_time,
        "signals_activos":    n,
        "own_positions":      pos_copy,
        "total_posiciones":   total_pos,
        "cooldowns":          cd_copy,
        "trading_habilitado": True,
        "cuenta":             "DEMO (dinero ficticio)",
    }), 200


@app.route("/signals", methods=["GET"])
def signals():
    with scanner_lock:
        state_copy = dict(scanner_state)
    with own_positions_lock:
        pos_copy = {s: list(v) for s, v in own_positions.items()}
    with cooldown_until_lock:
        cd_copy = {s: t.isoformat() for s, t in cooldown_until.items()}
    return jsonify({
        "last_scan":      last_scan_time,
        "signals":        state_copy,
        "errors":         scan_errors,
        "own_positions":  pos_copy,
        "cooldowns":      cd_copy,
    }), 200


@app.route("/scan", methods=["GET"])
def scan_now():
    t = threading.Thread(target=run_cycle, daemon=True)
    t.start()
    t.join(timeout=120)
    with scanner_lock:
        state_copy = dict(scanner_state)
    with own_positions_lock:
        pos_copy = {s: list(v) for s, v in own_positions.items()}
    return jsonify({
        "last_scan":     last_scan_time,
        "signals":       state_copy,
        "errors":        scan_errors,
        "own_positions": pos_copy,
    }), 200


@app.route("/stats", methods=["GET"])
def stats():
    try:
        positions  = client.get_positions()
        accounts   = client.get_accounts()
        activities = client.get_activity_history(days=7)
        with own_positions_lock:
            pos_copy = {s: list(v) for s, v in own_positions.items()}
        return jsonify({
            "bot":            "Bot Scalper v7 (mean-reversion 5min, alto volumen)",
            "own_positions":  pos_copy,
            "positions":      positions,
            "accounts":       accounts,
            "activities":     activities,
            "last_scan":      last_scan_time,
        }), 200
    except Exception as e:
        logger.error(f"[stats] Error: {e}\n{traceback.format_exc()}")
        return jsonify({"error": str(e)}), 500


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = request.get_json(force=True)
        logger.info(f"[webhook] {json.dumps(payload)}")
        threading.Thread(target=run_cycle, daemon=True).start()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


try:
    client.login()
    logger.info("[main] Login inicial OK.")
except Exception as e:
    logger.error(f"[main] Error login inicial: {e}")

threading.Thread(target=start_scheduler, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
