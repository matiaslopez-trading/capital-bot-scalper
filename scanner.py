"""
scanner.py — Bot Scalper v7.18
Estrategia: mean-reversion en velas de 5 minutos, con filtro de tendencia (ADX).

v7.18 (22/07/2026): investigacion completa tras un dia de resultados
mayormente negativos (32% de aciertos, perdida promedio mayor que
ganancia promedio). Se identificaron 2 causas de fondo (ademas de bugs
de codigo ya corregidos):
  1. El universo de 18 activos incluia 12 criptomonedas con spread real
     medido en vivo de ~0.5% (hasta 15x mas ancho que acciones/indices/
     forex, verificado via /debug-spreads) - eso solo ya se come una
     porcion grande de un objetivo de $2-3 por operacion. Los peores
     activos del dia (ADAUSD -$5.72, AVAXUSD -$1.65, LINKUSD -$1.17)
     fueron todos crypto, mientras que acciones/indices quedaron planos
     o positivos.
  2. Sin ningun filtro de tendencia: la reversion a la media (comprar
     sobreventa, vender sobrecompra) pierde plata sistematicamente
     cuando el activo esta en tendencia fuerte ("agarra cuchillos
     cayendo" - lo extremo de hoy es lo normal de mañana en una
     tendencia). Confirmado por literatura de trading y por los PDFs de
     scalping de Matias (Manual Avanzado de Trading, Modulo 1
     Introduccion al Scalping - XTB): recomiendan evitar operar en
     contra de la tendencia y usar ADX para distinguir mercado en
     rango (favorable a reversion) de mercado en tendencia (favorable
     a momentum, no a reversion).

Fix: se agrega filtro ADX(14) - no se opera reversion a la media si
ADX > ADX_MAX_TREND (tendencia demasiado fuerte). Se eligio un umbral
mas permisivo (30, no el 25 "de libro") a proposito para no sacrificar
de mas la frecuencia de señales, ya que Matias pidio explicitamente
"muchas operaciones" como condicion no negociable.

Ademas se amplia el universo de activos en capital_client.py (sacando
las 12 criptomonedas, sumando NATURALGAS/META/NFLX/COIN/JPM/GOLD/
SILVER/OIL_CRUDE - todos verificados en vivo con spread bajo y tamaño
de operacion real cercano a $2-3) para compensar la baja de frecuencia
que iba a producir sacar las cryptos.

v7.2 (20/07/2026): fix critico — ahora se descarta siempre la ultima
vela del array antes de calcular indicadores, porque con polling cada
1 minuto esa vela suele estar todavia formandose y su RSI fluctua por
ruido, no por señal real. El 20/07 esto causo 170 operaciones en un dia
con 28% de aciertos y -$673 netos (ver CSV de la cuenta). Ahora solo se
opera sobre velas de 5min ya cerradas.

v7.11 (21/07/2026): umbral de RSI relajado de 30/70 a 35/65. Con 18
activos activos y el guardrail de exposicion ya arreglado (v7.10.1),
se observaron horas seguidas sin que NINGUN activo tocara 30/70 (rango
real observado: 38-64) - cero señales, cero operaciones nuevas del
Scalper en todo el dia. Matias pidio explicitamente relajar el umbral
tras ser advertido de que esto fue justamente lo que causo el desastre
de las 170 operaciones (28% aciertos) cuando se hizo sin medir impacto
en el pasado. Diferencia clave esta vez: la vela de confirmacion sigue
siendo obligatoria (nada cambia ahi) y el fix v7.2 (no operar sobre la
vela todavia en formacion) sigue vigente - solo se ensancha la zona
donde se considera "extremo", no se elimina el filtro de calidad.
Monitorear win rate de las primeras operaciones bajo este umbral antes
de decidir si se mantiene.

Objetivo: MUCHAS operaciones de calidad por día (no pocas con R:R alto).
Sin filtro de tendencia superior — opera LONG y SHORT indistintamente,
el criterio de calidad viene de RSI extremo + confirmación de vela
(martillo, estrella fugaz, envolvente — Manual Avanzado de Trading,
Admiral Markets, sección "Velas Japonesas").

Lógica de entrada:
  LONG:
      - RSI(14) <= 35 (zona de sobreventa, relajado desde 30 en v7.11)
      - Confirmación: vela martillo, envolvente alcista,
        o vela verde con RSI ya girando hacia arriba (rsi_curr > rsi_prev)

  SHORT:
      - RSI(14) >= 65 (zona de sobrecompra, relajado desde 70 en v7.11)
      - Confirmación: vela estrella fugaz, envolvente bajista,
        o vela roja con RSI ya girando hacia abajo (rsi_curr < rsi_prev)

Lógica de salida (en main.py):
  - TP / SL nativos en la plataforma (ATR based)
  - Salida anticipada si RSI vuelve a zona neutral (50) sin llegar a TP
  - Time-stop: cerrar si la posición lleva demasiadas velas sin resolver

SL: ATR x1.0   |   TP: ATR x1.3
(ratio ligeramente > 1:1 para compensar spread/comisiones mientras
se mantiene alta frecuencia de señales)
"""

import logging
import numpy as np
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Parámetros RSI ─────────────────────────────────────────────────────────
RSI_LEN    = 14
OVERSOLD   = 35   # v7.11: relajado desde 30 (0 señales en horas con umbral estricto)
OVERBOUGHT = 65   # v7.11: relajado desde 70
EXIT_LONG_RSI  = 55   # salida anticipada LONG: RSI volvió a zona neutral+
EXIT_SHORT_RSI = 45   # salida anticipada SHORT: RSI volvió a zona neutral-

# ── Parámetros SL/TP ATR ─────────────────────────────────────────────────
ATR_LEN     = 14
ATR_MULT_SL = 1.0
ATR_MULT_TP = 1.3

# ── Filtro de tendencia (v7.18) ──────────────────────────────────────────
# ADX alto = tendencia fuerte = evitar reversion a la media (se opera en
# contra del movimiento). ADX bajo = mercado en rango = favorable para
# este tipo de estrategia. Umbral mas permisivo que el estandar de
# libro (25) para no sacrificar de mas la frecuencia de señales.
ADX_LEN         = 14
ADX_MAX_TREND   = 30

# ── Bollinger Bands + VWAP (v7.19) ───────────────────────────────────────
# Matias pidio explicitamente investigar mas alla de "dos indicadores
# matematicos" - las Bandas de Bollinger dan una confirmacion de extremo
# ESTADISTICO (precio a N desvios estandar de su media movil) que es
# independiente del RSI (que mide momentum, no distancia de precio). Es
# una tecnica bien documentada de scalping/reversion a la media (fuente:
# busqueda web 22/07/2026 - "RSI + Bollinger Band confirmation... when
# price hits the upper band and RSI shows bearish divergence, thats a
# high-probability short setup"). Se usa como confirmacion ALTERNATIVA a
# los patrones de vela (no exige las dos a la vez) para no bajar de mas
# la frecuencia: ahora una señal es valida si RSI extremo + (vela de
# confirmacion O precio toco la banda de Bollinger opuesta).
# VWAP se calcula y se expone en el resultado como dato de contexto
# (sesgo: por debajo de VWAP = zona "barata", por encima = zona "cara"),
# no como filtro duro todavia - da contexto util para lectura manual y
# queda listo para usarse como filtro en una proxima iteracion si los
# datos lo justifican.
BB_LEN    = 20
BB_STD    = 2.0

# ── Otros ─────────────────────────────────────────────────────────────────
COOLDOWN_VELAS  = 2   # velas de 5min de espera tras cerrar una posición
TIME_STOP_BARS  = 24  # 24 velas x 5min = 120 min máx en una operación
MAX_POS_PER_SYM = 2   # hasta 2 operaciones simultáneas por activo


# ── Helpers matemáticos ──────────────────────────────────────────────────

def _rsi_wilder(close, period=RSI_LEN):
    """RSI con suavizado de Wilder — idéntico a TradingView / Capital.com."""
    delta = np.diff(close.astype(float))
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    avg_g = np.full(len(gain), np.nan)
    avg_l = np.full(len(loss), np.nan)
    if period <= len(gain):
        avg_g[period - 1] = np.mean(gain[:period])
        avg_l[period - 1] = np.mean(loss[:period])
        for i in range(period, len(gain)):
            avg_g[i] = (avg_g[i - 1] * (period - 1) + gain[i]) / period
            avg_l[i] = (avg_l[i - 1] * (period - 1) + loss[i]) / period
    rs  = np.where(avg_l == 0, 100.0, avg_g / avg_l)
    rsi = 100.0 - 100.0 / (1 + rs)
    return np.concatenate([[np.nan], rsi])


def _atr(high, low, close, period=ATR_LEN):
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - close[:-1]),
                   np.abs(low[1:]  - close[:-1]))
    )
    atr = np.full(len(close), np.nan)
    if period < len(tr):
        atr[period] = np.mean(tr[:period])
        for i in range(period + 1, len(close)):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i - 1]) / period
    return atr


def _adx_last(high, low, close, period=ADX_LEN):
    """ADX(14) — mide fuerza de tendencia (no direccion). >30 = tendencia
    fuerte (evitar reversion a la media), <20 = mercado en rango."""
    n = len(close)
    if n < period * 2 + 2:
        return float("nan")
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]),
                               np.abs(low[1:] - close[:-1])))
    up   = high[1:] - high[:-1]
    down = low[:-1] - low[1:]
    pdm  = np.where((up > down) & (up > 0), up, 0.0)
    mdm  = np.where((down > up) & (down > 0), down, 0.0)
    atr_s = np.full(n - 1, np.nan); ps = np.full(n - 1, np.nan); ms = np.full(n - 1, np.nan)
    atr_s[period - 1] = np.sum(tr[:period])
    ps[period - 1]    = np.sum(pdm[:period])
    ms[period - 1]    = np.sum(mdm[:period])
    for i in range(period, n - 1):
        atr_s[i] = atr_s[i - 1] - atr_s[i - 1] / period + tr[i]
        ps[i]    = ps[i - 1]    - ps[i - 1] / period    + pdm[i]
        ms[i]    = ms[i - 1]    - ms[i - 1] / period    + mdm[i]
    pdi = 100 * np.where(atr_s > 0, ps / atr_s, 0.0)
    mdi = 100 * np.where(atr_s > 0, ms / atr_s, 0.0)
    dx  = 100 * np.abs(pdi - mdi) / np.where((pdi + mdi) > 0, pdi + mdi, 1.0)
    adx = np.full(n - 1, np.nan)
    adx[period * 2 - 2] = np.mean(dx[period - 1:period * 2 - 1])
    for i in range(period * 2 - 1, n - 1):
        adx[i] = (adx[i - 1] * (period - 1) + dx[i]) / period
    val = adx[-1]
    return float(val) if not np.isnan(val) else float("nan")


def _bollinger(close, period=BB_LEN, n_std=BB_STD):
    """Bandas de Bollinger: media movil simple +- n desvios estandar."""
    n = len(close)
    mid = np.full(n, np.nan)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    if n >= period:
        for i in range(period - 1, n):
            window = close[i - period + 1:i + 1]
            m = np.mean(window)
            s = np.std(window)
            mid[i] = m
            upper[i] = m + n_std * s
            lower[i] = m - n_std * s
    return mid, upper, lower


def _vwap_rolling(high, low, close, volume, period=BB_LEN):
    """VWAP movil (ventana de BB_LEN velas) usando precio tipico (H+L+C)/3.
    No es el VWAP de sesion clasico (Capital.com no expone horario de
    sesion de forma directa via API), pero cumple la misma funcion de
    referencia de 'precio justo' reciente para dar contexto caro/barato."""
    tp = (high + low + close) / 3.0
    n = len(close)
    vwap = np.full(n, np.nan)
    vol = volume if volume is not None else np.ones(n)
    for i in range(period - 1, n):
        w_tp  = tp[i - period + 1:i + 1]
        w_vol = vol[i - period + 1:i + 1]
        vol_sum = np.sum(w_vol)
        if vol_sum > 0:
            vwap[i] = np.sum(w_tp * w_vol) / vol_sum
        else:
            vwap[i] = np.mean(w_tp)
    return vwap


# ── Patrones de velas (Manual Avanzado de Trading, Admiral Markets) ──────

def _is_hammer(o, h, l, c):
    """Martillo: cuerpo chico arriba, sombra inferior >= 2x el cuerpo."""
    body = abs(c - o)
    if body == 0:
        return False
    lower_wick = min(o, c) - l
    upper_wick = h - max(o, c)
    return lower_wick >= body * 2 and upper_wick <= body * 0.5


def _is_shooting_star(o, h, l, c):
    """Estrella fugaz: cuerpo chico abajo, sombra superior >= 2x el cuerpo."""
    body = abs(c - o)
    if body == 0:
        return False
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    return upper_wick >= body * 2 and lower_wick <= body * 0.5


def _is_bullish_engulfing(o_prev, c_prev, o_curr, c_curr):
    """Vela roja cubierta totalmente por una vela verde."""
    prev_red   = c_prev < o_prev
    curr_green = c_curr > o_curr
    return prev_red and curr_green and c_curr >= o_prev and o_curr <= c_prev


def _is_bearish_engulfing(o_prev, c_prev, o_curr, c_curr):
    """Vela verde cubierta totalmente por una vela roja."""
    prev_green = c_prev > o_prev
    curr_red   = c_curr < o_curr
    return prev_green and curr_red and o_curr >= c_prev and c_curr <= o_prev


# ── Lógica principal ──────────────────────────────────────────────────────

def _score_symbol(sym, candles_5m):
    if not candles_5m or len(candles_5m) < 41:
        return None

    # v7.2 FIX: descartar la última vela — con polling cada 1 minuto pero
    # velas de 5 minutos, la última vela del array suele estar todavia
    # formandose. Su precio (y por lo tanto el RSI) fluctua constantemente
    # mientras se arma, generando cruces de 30/70 por ruido momentaneo en
    # vez de por una señal real. Esto causaba entradas y salidas en
    # segundos (170 operaciones/dia, 28% de aciertos, -$673 netos el
    # 20/07/2026). Al usar solo velas YA CERRADAS, el RSI deja de
    # parpadear. Seguimos consultando cada 1 min para reaccionar rapido
    # apenas una vela cierra de verdad, pero ya no operamos sobre ruido.
    candles_5m = candles_5m[:-1]

    close  = np.array([c["close"]  for c in candles_5m], dtype=float)
    open_  = np.array([c["open"]   for c in candles_5m], dtype=float)
    high   = np.array([c["high"]   for c in candles_5m], dtype=float)
    low    = np.array([c["low"]    for c in candles_5m], dtype=float)
    volume = np.array([c.get("volume", 0) or 0 for c in candles_5m], dtype=float)

    rsi = _rsi_wilder(close, RSI_LEN)
    if np.isnan(rsi[-1]) or np.isnan(rsi[-2]):
        return None

    rsi_curr = float(rsi[-1])
    rsi_prev = float(rsi[-2])

    atr_arr = _atr(high, low, close, ATR_LEN)
    atr_val = float(atr_arr[-1]) if not np.isnan(atr_arr[-1]) else 0.0
    last    = float(close[-1])

    # v7.18: filtro de tendencia — no operar reversion a la media si el
    # activo esta en tendencia fuerte (ver comentario junto a ADX_MAX_TREND).
    adx_val = _adx_last(high, low, close, ADX_LEN)
    tendencia_fuerte = (not np.isnan(adx_val)) and adx_val > ADX_MAX_TREND

    # v7.19: Bandas de Bollinger (confirmacion alternativa a la vela) + VWAP
    # movil (contexto caro/barato, informativo).
    bb_mid, bb_upper, bb_lower = _bollinger(close, BB_LEN, BB_STD)
    vwap_arr = _vwap_rolling(high, low, close, volume, BB_LEN)
    bb_lower_val = float(bb_lower[-1]) if not np.isnan(bb_lower[-1]) else None
    bb_upper_val = float(bb_upper[-1]) if not np.isnan(bb_upper[-1]) else None
    vwap_val     = float(vwap_arr[-1]) if not np.isnan(vwap_arr[-1]) else None
    touched_lower_bb = bb_lower_val is not None and low[-1] <= bb_lower_val
    touched_upper_bb = bb_upper_val is not None and high[-1] >= bb_upper_val

    green_candle = close[-1] > open_[-1]
    red_candle   = close[-1] < open_[-1]

    hammer         = _is_hammer(open_[-1], high[-1], low[-1], close[-1])
    shooting_star  = _is_shooting_star(open_[-1], high[-1], low[-1], close[-1])
    bull_engulf    = _is_bullish_engulfing(open_[-2], close[-2], open_[-1], close[-1])
    bear_engulf    = _is_bearish_engulfing(open_[-2], close[-2], open_[-1], close[-1])

    signal = "ESPERAR"
    filtro = None

    if tendencia_fuerte:
        filtro = f"tendencia_fuerte (ADX={adx_val:.1f} > {ADX_MAX_TREND})"
    elif rsi_curr <= OVERSOLD:
        # v7.19: confirmacion por vela O por Bandas de Bollinger (precio
        # tocando/rompiendo la banda inferior = extremo estadistico,
        # confirmacion independiente del RSI).
        confirmacion = hammer or bull_engulf or touched_lower_bb or (green_candle and rsi_curr > rsi_prev)
        if confirmacion:
            signal = "LONG"
        else:
            filtro = f"oversold_sin_confirmacion (RSI={rsi_curr:.1f})"
    elif rsi_curr >= OVERBOUGHT:
        confirmacion = shooting_star or bear_engulf or touched_upper_bb or (red_candle and rsi_curr < rsi_prev)
        if confirmacion:
            signal = "SHORT"
        else:
            filtro = f"overbought_sin_confirmacion (RSI={rsi_curr:.1f})"

    if signal == "LONG" and atr_val > 0:
        sl  = round(last - atr_val * ATR_MULT_SL, 5)
        tp1 = round(last + atr_val * ATR_MULT_TP, 5)
    elif signal == "SHORT" and atr_val > 0:
        sl  = round(last + atr_val * ATR_MULT_SL, 5)
        tp1 = round(last - atr_val * ATR_MULT_TP, 5)
    else:
        sl = tp1 = 0.0

    result = {
        "signal":          signal,
        "rsi":             round(rsi_curr, 2),
        "rsi_prev":        round(rsi_prev, 2),
        "exit_long_rsi":   EXIT_LONG_RSI,
        "exit_short_rsi":  EXIT_SHORT_RSI,
        "entry":           last,
        "sl":              sl,
        "tp1":             tp1,
        "atr":             round(atr_val, 5),
        "hammer":          bool(hammer),
        "shooting_star":   bool(shooting_star),
        "bull_engulf":     bool(bull_engulf),
        "bear_engulf":     bool(bear_engulf),
        "adx":             round(adx_val, 1) if not np.isnan(adx_val) else None,
        "bb_lower":        round(bb_lower_val, 6) if bb_lower_val is not None else None,
        "bb_upper":        round(bb_upper_val, 6) if bb_upper_val is not None else None,
        "vwap":            round(vwap_val, 6) if vwap_val is not None else None,
        "touched_lower_bb": bool(touched_lower_bb),
        "touched_upper_bb": bool(touched_upper_bb),
    }
    if filtro:
        result["filtro"] = filtro

    return result


def run_scanner(data_5m,
                 open_positions_count: dict = None,
                 cooldown_until: dict = None) -> dict:
    """
    open_positions_count: { sym: n_posiciones_abiertas }
    cooldown_until:       { sym: datetime }
    """
    if open_positions_count is None:
        open_positions_count = {}
    if cooldown_until is None:
        cooldown_until = {}

    now     = datetime.now(timezone.utc)
    results = {}

    for sym, candles in data_5m.items():
        try:
            res = _score_symbol(sym, candles)
            if res is None:
                results[sym] = {
                    "signal": "ESPERAR", "rsi": 0,
                    "exit_long_rsi": EXIT_LONG_RSI,
                    "exit_short_rsi": EXIT_SHORT_RSI,
                    "error": "datos_insuficientes",
                }
                continue

            if res["signal"] in ("LONG", "SHORT"):
                cd = cooldown_until.get(sym)
                if cd and now < cd:
                    remaining = int((cd - now).total_seconds() / 60)
                    res["signal"] = "ESPERAR"
                    res["filtro"] = f"cooldown:{remaining}min"

            if res["signal"] in ("LONG", "SHORT"):
                n_abiertas = open_positions_count.get(sym, 0)
                if n_abiertas >= MAX_POS_PER_SYM:
                    res["signal"] = "ESPERAR"
                    res["filtro"] = f"max_posiciones:{n_abiertas}/{MAX_POS_PER_SYM}"

            results[sym] = res
            logger.info(
                f"[scanner] {sym}: {res['signal']} | "
                f"RSI={res['rsi']:.1f} prev={res['rsi_prev']:.1f} | "
                f"filtro={res.get('filtro', '-')}"
            )

        except Exception as e:
            logger.error(f"[scanner] {sym}: {e}")
            results[sym] = {
                "signal": "ESPERAR", "rsi": 0,
                "exit_long_rsi": EXIT_LONG_RSI,
                "exit_short_rsi": EXIT_SHORT_RSI,
                "error": str(e),
            }

    return results
