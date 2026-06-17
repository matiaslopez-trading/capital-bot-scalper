"""
main.py — Bot Scalper
Flask + APScheduler. Ciclo cada 5 minutos.
10 activos en 15min con bias de 4H.
PROTECCION: si datos fallan, ciclo abortado. Posiciones protegidas.
"""

import os
import json
import logging
import threading
import traceback
import time
from datetime import datetime

from flask import Flask, request, jsonify
from capital_client import CapitalClient
from apscheduler.schedulers.background import BackgroundScheduler
from data_feed import get_all_ohlcv
from scanner import run_scanner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app    = Flask(__name__)
client = CapitalClient()

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

scanner_state  = {}
scanner_lock   = threading.Lock()
last_scan_time = None
scan_errors    = {}


def run_cycle():
    """
    Ciclo principal cada 5 minutos.
    Descarga 15min + 4H, corre scanner, ejecuta trades.
    Si no hay datos validos, aborta sin tocar posiciones.
    """
    global last_scan_time

    # Re-login si tokens vacios
    try:
        if not client.cst or not client.security:
            logger.info("[main] Re-login necesario...")
            client.login()
            time.sleep(2)
    except Exception as e:
        logger.error(f"[main] Error en login: {e}")
        return

    # Descargar datos
    try:
        data_15m, data_4h = get_all_ohlcv(client)
    except Exception as e:
        logger.error(f"[main] Error descargando datos: {e}")
        return

    # GUARDIA: si ningun activo tiene datos 15m validos, abortar
    valid_syms = {sym for sym, rows in data_15m.items() if rows is not None}
    if not valid_syms:
        logger.warning(
            "[main] CICLO ABORTADO — sin datos validos. "
            "Posiciones protegidas sin cambios."
        )
        return

    logger.info(f"[main] Datos 15m validos: {len(valid_syms)}/10 activos")

    # Correr scanner
    try:
        results = run_scanner(data_15m, data_4h)
    except Exception as e:
        logger.error(f"[main] Error en scanner: {e}")
        return

    # Actualizar estado
    with scanner_lock:
        scanner_state.clear()
        scanner_state.update(results)
        last_scan_time = datetime.utcnow().isoformat() + "Z"

    # Ejecutar trades
    for sym, res in results.items():
        if sym not in valid_syms:
            logger.info(f"[main] {sym}: sin datos — posicion protegida")
            continue
        signal = res.get("signal", "ESPERAR")
        score  = res.get("score", 0)
        try:
            if signal == "ESPERAR":
                closed = client.close_all(sym)
                if closed:
                    logger.info(f"[main] {sym}: cerrado (ESPERAR, score={score})")
            elif signal in ("LONG", "SHORT"):
                entry = res.get("entry", 0)
                sl    = res.get("sl", 0)
                tp1   = res.get("tp1", 0)
                if entry and sl and tp1:
                    deal = client.open_position(
                        symbol=sym, action=signal,
                        entry=entry, sl=sl, tp1=tp1
                    )
                    if deal:
                        logger.info(
                            f"[main] {sym}: {signal} @ {entry} "
                            f"SL={sl} TP={tp1} score={score}"
                        )
        except Exception as e:
            scan_errors[sym] = str(e)
            logger.error(f"[main] {sym}: {e}\n{traceback.format_exc()}")


def start_scheduler():
    retries = 0
    while not client.cst and retries < 10:
        logger.info(f"[main] Esperando login... {retries+1}/10")
        time.sleep(3)
        retries += 1

    if not client.cst:
        logger.error("[main] Login fallido. Scheduler no iniciado.")
        return

    logger.info("[main] Login OK. Lanzando primer ciclo...")
    threading.Thread(target=run_cycle, daemon=True).start()

    scheduler = BackgroundScheduler(daemon=True)
    scheduler.add_job(run_cycle, "interval", minutes=5, id="scalper_cycle")
    scheduler.start()
    logger.info("[main] Scheduler activo — ciclo cada 5 minutos.")


# ── Endpoints ──

@app.route("/", methods=["GET"])
def health():
    with scanner_lock:
        n = sum(1 for r in scanner_state.values() if r.get("signal") in ("LONG","SHORT"))
    return jsonify({
        "status":    "ok",
        "bot":       "Bot Scalper v1",
        "activos":   10,
        "last_scan": last_scan_time,
        "signals":   n,
    }), 200


@app.route("/signals", methods=["GET"])
def signals():
    with scanner_lock:
        state_copy = dict(scanner_state)
    return jsonify({
        "last_scan": last_scan_time,
        "signals":   state_copy,
        "errors":    scan_errors,
    }), 200


@app.route("/scan", methods=["GET"])
def scan_now():
    t = threading.Thread(target=run_cycle, daemon=True)
    t.start()
    t.join(timeout=120)
    with scanner_lock:
        state_copy = dict(scanner_state)
    return jsonify({
        "last_scan": last_scan_time,
        "signals":   state_copy,
        "errors":    scan_errors,
    }), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = request.get_json(force=True)
        logger.info(f"[webhook] {json.dumps(payload)}")
        threading.Thread(target=run_cycle, daemon=True).start()
        return jsonify({"status": "ok"}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Inicio ──
try:
    client.login()
    logger.info("[main] Login inicial OK.")
except Exception as e:
    logger.error(f"[main] Error login inicial: {e}")

threading.Thread(target=start_scheduler, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
