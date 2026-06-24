"""
main.py — Bot Scalper v5
Flask + APScheduler. Ciclo cada 5 minutos.
9 activos en 15min con bias 4H.

Cambios v5 vs v4:
- Estrategia RSI 14 plano (reemplaza IFTRSI)
- Entrada: RSI cruza >30 (LONG) o <70 (SHORT)
- Exit dinámico: cierra posición cuando RSI llega a 50
- Filtro 4H: tendencia alcista=solo LONG, bajista=solo SHORT, neutral=ambos
- Trailing stop: se mantiene igual (BE al 25% TP, lock25% al 50% TP)
- score fijo = 2 para todas las entradas
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

app    = Flask(__name__)
client = CapitalClient()

WEBHOOK_SECRET   = os.environ.get("WEBHOOK_SECRET", "")
COOLDOWN_DURATION = timedelta(minutes=COOLDOWN_VELAS * 15)

# Estado del scanner
scanner_state  = {}
scanner_lock   = threading.Lock()
last_scan_time = None
scan_errors    = {}

# Posiciones propias: {sym: deal_id}
own_positions      = {}
own_positions_lock = threading.Lock()

# Cooldowns: {sym: datetime_utc}
cooldown_until      = {}
cooldown_until_lock = threading.Lock()

# Trailing stop tracking: {deal_id: {"be_done": bool, "lock25_done": bool}}
trailing_state      = {}
trailing_state_lock = threading.Lock()


def _now_utc():
    return datetime.now(timezone.utc)


# ── Gestión de posiciones abiertas ────────────────────────────────────────────

def _manage_open_positions(positions_api, signals):
    """
    Recorre las posiciones abiertas y aplica:
    1. Exit dinámico por RSI=50 (salida cuando llega a la mitad del indicador)
    2. Trailing stop: BE al 25% del TP, luego +25% al 50% del TP
    """
    now = _now_utc()

    # Mapa epic -> sym para lookups inversos
    from capital_client import SYMBOL_MAP
    epic_to_sym = {v: k for k, v in SYMBOL_MAP.items()}

    with own_positions_lock:
        own_pos_copy = dict(own_positions)

    for pos in positions_api:
        try:
            market   = pos.get("market", {})
            position = pos.get("position", {})

            epic      = market.get("epic", "")
            sym       = epic_to_sym.get(epic)
            if not sym:
                continue

            deal_id   = position.get("dealId", "")
            direction = position.get("direction", "")   # "BUY" o "SELL"
            pnl       = float(position.get("unrealisedPnl", 0) or 0)
            entry     = float(position.get("level", 0) or 0)
            sl        = float(position.get("stopLevel", 0) or 0)
            tp        = float(position.get("limitLevel", 0) or 0)
            size      = float(position.get("size", 0) or 0)

            # Solo gestionamos posiciones que abrimos nosotros
            if own_pos_copy.get(sym) != deal_id:
                continue

            # RSI actual de esta vela
            sig      = signals.get(sym, {})
            rsi_curr = sig.get("rsi", 50)
            rsi_exit = sig.get("rsi_exit", 50)   # siempre 50

            # ── 1. Exit dinámico por RSI=50 ───────────────────────────────
            should_exit = False
            if direction == "BUY"  and rsi_curr >= rsi_exit:
                logger.info(
                    f"[manage] {sym} LONG: RSI={rsi_curr:.1f} >= {rsi_exit} "
                    f"→ cerrando por RSI medio (PnL={pnl:+.2f})"
                )
                should_exit = True
            elif direction == "SELL" and rsi_curr <= rsi_exit:
                logger.info(
                    f"[manage] {sym} SHORT: RSI={rsi_curr:.1f} <= {rsi_exit} "
                    f"→ cerrando por RSI medio (PnL={pnl:+.2f})"
                )
                should_exit = True

            if should_exit:
                try:
                    client.close_position(deal_id)
                    with own_positions_lock:
                        own_positions.pop(sym, None)
                    with trailing_state_lock:
                        trailing_state.pop(deal_id, None)
                    with cooldown_until_lock:
                        cooldown_until[sym] = now + COOLDOWN_DURATION
                    logger.info(f"[manage] {sym}: cerrado por IFTRSI exit, cooldown activado")
                except Exception as e:
                    logger.error(f"[manage] {sym}: error cerrando {deal_id}: {e}")
                    if "404" in str(e) or "not found" in str(e).lower():
                        with own_positions_lock:
                            own_positions.pop(sym, None)
                        with trailing_state_lock:
                            trailing_state.pop(deal_id, None)
                    continue

            # ── 2. Trailing stop ───────────────────────────────────────────
            # Solo si hay ganancia y tenemos SL y TP configurados
            if pnl <= 0 or entry <= 0 or sl <= 0 or tp <= 0 or size <= 0:
                continue

            tp_dist_price = abs(tp - entry)     # distancia total al TP en precio
            if tp_dist_price == 0:
                continue

            tp_usd = tp_dist_price * size        # ganancia máxima esperada en USD

            with trailing_state_lock:
                ts = trailing_state.setdefault(deal_id, {"be_done": False, "lock25_done": False})
                be_done    = ts["be_done"]
                lock25_done = ts["lock25_done"]

            # Nivel 1: al 25% del TP → mover SL a breakeven (+pequeño buffer)
            if not be_done and pnl >= tp_usd * 0.25:
                if direction == "BUY":
                    # BE = entry + 0.5% de buffer para evitar ruido
                    new_sl = round(entry * 1.001, 5)
                    if new_sl > sl:
                        result = client.update_sl(deal_id, new_sl)
                        if result is not None:
                            with trailing_state_lock:
                                trailing_state[deal_id]["be_done"] = True
                            logger.info(
                                f"[manage] {sym} LONG trailing: BE → SL={new_sl} "
                                f"(PnL={pnl:+.2f} / TP_usd={tp_usd:.2f})"
                            )
                else:  # SELL
                    new_sl = round(entry * 0.999, 5)
                    if sl == 0 or new_sl < sl:
                        result = client.update_sl(deal_id, new_sl)
                        if result is not None:
                            with trailing_state_lock:
                                trailing_state[deal_id]["be_done"] = True
                            logger.info(
                                f"[manage] {sym} SHORT trailing: BE → SL={new_sl} "
                                f"(PnL={pnl:+.2f} / TP_usd={tp_usd:.2f})"
                            )

            # Nivel 2: al 50% del TP → bloquear 25% de ganancia
            if be_done and not lock25_done and pnl >= tp_usd * 0.50:
                if direction == "BUY":
                    new_sl = round(entry + tp_dist_price * 0.25, 5)
                    if new_sl > sl:
                        result = client.update_sl(deal_id, new_sl)
                        if result is not None:
                            with trailing_state_lock:
                                trailing_state[deal_id]["lock25_done"] = True
                            logger.info(
                                f"[manage] {sym} LONG trailing: lock25% → SL={new_sl} "
                                f"(PnL={pnl:+.2f} / TP_usd={tp_usd:.2f})"
                            )
                else:  # SELL
                    new_sl = round(entry - tp_dist_price * 0.25, 5)
                    if sl == 0 or new_sl < sl:
                        result = client.update_sl(deal_id, new_sl)
                        if result is not None:
                            with trailing_state_lock:
                                trailing_state[deal_id]["lock25_done"] = True
                            logger.info(
                                f"[manage] {sym} SHORT trailing: lock25% → SL={new_sl} "
                                f"(PnL={pnl:+.2f} / TP_usd={tp_usd:.2f})"
                            )

        except Exception as e:
            logger.error(f"[manage] Error procesando posicion {pos}: {e}\n{traceback.format_exc()}")


# ── Ciclo principal ────────────────────────────────────────────────────────────

def run_cycle():
    global last_scan_time

    # Login si hace falta
    try:
        if not client.cst or not client.x_token:
            logger.info("[main] Re-login necesario...")
            client.login()
            time.sleep(2)
    except Exception as e:
        logger.error(f"[main] Error en login: {e}")
        return

    # Descargar datos 15m + 4H
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

    # Escanear señales RSI
    try:
        results = run_scanner(
            data_15m, data_4h,
            open_positions=open_pos_set,
            cooldown_until=cd_snapshot,
        )
    except Exception as e:
        logger.error(f"[main] Error en scanner: {e}")
        return

    with scanner_lock:
        scanner_state.clear()
        scanner_state.update(results)
        last_scan_time = datetime.utcnow().isoformat() + "Z"

    # ── Gestionar posiciones abiertas (exit dinámico + trailing) ──────────────
    try:
        positions_api = client.get_positions()
        if positions_api:
            _manage_open_positions(positions_api, results)
    except Exception as e:
        logger.warning(f"[main] No se pudo obtener posiciones para gestionar: {e}")

    # ── Abrir nuevas posiciones ────────────────────────────────────────────────
    for sym, res in results.items():
        if sym not in valid_syms:
            logger.info(f"[main] {sym}: sin datos - posicion protegida")
            continue

        signal = res.get("signal", "ESPERAR")

        try:
            if signal in ("LONG", "SHORT"):

                with own_positions_lock:
                    if sym in own_positions:
                        logger.info(f"[main] {sym}: {signal} - posicion propia ya abierta")
                        continue

                entry = res.get("entry", 0)
                sl    = res.get("sl", 0)
                tp1   = res.get("tp1", 0)
                score = 2  # RSI crossover → score fijo

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
                        with trailing_state_lock:
                            trailing_state[deal_id] = {"be_done": False, "lock25_done": False}
                        logger.info(
                            f"[main] {sym}: {signal} @ {entry} "
                            f"SL={sl} TP={tp1} ATR={res.get('atr','?')} "
                            f"RSI={res.get('rsi','?')} bias4h={res.get('bias_4h','?')} "
                            f"deal={deal_id}"
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
                logger.info(f"[main] {sym}: SL/cierre externo detectado - cooldown activado")
            scan_errors[sym] = str(e)
            logger.error(f"[main] {sym}: {e}\n{traceback.format_exc()}")

    # Limpiar cooldowns vencidos
    with cooldown_until_lock:
        now = _now_utc()
        vencidos = [s for s, t in cooldown_until.items() if now >= t]
        for s in vencidos:
            del cooldown_until[s]
            logger.info(f"[main] {s}: cooldown vencido - activo nuevamente")


# ── Scheduler ─────────────────────────────────────────────────────────────────

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


# ── Endpoints Flask ────────────────────────────────────────────────────────────

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
    with trailing_state_lock:
        ts_copy = dict(trailing_state)
    return jsonify({
        "status":             "ok",
        "bot":                "Bot Scalper v5 (RSI14)",
        "activos":            9,
        "last_scan":          last_scan_time,
        "signals":            n,
        "own_positions":      pos_copy,
        "cooldowns":          cd_copy,
        "trailing_state":     ts_copy,
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
    return jsonify({
        "last_scan":     last_scan_time,
        "signals":       state_copy,
        "errors":        scan_errors,
        "own_positions": pos_copy,
        "cooldowns":     cd_copy,
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
            pos_copy = dict(own_positions)
        with trailing_state_lock:
            ts_copy = dict(trailing_state)
        return jsonify({
            "bot":            "Bot Scalper v5 (RSI14)",
            "own_positions":  pos_copy,
            "trailing_state": ts_copy,
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


# ── Inicio ─────────────────────────────────────────────────────────────────────

try:
    client.login()
    logger.info("[main] Login inicial OK.")
except Exception as e:
    logger.error(f"[main] Error login inicial: {e}")

threading.Thread(target=start_scheduler, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
