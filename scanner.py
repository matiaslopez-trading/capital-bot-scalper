"""
scanner.py — Bot Scalper v7.11
Estrategia: mean-reversion pura en velas de 5 minutos.

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

    close = np.array([c["close"] for c in candles_5m], dtype=float)
    open_ = np.array([c["open"]  for c in candles_5m], dtype=float)
    high  = np.array([c["high"]  for c in candles_5m], dtype=float)
    low   = np.array([c["low"]   for c in candles_5m], dtype=float)

    rsi = _rsi_wilder(close, RSI_LEN)
    if np.isnan(rsi[-1]) or np.isnan(rsi[-2]):
        return None

    rsi_curr = float(rsi[-1])
    rsi_prev = float(rsi[-2])

    atr_arr = _atr(high, low, close, ATR_LEN)
    atr_val = float(atr_arr[-1]) if not np.isnan(atr_arr[-1]) else 0.0
    last    = float(close[-1])

    green_candle = close[-1] > open_[-1]
    red_candle   = close[-1] < open_[-1]

    hammer         = _is_hammer(open_[-1], high[-1], low[-1], close[-1])
    shooting_star  = _is_shooting_star(open_[-1], high[-1], low[-1], close[-1])
    bull_engulf    = _is_bullish_engulfing(open_[-2], close[-2], open_[-1], close[-1])
    bear_engulf    = _is_bearish_engulfing(open_[-2], close[-2], open_[-1], close[-1])

    signal = "ESPERAR"
    filtro = None

    if rsi_curr <= OVERSOLD:
        confirmacion = hammer or bull_engulf or (green_candle and rsi_curr > rsi_prev)
        if confirmacion:
            signal = "LONG"
        else:
            filtro = f"oversold_sin_confirmacion (RSI={rsi_curr:.1f})"
    elif rsi_curr >= OVERBOUGHT:
        confirmacion = shooting_star or bear_engulf or (red_candle and rsi_curr < rsi_prev)
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
