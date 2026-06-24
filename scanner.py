"""
scanner.py — Bot Scalper v4
Estrategia: IFTRSI (Inverse Fisher Transform RSI) — matches TradingView IFTRSI_LB 14 9

Lógica:
- LONG:  IFTRSI cruza hacia ARRIBA el umbral de oversold (-0.5)
- SHORT: IFTRSI cruza hacia ABAJO el umbral de overbought (+0.5)
- Bias 4H: en tendencia bajista exige IFTRSI más extremo para LONG
            en tendencia alcista exige IFTRSI más extremo para SHORT
- Exit thresholds: exportados para que main.py cierre anticipadamente

v4.1: Reversión desde zona extrema (REVERSAL_MIN=0.03)
- Captura señales cuando IFTRSI está atascado en zona extrema y empieza a girar
"""

import logging
import numpy as np
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# IFTRSI — parámetros idénticos al indicador TradingView IFTRSI_LB 14 9
RSI_LEN    = 14
RSI_SMOOTH = 9     # EMA aplicada al RSI antes de la transformada

# Umbrales estándar (mercado neutral)
OB_STD = 0.5    # overbought — entrada SHORT / salida LONG
OS_STD = -0.5   # oversold   — entrada LONG  / salida SHORT

# Umbrales reforzados (contra tendencia)
OB_STRONG = 0.7
OS_STRONG = -0.7

# Multiplicadores SL/TP sobre ATR
ATR_LEN     = 14
ATR_MULT_SL = 2.0
ATR_MULT_TP = 4.0   # R:R 2:1 — el exit dinámico reemplaza al TP en la práctica

COOLDOWN_VELAS = 3

CORRELATION_GROUPS = [
    {"DOGEUSD", "XRPUSD", "SOLUSD"},
    {"AAPL", "MSFT"},
    {"AMZN", "TSLA"},
]


# ── helpers matemáticos ────────────────────────────────────────────────────────

def _ema(arr, period):
    k   = 2.0 / (period + 1)
    out = np.full(len(arr), np.nan, dtype=float)
    # seed con el primer valor no-nan
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
    delta = np.diff(close)
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


def _iftrsi(close, rsi_period=RSI_LEN, smooth=RSI_SMOOTH):
    """
    Inverse Fisher Transform del RSI.
    Resultado oscila limpiamente entre -1 y +1.
    Fórmula: IFT( EMA(RSI, smooth) )
    """
    rsi   = _rsi_wilder(close, rsi_period)
    rsi_s = _ema(rsi, smooth)
    x     = np.clip(0.1 * (rsi_s - 50), -10, 10)   # evitar overflow
    exp2x = np.exp(2 * x)
    ift   = (exp2x - 1) / (exp2x + 1)
    return ift


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


def _bias_4h(candles_4h):
    """Bias direccional basado en EMA20 de las velas 4H."""
    if not candles_4h or len(candles_4h) < 25:
        return 0
    close = np.array([c["close"] for c in candles_4h], dtype=float)
    ema20 = _ema(close, 20)
    if np.isnan(ema20[-1]) or ema20[-1] == 0:
        return 0
    diff = (close[-1] - ema20[-1]) / ema20[-1]
    if diff > 0.001:
        return 1
    elif diff < -0.001:
        return -1
    return 0


# ── lógica principal ───────────────────────────────────────────────────────────

def _score_symbol(sym, candles_15m, candles_4h, regime=None):
    # Necesitamos al menos 60 velas para RSI(14) + EMA(9) + algo de historia
    if not candles_15m or len(candles_15m) < 60:
        return None

    close  = np.array([c["close"]  for c in candles_15m], dtype=float)
    high   = np.array([c["high"]   for c in candles_15m], dtype=float)
    low    = np.array([c["low"]    for c in candles_15m], dtype=float)

    # ── IFTRSI ──────────────────────────────────────────────────────────────
    ift = _iftrsi(close)
    if np.isnan(ift[-1]) or np.isnan(ift[-2]):
        logger.info(f"[scanner] {sym}: IFTRSI NaN — datos insuficientes")
        return None

    ift_curr = float(ift[-1])
    ift_prev = float(ift[-2])

    # ── ATR para SL/TP ──────────────────────────────────────────────────────
    atr_arr = _atr(high, low, close, ATR_LEN)
    atr_val = float(atr_arr[-1]) if not np.isnan(atr_arr[-1]) else 0.0
    last    = float(close[-1])

    # ── Bias 4H ─────────────────────────────────────────────────────────────
    bias = _bias_4h(candles_4h)

    # ── Umbrales dinámicos según tendencia 4H ───────────────────────────────
    if bias == -1:      # bajista
        entry_long_thresh  = OS_STRONG   # -0.7
        entry_short_thresh = OB_STD      # +0.5
        exit_long_thresh   = 0.3
        exit_short_thresh  = -0.5
    elif bias == 1:     # alcista
        entry_long_thresh  = OS_STD      # -0.5
        entry_short_thresh = OB_STRONG   # +0.7
        exit_long_thresh   = 0.5
        exit_short_thresh  = -0.3
    else:               # neutral
        entry_long_thresh  = OS_STD      # -0.5
        entry_short_thresh = OB_STD      # +0.5
        exit_long_thresh   = OB_STD      # +0.5
        exit_short_thresh  = OS_STD      # -0.5

    # ── Señal de entrada ─────────────────────────────────────────────────────
    # 1) Cruce clásico del umbral (original)
    # 2) Reversión desde zona extrema: IFTRSI atascado bajo/sobre el umbral
    #    pero empieza a girar con fuerza (delta >= REVERSAL_MIN)
    REVERSAL_MIN = 0.03

    if ift_prev <= entry_long_thresh and ift_curr > entry_long_thresh:
        signal = "LONG"    # cruce clásico
    elif ift_curr <= entry_long_thresh and (ift_curr - ift_prev) >= REVERSAL_MIN:
        signal = "LONG"    # reversión desde oversold sin cruzar aún
    elif ift_prev >= entry_short_thresh and ift_curr < entry_short_thresh:
        signal = "SHORT"   # cruce clásico
    elif ift_curr >= entry_short_thresh and (ift_prev - ift_curr) >= REVERSAL_MIN:
        signal = "SHORT"   # reversión desde overbought sin cruzar aún
    else:
        signal = "ESPERAR"

    # ── SL / TP basados en ATR ───────────────────────────────────────────────
    if signal == "LONG" and atr_val > 0:
        sl  = round(last - atr_val * ATR_MULT_SL, 5)
        tp1 = round(last + atr_val * ATR_MULT_TP, 5)
    elif signal == "SHORT" and atr_val > 0:
        sl  = round(last + atr_val * ATR_MULT_SL, 5)
        tp1 = round(last - atr_val * ATR_MULT_TP, 5)
    else:
        sl = tp1 = 0.0

    return {
        "signal":             signal,
        "iftrsi":             round(ift_curr, 4),
        "ift_prev":           round(ift_prev, 4),
        "entry_long_thresh":  entry_long_thresh,
        "entry_short_thresh": entry_short_thresh,
        "exit_long_thresh":   exit_long_thresh,
        "exit_short_thresh":  exit_short_thresh,
        "entry":              last,
        "sl":                 sl,
        "tp1":                tp1,
        "atr":                round(atr_val, 5),
        "bias_4h":            bias,
        "regime":             regime or "NEUTRAL",
    }


def run_scanner(data_15m, data_4h,
                open_positions: set = None,
                cooldown_until: dict = None,
                regimes: dict = None) -> dict:
    if open_positions is None:
        open_positions = set()
    if cooldown_until is None:
        cooldown_until = {}
    if regimes is None:
        regimes = {}

    now     = datetime.now(timezone.utc)
    results = {}

    for sym, candles in data_15m.items():
        candles_4h = (data_4h or {}).get(sym)
        regime     = (regimes or {}).get(sym)
        try:
            res = _score_symbol(sym, candles, candles_4h, regime=regime)
            if res is None:
                results[sym] = {
                    "signal": "ESPERAR", "iftrsi": 0,
                    "exit_long_thresh": 0.5, "exit_short_thresh": -0.5,
                    "error": "datos_insuficientes",
                }
                continue

            if res["signal"] in ("LONG", "SHORT"):
                cd = cooldown_until.get(sym)
                if cd and now < cd:
                    remaining = int((cd - now).total_seconds() / 60)
                    logger.info(f"[scanner] {sym}: cooldown {remaining}min")
                    res["signal"] = "ESPERAR"
                    res["filtro"] = f"cooldown:{remaining}min"

            if res["signal"] in ("LONG", "SHORT") and sym in open_positions:
                res["signal"] = "ESPERAR"
                res["filtro"] = "ya_abierto"

            if res["signal"] in ("LONG", "SHORT"):
                for grupo in CORRELATION_GROUPS:
                    if sym in grupo:
                        bloq = grupo & open_positions
                        if bloq:
                            res["signal"] = "ESPERAR"
                            res["filtro"] = f"correlacion:{bloq}"
                            break

            results[sym] = res
            logger.info(
                f"[scanner] {sym}: {res['signal']} "
                f"IFTRSI={res['iftrsi']:+.3f} prev={res['ift_prev']:+.3f} "
                f"bias4h={res['bias_4h']:+d} "
                f"exit_L={res['exit_long_thresh']} exit_S={res['exit_short_thresh']}"
            )

        except Exception as e:
            logger.error(f"[scanner] {sym}: {e}")
            results[sym] = {
                "signal": "ESPERAR", "iftrsi": 0,
                "exit_long_thresh": 0.5, "exit_short_thresh": -0.5,
                "error": str(e),
            }

    return results
