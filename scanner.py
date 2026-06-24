"""
scanner.py — Bot Scalper v5
Estrategia: RSI 14 plano + filtro de tendencia 4H (EMA20)

Lógica de entrada:
- LONG:  RSI cruza hacia ARRIBA el nivel 30 (sale de sobreventa)
- SHORT: RSI cruza hacia ABAJO el nivel 70 (sale de sobrecompra)

Filtro 4H bias (EMA20):
- Tendencia alcista (bias=+1):  solo LONG permitido
- Tendencia bajista (bias=-1):  solo SHORT permitido
- Neutral (bias=0):             ambos permitidos (mean-reversion pura)

Salida dinámica (en main.py):
- Cerrar LONG cuando RSI >= 50
- Cerrar SHORT cuando RSI <= 50
"""

import logging
import numpy as np
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# RSI parámetros
RSI_LEN = 14

# Niveles RSI
RSI_OS   = 30    # oversold  — entrada LONG
RSI_OB   = 70    # overbought — entrada SHORT
RSI_MID  = 50    # salida (exportado para main.py)

# Multiplicadores SL/TP sobre ATR
ATR_LEN     = 14
ATR_MULT_SL = 2.0
ATR_MULT_TP = 4.0

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
    # Necesitamos al menos 60 velas para RSI(14) + historia
    if not candles_15m or len(candles_15m) < 60:
        return None

    close  = np.array([c["close"]  for c in candles_15m], dtype=float)
    high   = np.array([c["high"]   for c in candles_15m], dtype=float)
    low    = np.array([c["low"]    for c in candles_15m], dtype=float)

    # ── RSI 14 ──────────────────────────────────────────────────────────────
    rsi = _rsi_wilder(close, RSI_LEN)
    if np.isnan(rsi[-1]) or np.isnan(rsi[-2]):
        logger.info(f"[scanner] {sym}: RSI NaN — datos insuficientes")
        return None

    rsi_curr = float(rsi[-1])
    rsi_prev = float(rsi[-2])

    # ── ATR para SL/TP ──────────────────────────────────────────────────────
    atr_arr = _atr(high, low, close, ATR_LEN)
    atr_val = float(atr_arr[-1]) if not np.isnan(atr_arr[-1]) else 0.0
    last    = float(close[-1])

    # ── Bias 4H ─────────────────────────────────────────────────────────────
    bias = _bias_4h(candles_4h)

    # ── Señal de entrada ─────────────────────────────────────────────────────
    # LONG:  RSI cruza >30 desde abajo (sale de sobreventa)
    # SHORT: RSI cruza <70 desde arriba (sale de sobrecompra)
    if rsi_prev <= RSI_OS and rsi_curr > RSI_OS:
        raw_signal = "LONG"
    elif rsi_prev >= RSI_OB and rsi_curr < RSI_OB:
        raw_signal = "SHORT"
    else:
        raw_signal = "ESPERAR"

    # ── Filtro de tendencia 4H ───────────────────────────────────────────────
    # Tendencia alcista: solo LONGs
    # Tendencia bajista: solo SHORTs
    # Neutral: ambos permitidos
    filtro = None
    if raw_signal == "LONG"  and bias == -1:
        signal = "ESPERAR"
        filtro = "contra_tendencia_4H_bajista"
    elif raw_signal == "SHORT" and bias == 1:
        signal = "ESPERAR"
        filtro = "contra_tendencia_4H_alcista"
    else:
        signal = raw_signal

    # ── SL / TP basados en ATR ───────────────────────────────────────────────
    if signal == "LONG" and atr_val > 0:
        sl  = round(last - atr_val * ATR_MULT_SL, 5)
        tp1 = round(last + atr_val * ATR_MULT_TP, 5)
    elif signal == "SHORT" and atr_val > 0:
        sl  = round(last + atr_val * ATR_MULT_SL, 5)
        tp1 = round(last - atr_val * ATR_MULT_TP, 5)
    else:
        sl = tp1 = 0.0

    result = {
        "signal":     signal,
        "raw_signal": raw_signal,
        "rsi":        round(rsi_curr, 2),
        "rsi_prev":   round(rsi_prev, 2),
        "rsi_exit":   RSI_MID,        # main.py usa este valor para cerrar
        "entry":      last,
        "sl":         sl,
        "tp1":        tp1,
        "atr":        round(atr_val, 5),
        "bias_4h":    bias,
        "regime":     regime or "NEUTRAL",
    }
    if filtro:
        result["filtro"] = filtro

    return result


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
                    "signal": "ESPERAR", "rsi": 0,
                    "rsi_exit": RSI_MID,
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
                f"RSI={res['rsi']:.1f} prev={res['rsi_prev']:.1f} "
                f"bias4h={res['bias_4h']:+d} "
                f"raw={res.get('raw_signal','?')} "
                f"filtro={res.get('filtro','-')}"
            )

        except Exception as e:
            logger.error(f"[scanner] {sym}: {e}")
            results[sym] = {
                "signal": "ESPERAR", "rsi": 0,
                "rsi_exit": RSI_MID,
                "error": str(e),
            }

    return results
