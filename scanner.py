"""
scanner.py — Bot Scalper v6
Estrategia: RSI 14 con bandas adaptativas por régimen 4H

Lógica de entrada (preparada para plata real):
  4H alcista  → solo LONG:
      - RSI tocó <=45 en las últimas 6 velas (pullback válido)
      - RSI cruza hacia arriba el nivel 50 (momentum confirmado)
      - La vela actual cierra en verde (close > open)

  4H bajista  → solo SHORT:
      - RSI tocó >=55 en las últimas 6 velas (rally válido)
      - RSI cruza hacia abajo el nivel 50 (momentum confirmado)
      - La vela actual cierra en rojo (close < open)

  4H neutral  → ambos (mean-reversion):
      - LONG:  RSI cruza >35 con vela verde
      - SHORT: RSI cruza <65 con vela roja

Lógica de salida (main.py):
  LONG:  RSI >= 70 (TP) | RSI cruza <50 (momentum fade) | 10 velas (time-stop)
  SHORT: RSI <= 30 (TP) | RSI cruza >50 (momentum fade) | 10 velas (time-stop)
  Neutral LONG:  RSI >= 65 | RSI cruza <50 | 10 velas
  Neutral SHORT: RSI <= 35 | RSI cruza >50 | 10 velas

SL: ATR x2 (sin cambios)
TP: ATR x4 (sin cambios, como límite máximo en la plataforma)
"""

import logging
import numpy as np
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ── Parámetros RSI ─────────────────────────────────────────────────────────────
RSI_LEN = 14

# Niveles por régimen
BULL_DIP_LEVEL       = 45    # RSI debe haber tocado <=45 en el pullback
BULL_TRIGGER         = 50    # cruce alcista de 50 = entrada LONG
BULL_TP_RSI          = 70    # salida LONG en tendencia alcista
BEAR_RALLY_LEVEL     = 55    # RSI debe haber tocado >=55 en el rally
BEAR_TRIGGER         = 50    # cruce bajista de 50 = entrada SHORT
BEAR_TP_RSI          = 30    # salida SHORT en tendencia bajista
NEUTRAL_LONG_TRIG    = 35    # cruce alcista de 35 = LONG neutral
NEUTRAL_SHORT_TRIG   = 65    # cruce bajista de 65 = SHORT neutral
NEUTRAL_LONG_TP      = 65    # salida LONG neutral
NEUTRAL_SHORT_TP     = 35    # salida SHORT neutral
MOMENTUM_FADE_LEVEL  = 50    # nivel de cierre anticipado por pérdida de momentum
PULLBACK_LOOKBACK    = 6     # velas hacia atrás para validar pullback/rally

# ── Parámetros SL/TP ATR ───────────────────────────────────────────────────────
ATR_LEN     = 14
ATR_MULT_SL = 2.0
ATR_MULT_TP = 4.0    # límite máximo en plataforma; la salida real es por RSI

# ── Otros ──────────────────────────────────────────────────────────────────────
COOLDOWN_VELAS = 3
TIME_STOP_BARS = 10  # cerrar posición si tras 10 velas (150 min) no llegó a TP/SL

CORRELATION_GROUPS = [
    {"DOGEUSD", "XRPUSD", "SOLUSD"},
    {"AAPL", "MSFT"},
    {"AMZN", "TSLA"},
]


# ── Helpers matemáticos ────────────────────────────────────────────────────────

def _ema(arr, period):
    k   = 2.0 / (period + 1)
    out = np.full(len(arr), np.nan, dtype=float)
    start = 0
    for i, v in enumerate(arr):
        if not np.isnan(v):
            out[i] = v
            start  = i + 1
            break
    for i in range(start, len(arr)):
        prev = out[i - 1]
        if np.isnan(prev):
            out[i] = arr[i]
        elif np.isnan(arr[i]):
            out[i] = prev
        else:
            out[i] = arr[i] * k + prev * (1 - k)
    return out


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


def _classify_4h_regime(candles_4h):
    """
    Régimen basado en EMA20 4H + pendiente de la EMA.
    Retorna: 'bullish' | 'bearish' | 'neutral'
    """
    if not candles_4h or len(candles_4h) < 25:
        return "neutral"
    close = np.array([c["close"] for c in candles_4h], dtype=float)
    ema20 = _ema(close, 20)
    last_close = close[-1]
    last_ema   = ema20[-1]
    if np.isnan(last_ema) or last_ema == 0:
        return "neutral"
    dist = (last_close - last_ema) / last_ema
    # Pendiente: EMA ahora vs hace 3 velas
    slope_up   = ema20[-1] > ema20[-4] if len(ema20) >= 4 else False
    slope_down = ema20[-1] < ema20[-4] if len(ema20) >= 4 else False
    NEUTRAL_BUFFER = 0.001   # ±0.1%
    if dist > NEUTRAL_BUFFER and slope_up:
        return "bullish"
    if dist < -NEUTRAL_BUFFER and slope_down:
        return "bearish"
    return "neutral"


# ── Lógica principal ───────────────────────────────────────────────────────────

def _score_symbol(sym, candles_15m, candles_4h):
    if not candles_15m or len(candles_15m) < 60:
        return None

    close = np.array([c["close"] for c in candles_15m], dtype=float)
    open_ = np.array([c["open"]  for c in candles_15m], dtype=float)
    high  = np.array([c["high"]  for c in candles_15m], dtype=float)
    low   = np.array([c["low"]   for c in candles_15m], dtype=float)

    # ── RSI Wilder ───────────────────────────────────────────────────────────
    rsi = _rsi_wilder(close, RSI_LEN)
    if np.isnan(rsi[-1]) or np.isnan(rsi[-2]):
        return None

    rsi_curr = float(rsi[-1])
    rsi_prev = float(rsi[-2])

    # ── ATR ──────────────────────────────────────────────────────────────────
    atr_arr = _atr(high, low, close, ATR_LEN)
    atr_val = float(atr_arr[-1]) if not np.isnan(atr_arr[-1]) else 0.0
    last    = float(close[-1])

    # ── Confirmación de vela ─────────────────────────────────────────────────
    green_candle = close[-1] > open_[-1]   # cierre > apertura
    red_candle   = close[-1] < open_[-1]

    # ── Régimen 4H ───────────────────────────────────────────────────────────
    regime = _classify_4h_regime(candles_4h)

    # ── RSI de las últimas PULLBACK_LOOKBACK velas (sin la actual) ───────────
    lookback_start = -(PULLBACK_LOOKBACK + 1)
    recent_rsi = rsi[lookback_start:-1]
    recent_rsi_valid = recent_rsi[~np.isnan(recent_rsi)]

    # ── Señal de entrada ─────────────────────────────────────────────────────
    signal    = "ESPERAR"
    long_tp   = BULL_TP_RSI
    short_tp  = BEAR_TP_RSI
    filtro    = None

    if regime == "bullish":
        long_tp  = BULL_TP_RSI      # 70
        short_tp = BULL_TP_RSI      # no aplica, solo LONG
        had_pullback = len(recent_rsi_valid) > 0 and np.any(recent_rsi_valid <= BULL_DIP_LEVEL)
        crossed_up   = rsi_prev < BULL_TRIGGER and rsi_curr >= BULL_TRIGGER
        if had_pullback and crossed_up and green_candle:
            signal = "LONG"
        elif not had_pullback and crossed_up:
            filtro = "sin_pullback_previo"
        elif not crossed_up:
            filtro = f"RSI_no_cruzo_50 (curr={rsi_curr:.1f})"

    elif regime == "bearish":
        long_tp  = BEAR_TP_RSI      # no aplica, solo SHORT
        short_tp = BEAR_TP_RSI      # 30
        had_rally  = len(recent_rsi_valid) > 0 and np.any(recent_rsi_valid >= BEAR_RALLY_LEVEL)
        crossed_dn = rsi_prev > BEAR_TRIGGER and rsi_curr <= BEAR_TRIGGER
        if had_rally and crossed_dn and red_candle:
            signal = "SHORT"
        elif not had_rally and crossed_dn:
            filtro = "sin_rally_previo"
        elif not crossed_dn:
            filtro = f"RSI_no_cruzo_50 (curr={rsi_curr:.1f})"

    else:  # neutral
        long_tp  = NEUTRAL_LONG_TP   # 65
        short_tp = NEUTRAL_SHORT_TP  # 35
        if rsi_prev < NEUTRAL_LONG_TRIG and 