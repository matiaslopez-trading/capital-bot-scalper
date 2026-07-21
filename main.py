"""
main.py — Bot Scalper v7.10
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

v7.6 (20/07/2026): el dashboard suma al Bot Swing (via /swing-proxy,
lectura server-side de su endpoint publico - repos y deploys siguen
100% separados) y agrega numeros de escala (0/30/70/100) debajo de
cada gauge de RSI.

v7.7 (20/07/2026): dashboard reorganizado en dos bloques (Bot Scalper
arriba, Bot Swing abajo), cada uno con su lista de activos en filas
compactas en vez de tarjetas sueltas, y su propia seccion de
posiciones abiertas. El tracking interno del Bot Swing (own_positions)
pierde el rastro de sus posiciones en cada redeploy - las posiciones
del Swing que se muestran ahora se derivan de la cuenta compartida de
Capital.com (via /swing-proxy), filtrando por epics que no pertenecen
al universo de activos del Scalper.

v7.8 (20/07/2026): los dos bloques (Bot Scalper / Bot Swing) pasan a
mostrarse lado a lado en pantallas anchas (PC), y apilados uno encima
del otro en celular (media query, breakpoint 900px) - para que las
filas de activos no queden ilegibles apretadas en pantalla chica.

v7.9 (20/07/2026): endpoint /pnl con resumen de ganancias vs perdidas
(dia/semana/mes), combinado y separado por bot.

v7.10 (21/07/2026): universo de activos ampliado de 9 a 18. Se suman
9 criptomonedas (ADAUSD, LTCUSD, LINKUSD, DOTUSD, AVAXUSD, MATICUSD,
ATOMUSD, XLMUSD, BNBUSD) — epics y minDealSize verificados en vivo
contra la API de Capital.com antes de sumarlos. Objetivo: mas señales
de calidad por dia (Matias reporto solo 1 operacion cerrada desde los
ultimos cambios) y cobertura 24/7 fuera del horario de NYSE para los
activos de acciones/indice. No se toco el universo del Bot Swing
(BTCUSD, ETHUSD, etc. quedan exclusivos de ese bot).

v7.10.1 (21/07/2026): fix critico en capital_client.py — el tope de
sizing por posicion (15% del balance) y el guardrail de exposicion
maxima (10%) se contradecian: cada vez que el tope de 15% era el que
mandaba (SL ajustado, tipico en scalping), la exposicion resultante
quedaba fija en 15% del balance, que siempre supera el 10% del
guardrail y aborta la operacion. Esto probablemente bloqueo la gran
mayoria de las señales validas desde que se agrego el guardrail, no
solo en activos puntuales. Confirmado en vivo con un short real de
TSLA que se hubiera abortado. Fix: el tope de sizing ahora usa
MAX_EXPOSURE_PCT en vez de un 15% hardcodeado, para que ambos limites
sean siempre consistentes.

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
import requests
from datetime import datetime, timezone, timedelta

from flask import Flask, request, jsonify
from capital_client import CapitalClient
from apscheduler.schedulers.background import BackgroundScheduler
from data_feed import get_all_ohlcv, CAPITAL_EPICS
from scanner import run_scanner, COOLDOWN_VELAS, TIME_STOP_BARS, MAX_POS_PER_SYM

# v7.6: URL publica del Bot Swing (repo y deploy 100% separados - solo se
# lee su endpoint publico de solo-lectura para mostrarlo en el dashboard,
# nunca se llama nada que module su trading).
SWING_BASE_URL = "https://capital-bot-production-1cc5.up.railway.app"

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

  .two-col { display:flex; flex-direction:column; gap:20px; }
  .bloque { background:#10131c; border:1px solid #1e2430; border-radius:14px; padding:16px; margin-bottom:20px; }
  .bloque-header { display:flex; align-items:baseline; justify-content:space-between; margin-bottom:4px; }
  .bloque-title { font-size:17px; font-weight:bold; color:#fff; }
  .bloque-sub { font-size:11px; color:#666; margin-bottom:14px; }
  h3 { font-size:12px; text-transform:uppercase; letter-spacing:.05em; color:#667; margin:16px 0 8px; }

  /* v7.8: en pantallas anchas (PC) las dos columnas van lado a lado.
     En celular (pantalla angosta) se apilan una encima de la otra,
     para que las filas de activos no queden ilegibles apretadas. */
  @media (min-width: 900px) {
    .two-col { flex-direction: row; align-items: flex-start; }
    .two-col > .bloque { flex: 1 1 0; min-width: 0; margin-bottom: 0; }
  }

  .list { display:flex; flex-direction:column; gap:6px; }
  .row { display:flex; align-items:center; gap:14px; background:#161b26; border:1px solid #232a3a;
         border-radius:8px; padding:10px 14px; flex-wrap:wrap; }
  .row-sym { font-weight:bold; font-size:14px; width:66px; flex-shrink:0; }
  .row-rsi { font-size:13px; color:#ccc; width:44px; flex-shrink:0; }
  .row-gauge-wrap { flex:1 1 160px; min-width:140px; }
  .row-dist { font-size:10px; color:#777; margin-top:3px; }
  .row-signal { width:76px; flex-shrink:0; font-size:12px; font-weight:bold; text-align:center;
                border-radius:6px; padding:4px 0; }
  .row-note { font-size:11px; color:#666; flex-basis:100%; margin-top:2px; margin-left:80px; }

  .gauge { position:relative; height:8px; border-radius:4px;
           background: linear-gradient(to right, #e74c3c 0%, #e74c3c 30%, #2a2f3d 30%, #2a2f3d 70%, #2ecc71 70%, #2ecc71 100%); }
  .marker { position:absolute; top:-3px; width:3px; height:14px; background:#fff; border-radius:2px; }
  .scale { position:relative; height:12px; font-size:9px; color:#666; margin-top:2px; }
  .scale span { position:absolute; transform:translateX(-50%); }

  .signal-LONG { color:#0b0e14; background:#2ecc71; }
  .signal-SHORT { color:#0b0e14; background:#e74c3c; }
  .signal-ESPERAR { color:#888; background:transparent; }

  .pnl-grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap:10px; }
  .pnl-card { background:#161b26; border:1px solid #232a3a; border-radius:10px; padding:14px; text-align:center; }
  .pnl-card .plabel { font-size:11px; color:#888; text-transform:uppercase; letter-spacing:.04em; margin-bottom:6px; }
  .pnl-card .pnet { font-size:22px; font-weight:bold; }
  .pnl-card .pdetail { font-size:11px; color:#888; margin-top:6px; }
  .bot-pnl-line { font-size:11px; color:#888; margin: -6px 0 10px; }

  .prow { display:flex; align-items:center; gap:14px; background:#161b26; border:1px solid #232a3a;
          border-radius:8px; padding:10px 14px; flex-wrap:wrap; margin-bottom:6px; }
  .prow-sym { font-weight:bold; font-size:14px; width:110px; flex-shrink:0; }
  .prow-info { font-size:11px; color:#888; width:170px; flex-shrink:0; }
  .prow-bar-wrap { flex:1 1 140px; min-width:120px; }
  .pnl { font-weight:bold; font-size:14px; width:90px; flex-shrink:0; text-align:right; }
  .pnl-pos { color:#2ecc71; }
  .pnl-neg { color:#e74c3c; }
  .pos-bar { position:relative; height:8px; border-radius:4px; background:#2a2f3d; overflow:hidden; }
  .pos-fill { position:absolute; height:100%; top:0; }

  .empty { color:#666; font-size:13px; }
  .updated { font-size:11px; color:#555; text-align:right; margin-top:8px; }
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

  <div class="bloque">
    <div class="bloque-header"><div class="bloque-title">Resultado - operaciones cerradas (Scalper + Swing)</div></div>
    <div class="bloque-sub">No incluye posiciones todavia abiertas ni swaps/comisiones</div>
    <div class="pnl-grid" id="pnlGrid">
      <div class="pnl-card"><div class="plabel">Hoy</div><div class="pnet">-</div></div>
      <div class="pnl-card"><div class="plabel">Esta semana</div><div class="pnet">-</div></div>
      <div class="pnl-card"><div class="plabel">Este mes</div><div class="pnet">-</div></div>
    </div>
  </div>

  <div class="two-col">
    <div class="bloque">
      <div class="bloque-header"><div class="bloque-title">Bot Scalper</div></div>
      <div class="bloque-sub">Velas de 5 min - mean reversion RSI</div>
      <div class="bot-pnl-line" id="scalperPnlLine">Resultado hoy: cargando...</div>
      <h3>Activos</h3>
      <div class="list" id="assetsList"></div>
      <h3>Posiciones abiertas</h3>
      <div class="list" id="positionsList"><div class="empty">Cargando...</div></div>
    </div>

    <div class="bloque">
      <div class="bloque-header"><div class="bloque-title">Bot Swing</div></div>
      <div class="bloque-sub" id="swingStatus">Cargando...</div>
      <div class="bot-pnl-line" id="swingPnlLine">Resultado hoy: cargando...</div>
      <h3>Activos</h3>
      <div class="list" id="swingList"></div>
      <h3>Posiciones abiertas</h3>
      <div class="list" id="swingPositionsList"><div class="empty">Cargando...</div></div>
    </div>
  </div>

  <div class="updated" id="updated"></div>

<script>
let livePnl = {};

async function fetchFast() {
  try {
    const [healthRes, signalsRes] = await Promise.all([fetch('/'), fetch('/signals')]);
    const health = await healthRes.json();
    const signals = await signalsRes.json();
    renderHealth(health);
    renderAssetsList('assetsList', signals.signals || {});
    renderScalperPositions(signals);
    document.getElementById('updated').innerText = 'Actualizado ' + new Date().toLocaleTimeString();
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

const SCALE_HTML =
  '<div class="scale">' +
  '<span style="left:0%">0</span>' +
  '<span style="left:30%">30</span>' +
  '<span style="left:70%">70</span>' +
  '<span style="left:100%">100</span>' +
  '</div>';

function buildAssetRow(sym, s) {
  const rsi = (s.rsi !== undefined && s.rsi !== null) ? s.rsi : null;
  const markerLeft = rsi !== null ? Math.max(0, Math.min(100, rsi)) : 50;
  const rsiTxt = rsi !== null ? rsi.toFixed(1) : '-';
  const sig = s.signal || 'ESPERAR';
  const row = document.createElement('div');
  row.className = 'row';
  let nota = '';
  if (s.trend) nota += 'tendencia: ' + s.trend + '. ';
  if (s.filtro) nota += s.filtro;
  if (s.error) nota += 'error: ' + s.error;
  row.innerHTML =
    '<div class="row-sym">' + sym + '</div>' +
    '<div class="row-rsi">RSI ' + rsiTxt + '</div>' +
    '<div class="row-gauge-wrap">' +
      '<div class="gauge"><div class="marker" style="left:calc(' + markerLeft + '% - 2px)"></div></div>' +
      SCALE_HTML +
      '<div class="row-dist">' + distanciaTexto(rsi) + '</div>' +
    '</div>' +
    '<div class="row-signal signal-' + sig + '">' + sig + '</div>' +
    (nota ? '<div class="row-note">' + nota + '</div>' : '');
  return row;
}

function renderAssetsList(containerId, sigs) {
  const list = document.getElementById(containerId);
  list.innerHTML = '';
  const syms = Object.keys(sigs).sort();
  for (const sym of syms) {
    list.appendChild(buildAssetRow(sym, sigs[sym]));
  }
  if (!syms.length) list.innerHTML = '<div class="empty">Sin datos todavia.</div>';
}

function pnlBarHtml(pnl, range) {
  if (pnl === undefined || pnl === null) {
    return { pnlHtml: '<span class="pnl">s/dato</span>', barHtml: '' };
  }
  const cls = pnl >= 0 ? 'pnl-pos' : 'pnl-neg';
  const pnlHtml = '<span class="pnl ' + cls + '">' + (pnl >= 0 ? '+' : '') + pnl.toFixed(2) + '</span>';
  const pct = Math.max(-100, Math.min(100, (pnl / range) * 100));
  const fillColor = pnl >= 0 ? '#2ecc71' : '#e74c3c';
  const left = pct >= 0 ? '50%' : (50 + pct / 2) + '%';
  const width = Math.abs(pct) / 2 + '%';
  const barHtml = '<div class="pos-bar"><div class="pos-fill" style="left:' + left + ';width:' + width + ';background:' + fillColor + '"></div></div>';
  return { pnlHtml, barHtml };
}

function renderScalperPositions(signals) {
  const posList = document.getElementById('positionsList');
  posList.innerHTML = '';
  const ownPos = signals.own_positions || {};
  let any = false;
  for (const sym in ownPos) {
    for (const p of ownPos[sym]) {
      any = true;
      const pnl = livePnl[p.deal_id];
      const dir = p.direction === 'BUY' ? 'LONG' : 'SHORT';
      const { pnlHtml, barHtml } = pnlBarHtml(pnl, 2);
      const row = document.createElement('div');
      row.className = 'prow';
      row.innerHTML =
        '<div class="prow-sym">' + sym + ' ' + dir + '</div>' +
        '<div class="prow-info">entrada ' + p.entry + '</div>' +
        '<div class="prow-bar-wrap">' + barHtml + '</div>' +
        pnlHtml;
      posList.appendChild(row);
    }
  }
  if (!any) posList.innerHTML = '<div class="empty">Sin posiciones abiertas ahora.</div>';
}

async function fetchSwing() {
  const statusEl = document.getElementById('swingStatus');
  try {
    const res = await fetch('/swing-proxy');
    const data = await res.json();
    if (data.health && data.health.error) {
      statusEl.innerText = 'Bot Swing no responde (' + data.health.error + ')';
      return;
    }
    const secAgo = timeAgoSec(data.signals && data.signals.last_scan);
    if (secAgo === null) {
      statusEl.innerText = 'Sin datos del Bot Swing todavia';
    } else if (secAgo < 20 * 60) {
      statusEl.innerText = 'Velas diarias - funcionando bien - ultimo scan hace ' + Math.round(secAgo / 60) + ' min (revisa cada 15 min)';
    } else {
      statusEl.innerText = 'POSIBLE FALLA - ultimo scan hace ' + Math.round(secAgo / 60) + ' min';
    }
    renderAssetsList('swingList', (data.signals && data.signals.signals) || {});
    renderSwingPositions(data.swing_positions || []);
  } catch (e) {
    statusEl.innerText = 'Error consultando al Bot Swing';
  }
}

function renderSwingPositions(positions) {
  const posList = document.getElementById('swingPositionsList');
  posList.innerHTML = '';
  if (!positions.length) {
    posList.innerHTML = '<div class="empty">Sin posiciones abiertas ahora.</div>';
    return;
  }
  for (const p of positions) {
    const dir = p.direction === 'BUY' ? 'LONG' : 'SHORT';
    // v7.7: al Swing (velas diarias/ATR grande) no le aplica el techo de +-$2
    // del Scalper - la barra usa un rango mas amplio (+-$100) solo como
    // referencia visual, no como techo real de cierre.
    const { pnlHtml, barHtml } = pnlBarHtml(p.upl, 100);
    const row = document.createElement('div');
    row.className = 'prow';
    row.innerHTML =
      '<div class="prow-sym">' + p.epic + ' ' + dir + '</div>' +
      '<div class="prow-info">entrada ' + p.entry + '</div>' +
      '<div class="prow-bar-wrap">' + barHtml + '</div>' +
      pnlHtml;
    posList.appendChild(row);
  }
}

async function toggleTrading() {
  await fetch('/panel/toggle', { method: 'POST' });
  fetchFast();
}

function fmtUsd(n) {
  const s = (n >= 0 ? '+' : '') + n.toFixed(2);
  return s;
}

function pnlCardHtml(label, p) {
  const cls = p.net_usd >= 0 ? 'pnl-pos' : 'pnl-neg';
  return '<div class="pnl-card">' +
    '<div class="plabel">' + label + '</div>' +
    '<div class="pnet ' + cls + '">' + fmtUsd(p.net_usd) + '</div>' +
    '<div class="pdetail">' + p.wins + ' ganadas (+' + p.win_usd.toFixed(2) + ') / ' +
       p.losses + ' perdidas (' + p.loss_usd.toFixed(2) + ')</div>' +
    '</div>';
}

function botPnlLineHtml(p) {
  const cls = p.net_usd >= 0 ? 'pnl-pos' : 'pnl-neg';
  return 'Hoy: <span class="' + cls + '">' + fmtUsd(p.net_usd) + '</span> (' +
    p.wins + 'G / ' + p.losses + 'P)';
}

async function fetchPnl() {
  try {
    const res = await fetch('/pnl');
    const d = await res.json();
    if (d.error) {
      document.getElementById('pnlGrid').innerHTML = '<div class="empty">Error trayendo resultados: ' + d.error + '</div>';
      return;
    }
    const grid = document.getElementById('pnlGrid');
    grid.innerHTML =
      pnlCardHtml('Hoy', d.combined.today) +
      pnlCardHtml('Esta semana', d.combined.week) +
      pnlCardHtml('Este mes', d.combined.month);
    document.getElementById('scalperPnlLine').innerHTML = botPnlLineHtml(d.scalper.today);
    document.getElementById('swingPnlLine').innerHTML = botPnlLineHtml(d.swing.today);
  } catch (e) {
    document.getElementById('pnlGrid').innerHTML = '<div class="empty">Error consultando resultados.</div>';
  }
}

fetchFast();
fetchSlow();
fetchSwing();
fetchPnl();
setInterval(fetchFast, 5000);
setInterval(fetchSlow, 20000);
setInterval(fetchSwing, 30000);
setInterval(fetchPnl, 60000);
</script>
</body>
</html>
"""


@app.route("/dashboard", methods=["GET"])
def dashboard():
    return DASHBOARD_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/swing-proxy", methods=["GET"])
def swing_proxy():
    """
    v7.6: el dashboard tambien muestra al Bot Swing. Como corre en otro
    servicio de Railway (dominio distinto) y no expone headers CORS, un
    fetch() directo desde el navegador quedaria bloqueado por el
    navegador (Same-Origin Policy). Se pide server-side (Python -> Python,
    sin CORS de por medio) y se reenvia tal cual al frontend. Solo lectura
    de endpoints publicos de status - nunca se llama nada que module el
    trading del Bot Swing, se respeta la separacion total entre bots.

    v7.7: el propio tracking interno del Bot Swing (own_positions) pierde
    el rastro de sus posiciones en cada redeploy (mismo tipo de bug que
    se arreglo en el Scalper v7.3c) - por eso su /signals reporta
    "positions": {} aun teniendo una posicion realmente abierta. En vez
    de tocar el repo del Swing, se derivan sus posiciones reales desde
    la cuenta compartida de Capital.com (ya autenticada aca mismo via
    `client`): cualquier posicion cuyo epic NO pertenezca al universo de
    activos del Scalper (SYMBOL_MAP) es, por descarte, del Bot Swing -
    los dos bots usan universos de activos sin superposicion por diseño.
    """
    try:
        health = requests.get(f"{SWING_BASE_URL}/", timeout=8).json()
    except Exception as e:
        health = {"error": str(e)}
    try:
        signals = requests.get(f"{SWING_BASE_URL}/signals", timeout=8).json()
    except Exception as e:
        signals = {"error": str(e)}

    from capital_client import SYMBOL_MAP as SCALPER_SYMBOL_MAP
    scalper_epics = set(SCALPER_SYMBOL_MAP.values())
    swing_positions = []
    try:
        for p in (client.get_positions() or []):
            epic = p.get("market", {}).get("epic")
            if epic in scalper_epics:
                continue  # es del Scalper, no del Swing
            pos = p.get("position", {})
            swing_positions.append({
                "epic":      epic,
                "direction": pos.get("direction"),
                "entry":     pos.get("level"),
                "upl":       pos.get("upl"),
                "size":      pos.get("size"),
                "created":   pos.get("createdDateUTC"),
                "dealId":    pos.get("dealId"),
            })
    except Exception as e:
        logger.warning(f"[swing-proxy] No se pudieron derivar posiciones del Swing: {e}")

    return jsonify({"health": health, "signals": signals, "swing_positions": swing_positions}), 200


def _fetch_month_transactions():
    """
    Trae las transacciones del ultimo mes en ventanas de 7 dias (por si
    la API tiene un limite de rango que no esta documentado - asi no
    dependemos de pedir 30 dias en una sola llamada).
    """
    now  = datetime.utcnow()
    txs  = []
    seen = set()
    cursor = now
    for _ in range(5):  # 5 ventanas de 7 dias = ~35 dias de cobertura
        start = cursor - timedelta(days=7)
        try:
            chunk = client.get_transactions(
                start.strftime("%Y-%m-%dT%H:%M:%S"),
                cursor.strftime("%Y-%m-%dT%H:%M:%S"),
            )
            for t in chunk:
                ref = t.get("reference")
                if ref and ref not in seen:
                    seen.add(ref)
                    txs.append(t)
        except Exception as e:
            logger.warning(f"[pnl] error trayendo ventana {start}-{cursor}: {e}")
        cursor = start
    return txs


def _summarize(txs, since_dt, epics_filter=None, exclude=False):
    """
    epics_filter=None -> sin filtro (todo).
    epics_filter=set, exclude=False -> solo instrumentos DENTRO del set.
    epics_filter=set, exclude=True  -> solo instrumentos FUERA del set
                                        (para derivar el Swing por descarte,
                                        igual que en /swing-proxy).
    """
    wins = losses = 0
    win_usd = loss_usd = 0.0
    for t in txs:
        if t.get("transactionType") != "TRADE":
            continue
        if epics_filter is not None:
            adentro = t.get("instrumentName") in epics_filter
            if exclude and adentro:
                continue
            if not exclude and not adentro:
                continue
        try:
            dt = datetime.fromisoformat(t["dateUtc"].replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if dt < since_dt:
            continue
        try:
            amt = float(t.get("size", 0))
        except Exception:
            continue
        if amt >= 0:
            wins += 1
            win_usd += amt
        else:
            losses += 1
            loss_usd += amt
    return {
        "wins": wins, "losses": losses,
        "win_usd": round(win_usd, 2), "loss_usd": round(loss_usd, 2),
        "net_usd": round(win_usd + loss_usd, 2),
    }


@app.route("/pnl", methods=["GET"])
def pnl():
    """
    v7.9: resumen de ganancias/perdidas de operaciones CERRADAS (dia,
    semana, mes), separado por bot y combinado. El campo "size" de
    /history/transactions para type=TRADE es en realidad el PnL
    realizado de esa operacion (confirmado empiricamente: Crude Oil dio
    size=96.72, exactamente la ganancia que ya se conocia). No incluye
    posiciones todavia abiertas (eso ya lo muestra /signals en vivo) ni
    swaps/comisiones - solo operaciones cerradas.
    """
    now = datetime.now(timezone.utc)
    day_start   = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start  = day_start - timedelta(days=now.weekday())
    month_start = day_start.replace(day=1)

    try:
        txs = _fetch_month_transactions()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    from capital_client import SYMBOL_MAP as SCALPER_SYMBOL_MAP
    scalper_epics = set(SCALPER_SYMBOL_MAP.values())

    def all_periods(epics_filter=None, exclude=False):
        return {
            "today": _summarize(txs, day_start, epics_filter, exclude),
            "week":  _summarize(txs, week_start, epics_filter, exclude),
            "month": _summarize(txs, month_start, epics_filter, exclude),
        }

    return jsonify({
        "combined": all_periods(None),
        "scalper":  all_periods(scalper_epics, exclude=False),
        "swing":    all_periods(scalper_epics, exclude=True),
        "tx_count": len(txs),
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
