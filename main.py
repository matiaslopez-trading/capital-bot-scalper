"""
main.py — Bot Scalper v7.5
Flask + APScheduler. Datos y RSI en velas de 5 minutos, pero el ciclo
de escaneo corre cada 1 minuto — reacciona a la vela de 5min todavía en
formación en vez de esperar a que cierre. Esto reduce la latencia de
reacción sin achicar el ATR (y por lo tanto sin que el spread se coma
una porción mayor de cada operación, como sí pasaría usando velas de 1min).
9 activos, mean-reversion pura (sin filtro de tendencia 4H).

v7.3 (20/07/2026): salida por PnL fijo en dólares. Analizando el CSV de
170 operaciones del 20/07 se vio que la pérdida promedio (-$7.73) fue
bastante mayor que la ganancia promedio ($5.62) — el SL nativo (ATR)
deja correr las pérdidas mucho más que lo que se deja correr a las
ganancias antes de un early-exit. Ahora, en cada ciclo (cada 1 min) se
chequea el PnL no realizado de cada posición: si llega a +FIXED_TP_USD
o a -FIXED_SL_USD, se cierra al instante. El SL/TP nativo de Capital.com
(ATR based) queda como respaldo de emergencia únicamente (por si el bot
se cae o Railway reinicia entre ciclos) — en operación normal, el cierre
por PnL fijo dispara primero casi siempre.

v7.4 (20/07/2026): panel manual on/off en GET /panel (boton) y POST
/panel/toggle. Pausar deja de abrir posiciones NUEVAS pero sigue
gestionando/cerrando las que ya estaban abiertas. Pensado para que
Matias pueda apagarlo antes de dormir sin dejar operaciones sueltas.
El estado vive en memoria (se resetea a activo en cada redeploy).

v7.5 (20/07/2026): dashboard visual en GET /dashboard. Matias pidió
poder ver en tiempo real que tan cerca esta cada activo de disparar
una señal (en vez de leer los logs crudos de Railway) y chequear
visualmente si algo esta fallando. La pagina hace fetch a / y /signals
cada 5s (datos en memoria, sin pegarle a la API de Capital.com) y a
/stats cada 20s (para el PnL en vivo de posiciones abiertas, que si
pega a Capital.com - por eso mas espaciado). Incluye semaforo de salud
basado en hace cuanto corrio el ultimo scan, gauge de RSI 0-100 por
activo con las zonas de sobrecompra/sobreventa marcadas, y el mismo
boton de pausar/reanudar que /panel.

Objetivo (mandato del usuario): MUCHAS operaciones de calidad por día.
No importa long o short — lo que importa es que haya más aciertos que
desaciertos. Hasta 2 posiciones simultáneas por activo.

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

# v7.3: techo de PnL fijo en dolares por operacion (ver docstring arriba)
FIXED_TP_USD = 2.0
FIXED_SL_USD = 2.0

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

# v7.4: interruptor manual on/off. Cuando esta en False, el bot deja de
# ABRIR posiciones nuevas, pero sigue gestionando (cerrando por +-$2,
# RSI o time-stop) las que ya estaban abiertas - para que Matias pueda
# apagarlo antes de dormir sin dejar operaciones sueltas sin vigilar.
# Vive solo en memoria: si Railway redeploya, vuelve a quedar en True.
trading_enabled      = True
trading_enabled_lock = threading.Lock()


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
            # v7.3b FIX: la API de Capital.com devuelve el PnL no realizado
            # en el campo "upl" (no "unrealisedPnl") y el TP en "profitLevel"
            # (no "limitLevel"). Con los nombres viejos, pnl y tp SIEMPRE se
            # leian como 0 -> el techo de PnL fijo nunca se disparaba y el
            # breakeven trailing tampoco (por eso se acumularon 4 posiciones
            # de AMZN sin gestionar). Se mantiene el nombre viejo como
            # fallback por si la API cambia el shape en el futuro.
            pnl      = float(position.get("upl", position.get("unrealisedPnl", 0)) or 0)
            entry    = float(position.get("level", 0) or 0)
            sl       = float(position.get("stopLevel", 0) or 0)
            tp       = float(position.get("profitLevel", position.get("limitLevel", 0)) or 0)
            size     = float(position.get("size", 0) or 0)

            should_exit = False
            exit_reason = ""

            # v7.3: prioridad maxima - techo de PnL fijo en dolares.
            # Se chequea primero porque es el criterio de salida principal;
            # el resto (time-stop, RSI neutral) son fallback para posiciones
            # que quedan flotando sin tocar ninguno de los dos techos.
            if pnl >= FIXED_TP_USD:
                should_exit = True
                exit_reason = f"TP fijo (${pnl:.2f} >= ${FIXED_TP_USD:.2f})"
            elif pnl <= -FIXED_SL_USD:
                should_exit = True
                exit_reason = f"SL fijo (${pnl:.2f} <= -${FIXED_SL_USD:.2f})"
            elif bars >= TIME_STOP_BARS:
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

    with trading_enabled_lock:
        puede_abrir = trading_enabled
    if not puede_abrir:
        logger.info("[main] Trading pausado manualmente - no se abren posiciones nuevas (se siguen gestionando las existentes).")
        return

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

            # v7.3: el cooldown se calcula ANTES de gestionar posiciones
            # (cd_snapshot), asi que si esta misma posicion se acaba de
            # cerrar arriba en _manage_open_positions() (por el nuevo TP/SL
            # fijo, por RSI o por time-stop), el snapshot todavia no lo
            # reflejaba y el simbolo podria reabrirse en el acto. Chequeo
            # el cooldown en vivo aca para evitar reentrar sin descanso.
            with cooldown_until_lock:
                cd_live = cooldown_until.get(sym)
            if cd_live and now < cd_live:
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


def _reconcile_positions():
    """
    v7.3b: al reiniciar (cada redeploy en Railway), own_positions arranca
    vacio en memoria, pero las posiciones siguen abiertas en Capital.com.
    Sin esto, el bot pierde el rastro y puede seguir abriendo posiciones
    nuevas del mismo simbolo sin respetar MAX_POS_PER_SYM (paso lo que
    causo 4 posiciones de AMZN abiertas a la vez el 20/07). Al arrancar,
    se importan las posiciones ya abiertas que correspondan a un simbolo
    del Scalper.
    """
    from capital_client import SYMBOL_MAP
    epic_to_sym = {v: k for k, v in SYMBOL_MAP.items()}
    try:
        positions_api = client.get_positions()
    except Exception as e:
        logger.error(f"[reconcile] No se pudo leer posiciones: {e}")
        return
    imported = 0
    for pos in positions_api or []:
        epic = pos.get("market", {}).get("epic")
        sym  = epic_to_sym.get(epic)
        if not sym:
            continue  # posicion de otro bot (ej. Bot Swing) - se ignora
        position = pos.get("position", {})
        deal_id  = position.get("dealId")
        if not deal_id:
            continue
        created_str = position.get("createdDateUTC")
        try:
            open_time = datetime.fromisoformat(created_str.replace("Z", "+00:00"))
            if open_time.tzinfo is None:
                open_time = open_time.replace(tzinfo=timezone.utc)
        except Exception:
            open_time = _now_utc()
        with own_positions_lock:
            existing = own_positions.setdefault(sym, [])
            if any(e["deal_id"] == deal_id for e in existing):
                continue
            existing.append({
                "deal_id":   deal_id,
                "direction": position.get("direction", "BUY"),
                "open_time": open_time,
                "entry":     float(position.get("level", 0) or 0),
                "be_done":   False,
            })
        imported += 1
    if imported:
        logger.warning(f"[reconcile] {imported} posicion(es) importadas desde Capital.com al arrancar.")
    else:
        logger.info("[reconcile] Sin posiciones huerfanas para importar.")


def start_scheduler():
    retries = 0
    while not client.cst and retries < 10:
        logger.info(f"[main] Esperando login... {retries+1}/10")
        time.sleep(3)
        retries += 1
    if not client.cst:
        logger.error("[main] Login fallido. Scheduler no iniciado.")
        return
    logger.info("[main] Login confirmado. Reconciliando posiciones...")
    _reconcile_positions()
    logger.info("[main] Lanzando primer ciclo...")
    threading.Thread(target=run_cycle, daemon=True).start()
    scheduler = BackgroundScheduler(daemon=True)
    # Ciclo cada 1 minuto: reacciona a la vela de 5min en formacion sin
    # esperar a que cierre. El RSI/ATR siguen calculados sobre velas de
    # 5min (CANDLE_MINUTES no cambia) - solo se reduce la latencia de reaccion.
    scheduler.add_job(run_cycle, "interval", minutes=1, id="scalper_cycle")
    scheduler.start()
    logger.info("[main] Scheduler activo - ciclo cada 1 minuto (RSI en velas de 5min).")


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
    with trading_enabled_lock:
        habilitado = trading_enabled
    return jsonify({
        "status":             "ok",
        "bot":                "Bot Scalper v7 (mean-reversion 5min, alto volumen)",
        "activos":            len(CAPITAL_EPICS),
        "last_scan":          last_scan_time,
        "signals_activos":    n,
        "own_positions":      pos_copy,
        "total_posiciones":   total_pos,
        "cooldowns":          cd_copy,
        "trading_habilitado": habilitado,
        "cuenta":             "DEMO (dinero ficticio)",
    }), 200


@app.route("/panel", methods=["GET"])
def panel():
    """
    v7.4: panel simple con un boton para pausar/reanudar la apertura de
    posiciones nuevas (ej. antes de dormir). Cuando esta pausado, el bot
    sigue gestionando y cerrando lo que ya tenia abierto (por +-$2, RSI
    o time-stop) - solo deja de abrir posiciones nuevas.
    """
    with trading_enabled_lock:
        habilitado = trading_enabled
    with own_positions_lock:
        total_pos = sum(len(v) for v in own_positions.values())
    color  = "#1f9d55" if habilitado else "#c53030"
    texto  = "ACTIVO — abriendo operaciones" if habilitado else "PAUSADO — no abre operaciones nuevas"
    accion = "pausar" if habilitado else "reanudar"
    boton_txt = "Pausar bot" if habilitado else "Reanudar bot"
    html = f"""
    <html>
    <head>
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Bot Scalper — Panel</title>
      <style>
        body {{ font-family: -apple-system, sans-serif; background:#111; color:#eee;
               display:flex; flex-direction:column; align-items:center; padding:40px 16px; }}
        .estado {{ font-size: 20px; font-weight: bold; color: {color}; margin-bottom: 24px; }}
        .info {{ color:#aaa; margin-bottom: 24px; }}
        button {{ font-size: 18px; padding: 16px 32px; border-radius: 10px; border: none;
                  background: {color}; color: white; font-weight: bold; }}
      </style>
    </head>
    <body>
      <h2>Bot Scalper</h2>
      <div class="estado">{texto}</div>
      <div class="info">Posiciones abiertas ahora: {total_pos}</div>
      <form method="POST" action="/panel/toggle">
        <button type="submit">{boton_txt}</button>
      </form>
      <p class="info" style="margin-top:24px;">
        Al pausar, el bot NO abre operaciones nuevas, pero sigue cerrando
        las que ya tenía abiertas (por +/-$2, RSI o time-stop).
      </p>
    </body>
    </html>
    """
    return html, 200


@app.route("/panel/toggle", methods=["POST", "GET"])
def panel_toggle():
    global trading_enabled
    with trading_enabled_lock:
        trading_enabled = not trading_enabled
        nuevo = trading_enabled
    logger.warning(f"[main] Trading {'REANUDADO' if nuevo else 'PAUSADO'} manualmente via /panel/toggle.")
    from flask import redirect
    return redirect("/panel", code=303)


DASHBOARD_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bot Scalper - Dashboard</title>
<style>
  * { box-sizing: border-box; }
  body { font-family: -apple-system, BlinkMacSystemFont, sans-serif; background:#0b0e14; color:#e6e6e6; margin:0; padding:16px; }
  .header { display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:12px; margin-bottom:20px; }
  .status { display:flex; align-items:center; gap:8px; font-size:15px; }
  .dot { width:10px; height:10px; border-radius:50%; display:inline-block; }
  .dot.ok { background:#2ecc71; box-shadow:0 0 8px #2ecc71; }
  .dot.warn { background:#f1c40f; box-shadow:0 0 8px #f1c40f; }
  .dot.bad { background:#e74c3c; box-shadow:0 0 8px #e74c3c; }
  button { font-size:14px; padding:10px 18px; border-radius:8px; border:none; font-weight:bold; cursor:pointer; }
  .btn-pause { background:#c53030; color:white; }
  .btn-resume { background:#1f9d55; color:white; }
  .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap:12px; margin-bottom:24px; }
  .card { background:#161b26; border-radius:12px; padding:14px; border:1px solid #232a3a; }
  .sym { font-weight:bold; font-size:15px; margin-bottom:6px; }
  .rsi-val { font-size:12px; color:#999; margin-bottom:8px; min-height:32px; }
  .gauge { position:relative; height:10px; border-radius:5px;
           background: linear-gradient(to right, #e74c3c 0%, #e74c3c 30%, #2a2f3d 30%, #2a2f3d 70%, #2ecc71 70%, #2ecc71 100%);
           margin-bottom:8px; }
  .marker { position:absolute; top:-4px; width:4px; height:18px; background:#fff; border-radius:2px; }
  .signal { font-size:13px; font-weight:bold; }
  .signal-LONG { color:#2ecc71; }
  .signal-SHORT { color:#e74c3c; }
  .signal-ESPERAR { color:#777; }
  .filtro { font-size:11px; color:#666; margin-top:4px; }
  h2 { font-size:15px; color:#ccc; margin: 24px 0 10px; }
  .pos-card { background:#161b26; border-radius:12px; padding:14px; margin-bottom:10px; border:1px solid #232a3a; }
  .pos-top { display:flex; justify-content:space-between; align-items:center; }
  .pnl { font-weight:bold; font-size:15px; }
  .pnl-pos { color:#2ecc71; }
  .pnl-neg { color:#e74c3c; }
  .pos-bar { position:relative; height:10px; border-radius:5px; background:#2a2f3d; margin-top:10px; overflow:hidden; }
  .pos-fill { position:absolute; height:100%; top:0; }
  .empty { color:#666; font-size:13px; }
  .updated { font-size:11px; color:#555; text-align:right; margin-top:20px; }
</style>
</head>
<body>
  <div class="header">
    <div class="status">
      <span class="dot" id="dot"></span>
      <span id="statusText">Cargando...</span>
    </div>
    <button id="toggleBtn" onclick="toggleTrading()">...</button>
  </div>

  <h2>Activos - RSI en vivo (5min)</h2>
  <div class="grid" id="assetsGrid"></div>

  <h2>Posiciones abiertas</h2>
  <div id="positionsList"><div class="empty">Cargando...</div></div>

  <div class="updated" id="updated"></div>

<script>
let livePnl = {};

async function fetchFast() {
  try {
    const [healthRes, signalsRes] = await Promise.all([fetch('/'), fetch('/signals')]);
    const health = await healthRes.json();
    const signals = await signalsRes.json();
    renderHealth(health);
    renderAssets(signals);
    renderPositions(signals, health);
  } catch (e) {
    document.getElementById('statusText').innerText = 'Error de conexion con el bot';
    document.getElementById('dot').className = 'dot bad';
  }
}

async function fetchSlow() {
  try {
    const res = await fetch('/stats');
    const stats = await res.json();
    livePnl = {};
    for (const p of (stats.positions || [])) {
      const dealId = p.position && p.position.dealId;
      if (dealId) livePnl[dealId] = p.position.upl;
    }
  } catch (e) {
    // /stats pega directo a Capital.com, puede fallar por rate limit - se ignora, se reintenta solo
  }
}

function timeAgoSec(iso) {
  if (!iso) return null;
  return (Date.now() - new Date(iso).getTime()) / 1000;
}

function renderHealth(health) {
  const secAgo = timeAgoSec(health.last_scan);
  const dot = document.getElementById('dot');
  const statusText = document.getElementById('statusText');
  if (secAgo === null) {
    dot.className = 'dot bad';
    statusText.innerText = 'Sin datos todavia';
  } else if (secAgo < 90) {
    dot.className = 'dot ok';
    statusText.innerText = 'Funcionando bien - ultimo scan hace ' + Math.round(secAgo) + 's';
  } else if (secAgo < 240) {
    dot.className = 'dot warn';
    statusText.innerText = 'Demorado - ultimo scan hace ' + Math.round(secAgo) + 's';
  } else {
    dot.className = 'dot bad';
    statusText.innerText = 'POSIBLE FALLA - ultimo scan hace ' + Math.round(secAgo / 60) + ' min';
  }

  const btn = document.getElementById('toggleBtn');
  if (health.trading_habilitado) {
    btn.innerText = 'Pausar bot';
    btn.className = 'btn-pause';
  } else {
    btn.innerText = 'Reanudar bot';
    btn.className = 'btn-resume';
  }
}

function distanciaTexto(rsi) {
  if (rsi === null || rsi === undefined) return '';
  if (rsi > 30 && rsi < 70) {
    const distLong = rsi - 30, distShort = 70 - rsi;
    if (distLong < distShort) return 'faltan ' + distLong.toFixed(1) + ' pts para sobreventa (long)';
    return 'faltan ' + distShort.toFixed(1) + ' pts para sobrecompra (short)';
  } else if (rsi <= 30) {
    return 'en zona de sobreventa';
  }
  return 'en zona de sobrecompra';
}

function renderAssets(signals) {
  const grid = document.getElementById('assetsGrid');
  grid.innerHTML = '';
  const sigs = signals.signals || {};
  const syms = Object.keys(sigs).sort();
  for (const sym of syms) {
    const s = sigs[sym];
    const rsi = (s.rsi !== undefined && s.rsi !== null) ? s.rsi : null;
    const markerLeft = rsi !== null ? Math.max(0, Math.min(100, rsi)) : 50;
    const rsiTxt = rsi !== null ? rsi.toFixed(1) : '-';
    const sig = s.signal || 'ESPERAR';
    const card = document.createElement('div');
    card.className = 'card';
    let extra = '';
    if (s.filtro) extra = '<div class="filtro">' + s.filtro + '</div>';
    if (s.error) extra = '<div class="filtro">error: ' + s.error + '</div>';
    card.innerHTML =
      '<div class="sym">' + sym + '</div>' +
      '<div class="rsi-val">RSI ' + rsiTxt + '<br>' + distanciaTexto(rsi) + '</div>' +
      '<div class="gauge"><div class="marker" style="left:calc(' + markerLeft + '% - 2px)"></div></div>' +
      '<div class="signal signal-' + sig + '">' + sig + '</div>' +
      extra;
    grid.appendChild(card);
  }
}

function renderPositions(signals) {
  const posList = document.getElementById('positionsList');
  posList.innerHTML = '';
  const ownPos = signals.own_positions || {};
  let any = false;
  for (const sym in ownPos) {
    for (const p of ownPos[sym]) {
      any = true;
      const pnl = livePnl[p.deal_id];
      const dir = p.direction === 'BUY' ? 'LONG' : 'SHORT';
      const card = document.createElement('div');
      card.className = 'pos-card';
      let pnlHtml = '<span class="pnl">sin dato aun</span>';
      let barHtml = '';
      if (pnl !== undefined && pnl !== null) {
        const cls = pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
        pnlHtml = '<span class="pnl ' + cls + '">' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + ' USD</span>';
        const pct = Math.max(-100, Math.min(100, (pnl / 2) * 100));
        const fillColor = pnl >= 0 ? '#2ecc71' : '#e74c3c';
        const left = pct >= 0 ? '50%' : (50 + pct / 2) + '%';
        const width = Math.abs(pct) / 2 + '%';
        barHtml = '<div class="pos-bar"><div class="pos-fill" style="left:' + left + ';width:' + width + ';background:' + fillColor + '"></div></div>';
      }
      card.innerHTML =
        '<div class="pos-top"><div class="sym">' + sym + ' ' + dir + '</div>' + pnlHtml + '</div>' +
        '<div class="rsi-val">Entrada ' + p.entry + ' - abierta ' + p.open_time + '</div>' +
        barHtml;
      posList.appendChild(card);
    }
  }
  if (!any) posList.innerHTML = '<div class="empty">Sin posiciones abiertas ahora.</div>';
  document.getElementById('updated').innerText = 'Actualizado ' + new Date().toLocaleTimeString();
}

async function toggleTrading() {
  await fetch('/panel/toggle', { method: 'POST' });
  fetchFast();
}

fetchFast();
fetchSlow();
setInterval(fetchFast, 5000);
setInterval(fetchSlow, 20000);
</script>
</body>
</html>
"""


@app.route("/dashboard", methods=["GET"])
def dashboard():
    return DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


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
