"""
main.py — Bot Scalper v3
Flask + APScheduler. Ciclo cada 5 minutos.
9 activos nuevos de alta volatilidad en 15min con bias de 4H.

Cambios v3 vs v2:
- Nuevos 9 activos (US100, GBPJPY, DOGEUSD, XRPUSD, SOLUSD, AMZN, TSLA, AAPL, MSFT)
- Regimen de mercado (ALCISTA/BAJISTA/LATERAL) via regime_detector.py
- sizing_mult por regimen pasado a client.open_position
- Fix: client.security -> client.x_token
- START_TRADING_UTC: no abre posiciones antes del lunes 2026-06-23 15:00 UTC
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
from scanner import run_scanner, COOLDOWN_VELAS
import regime_detector

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
client = CapitalClient()

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

# No abrir posiciones nuevas antes de esta fecha/hora
# Protege posiciones pre-v3 que Matias cierra manualmente
START_TRADING_UTC = datetime(2026, 6, 23, 15, 0, 0, tzinfo=timezone.utc)

scanner_state  = {}
scanner_lock   = threading.Lock()
last_scan_time = None
scan_errors    = {}

own_positions      = {}
own_positions_lock = threading.Lock()

cooldown_until      = {}
cooldown_until_lock = threading.Lock()

COOLDOWN_DURATION = timedelta(minutes=COOLDOWN_VELAS * 15)

# Regimenes por activo — se actualiza una vez por dia
current_regimes      = {}
current_regimes_lock = threading.Lock()
last_regime_update   = None


def _now_utc():
    return datetime.now(timezone.utc)


def _update_regimes_if_needed():
    """Actualiza regimenes de mercado una vez por dia."""
    global last_regime_update
    today = _now_utc().strftime("%Y-%m-%d")
    if last_regime_update == today:
        return
    try:
        regimes = regime_detector.fetch_and_update(client, CAPITAL_EPICS)
        with current_regimes_lock:
            current_regimes.clear()
            current_regimes.update(regimes)
        last_regime_update = today
        logger.info(f"[main] Regimenes actualizados: {regimes}")
    except Exception as e:
        logger.warning(f"[main] No se pudo actualizar regimenes: {e}")


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

    # Actualizar regimenes una vez al dia
    _update_regimes_if_needed()

    try:
        data_15m, data_4h = get_all_ohlcv(client)
    except Exception as e:
        logger.error(f"[main] Error descargando datos: {e}")
        return

    valid_syms = {sym for sym, rows in data_15m.items() if rows is not None}
    if not valid_syms:
        logger.warning("[main] CICLO ABORTADO - sin datos validos. Posiciones protegidas.")
        return

    logger.info(f"[main] Datos 15m validos: {len(valid_syms)}/9 activos")

    with own_positions_lock:
        open_pos_set = set(own_positions.keys())
    with cooldown_until_lock:
        cd_snapshot = dict(cooldown_until)
    with current_regimes_lock:
        regimes_snapshot = dict(current_regimes)

    try:
        results = run_scanner(data_15m, data_4h,
                              open_positions=open_pos_set,
                              cooldown_until=cd_snapshot,
                              regimes=regimes_snapshot)
    except Exception as e:
        logger.error(f"[main] Error en scanner: {e}")
        return

    with scanner_lock:
        scanner_state.clear()
        scanner_state.update(results)
        last_scan_time = datetime.utcnow().isoformat() + "Z"

    now = _now_utc()
    trading_habilitado = now >= START_TRADING_UTC
    if not trading_habilitado:
        remaining = int((START_TRADING_UTC - now).total_seconds() / 3600)
        logger.info(
            f"[main] START_TRADING_UTC no alcanzado — no se abren posiciones nuevas "
            f"({remaining}h restantes)"
        )

    for sym, res in results.items():
        if sym not in valid_syms:
            logger.info(f"[main] {sym}: sin datos - posicion protegida")
            continue
        signal = res.get("signal", "ESPERAR")
        score  = res.get("score", 0)
        try:
            if signal == "ESPERAR":
                with own_positions_lock:
                    deal_id = own_positions.get(sym)
                if deal_id:
                    try:
                        client.close_position(deal_id)
                        with own_positions_lock:
                            own_positions.pop(sym, None)
                        logger.info(f"[main] {sym}: cerrado deal={deal_id} (ESPERAR, score={score})")
                    except Exception as e:
                        logger.error(f"[main] {sym}: error cerrando deal {deal_id}: {e}")
                        if "404" in str(e) or "not found" in str(e).lower():
                            with own_positions_lock:
                                own_positions.pop(sym, None)
                else:
                    logger.debug(f"[main] {sym}: ESPERAR - sin posicion propia")

            elif signal in ("LONG", "SHORT"):
                if not trading_habilitado:
                    continue

                with own_positions_lock:
                    if sym in own_positions:
                        logger.info(f"[main] {sym}: {signal} - posicion propia ya abierta")
                        continue

                entry = res.get("entry", 0)
                sl    = res.get("sl", 0)
                tp1   = res.get("tp1", 0)
                if entry and sl and tp1:
                    # Sizing ajustado por regimen
                    sizing_mult = regime_detector.get_params(sym).get("sizing_mult", 1.0)
                    deal = client.open_position(
                        symbol=sym, action=signal,
                        entry=entry, sl=sl, tp1=tp1,
                        score=score, sizing_mult=sizing_mult
                    )
                    if deal is None:
                        continue
                    if deal:
                        deal_id = (
                            deal.get("dealId") or
                            deal.get("dealReference") or
                            deal.get("affectedDeals", [{}])[0].get("dealId")
                        )
                        if deal_id:
                            with own_positions_lock:
                                own_positions[sym] = deal_id
                            logger.info(
                                f"[main] {sym}: {signal} @ {entry} "
                                f"SL={sl} TP={tp1} ATR={res.get('atr','?')} "
                                f"score={score} regime={res.get('regime','?')} "
                                f"sizing_mult={sizing_mult} deal={deal_id}"
                            )
                        else:
                            logger.warning(f"[main] {sym}: posicion abierta sin dealId: {deal}")

        except Exception as e:
            err_str = str(e).lower()
            if "position" in err_str and ("closed" in err_str or "not found" in err_str or "404" in err_str):
                with own_positions_lock:
                    own_positions.pop(sym, None)
                with cooldown_until_lock:
                    cooldown_until[sym] = _now_utc() + COOLDOWN_DURATION
                logger.info(f"[main] {sym}: SL dtectado - cooldown activado")
            scan_errors[sym] = str(e)
            logger.error(f"[main] {sym}: {e}\n{traceback.format_exc()}")

    with cooldown_until_lock:
        now = _now_utc()
        vencidos = [s for s, t in cooldown_until.items() if now >= t]
        for s in vencidos:
            del cooldown_until[s]
            logger.info(f"[main] {s}: cooldown vencido - activo nuevamente")


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
    logger.info("[main] Scheduler activo - ciclo cada 5 minutos.")


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
        pos_copy = dict(own_positions)
    with cooldown_until_lock:
        cd_copy = {s: t.isoformat() for s, t in cooldown_until.items()}
    with current_regimes_lock:
        reg_copy = dict(current_regimes)
    now = _now_utc()
    return jsonify({
        "status":               "ok",
        "bot":                  "Bot Scalper v3",
        "activos":              9,
        "last_scan":            last_scan_time,
        "signals":              n,
        "own_positions":        pos_copy,
        "cooldowns":            cd_copy,
        "regimes":              reg_copy,
        "trading_habilitado":   now >= START_TRADING_UTC,
        "start_trading_utc":    START_TRADING_UTC.isoformat(),
    }), 200


@app.route("/signals", methods=["GET"])
def signals():
    with scanner_lock:
        state_copy = dict(scanner_state)
    with own_positions_lock:
        pos_copy = dict(own_positions)
    with cooldown_until_lock:
        cd_copy = {s: t.isoformat() for s, t in cooldown_until.items()}
    with current_regimes_lock:
        reg_copy = dict(current_regimes)
    return jsonify({
        "last_scan":     last_scan_time,
        "signals":       state_copy,
        "errors":        scan_errors,
        "own_positions": pos_copy,
        "cooldowns":     cd_copy,
        "regimes":       reg_copy,
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
        activities = client.get_activity_history(days=90)
        with own_positions_lock:
            pos_copy = dict(own_positions)
        with current_regimes_lock:
            reg_copy = dict(current_regimes)
        return jsonify({
            "bot":           "Bot Scalper v3",
            "own_positions": pos_copy,
            "positions":     positions,
            "accounts":      accounts,
            "activities":    activities,
            "last_scan":     last_scan_time,
            "regimes":       reg_copy,
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
