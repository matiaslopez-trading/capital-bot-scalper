"""
scanner.py — Bot Scalper
Replica MacroSignal en velas 15min para Capital.com.
Indicadores ajustados para alta frecuencia:
  - EMA 9/21 (trend rapido)
  - RSI 7 (mas sensible, peso doble si <30/>70)
  - MACD (5, 13, 4)
  - ATR 7 (stops dinamicos)
  - Bollinger Bands (20, 2.0)
  - Volumen relativo 1.5x
  - Bias de 4H como filtro direccional
  - TP1=1.5%, SL=0.7%
"""

import logging
import numpy as np

logger = logging.getLogger(__name__)

RSI_LEN   = 7
MACD_FAST = 5
MACD_SLOW = 13
MACD_SIG  = 4
EMA_FAST  = 9
EMA_SLOW  = 21
ATR_LEN   = 7
BB_LEN    = 20
BB_STD    = 2.0
VOL_MULT  = 1.5
TP1_PCT   = 0.015
SL_PCT    = 0.007
UMBRAL    = 2

GOLD_SYMS = {"GOLD", "SILVER"}


def _ema(arr, period):
    k = 2.0 / (period + 1)
    out = np.empty(len(arr))
    out[0] = arr[0]
    for i in range(1, len(arr)):
        out[i] = arr[i] * k + out[i - 1] * (1 - k)
    return out


def _rsi(close, period=RSI_LEN):
    delta = np.diff(close)
    gain  = np.where(delta > 0, delta, 0.0)
    loss  = np.where(delta < 0, -delta, 0.0)
    avg_g = np.convolve(gain, np.ones(period) / period, mode='valid')
    avg_l = np.convolve(loss, np.ones(period) / period, mode='valid')
    rs    = np.where(avg_l == 0, 100.0, avg_g / avg_l)
    return 100 - (100 / (1 + rs))


def _macd(close, fast=MACD_FAST, slow=MACD_SLOW, sig=MACD_SIG):
    ema_f  = _ema(close, fast)
    ema_s  = _ema(close, slow)
    line   = ema_f - ema_s
    signal = _ema(line, sig)
    hist   = line - signal
    return line, signal, hist


def _bollinger(close, period=BB_LEN, std=BB_STD):
    upper, lower = [], []
    for i in range(period - 1, len(close)):
        w  = close[i - period + 1: i + 1]
        m  = np.mean(w)
        sd = np.std(w)
        upper.append(m + std * sd)
        lower.append(m - std * sd)
    return np.array(upper), np.array(lower)


def _bias_4h(candles_4h):
    if not candles_4h or len(candles_4h) < 25:
        return 0
    close  = np.array([c["close"] for c in candles_4h], dtype=float)
    ema20  = _ema(close, 20)
    diff   = (close[-1] - ema20[-1]) / ema20[-1]
    if diff > 0.001:
        return 1
    elif diff < -0.001:
        return -1
    return 0


def _score_symbol(sym, candles_15m, candles_4h):
    if not candles_15m or len(candles_15m) < 50:
        return None

    close  = np.array([c["close"]  for c in candles_15m], dtype=float)
    high   = np.array([c["high"]   for c in candles_15m], dtype=float)
    low    = np.array([c["low"]    for c in candles_15m], dtype=float)
    volume = np.array([c["volume"] for c in candles_15m], dtype=float)

    score   = 0
    details = {}

    # 1. EMA 9/21
    ema_f = _ema(close, EMA_FAST)
    ema_s = _ema(close, EMA_SLOW)
    ema_sig = 1 if ema_f[-1] > ema_s[-1] else -1
    score += ema_sig
    details["ema"] = ema_sig

    # 2. RSI 7 con peso doble en extremos
    rsi_arr = _rsi(close)
    rsi_val = rsi_arr[-1]
    if rsi_val < 30:
        score += 2
        rsi_sig = 2
    elif rsi_val > 70:
        score -= 2
        rsi_sig = -2
    elif rsi_val < 45:
        score += 1
        rsi_sig = 1
    elif rsi_val > 55:
        score -= 1
        rsi_sig = -1
    else:
        rsi_sig = 0
    details["rsi"] = rsi_sig

    # 3. MACD histograma
    _, _, hist = _macd(close)
    macd_sig = 1 if hist[-1] > 0 else -1
    score += macd_sig
    details["macd"] = macd_sig

    # 4. Bollinger Bands
    bb_upper, bb_lower = _bollinger(close)
    last = close[-1]
    if last < bb_lower[-1]:
        score += 1
        bb_sig = 1
    elif last > bb_upper[-1]:
        score -= 1
        bb_sig = -1
    else:
        bb_sig = 0
    details["bb"] = bb_sig

    # 5. Volumen relativo
    vol_avg = np.mean(volume[-20:])
    if volume[-1] > vol_avg * VOL_MULT:
        if score > 0:
            score += 1
        elif score < 0:
            score -= 1
        details["vol"] = 1
    else:
        details["vol"] = 0

    # 6. Bias 4H
    bias = _bias_4h(candles_4h)
    details["bias_4h"] = bias
    if bias != 0 and np.sign(score) != np.sign(bias) and abs(score) < 3:
        logger.info(f"[scanner] {sym}: bloqueado bias 4H ({bias:+d}) score={score}")
        return {"signal": "ESPERAR", "score": score, "details": details,
                "entry": float(last), "sl": 0, "tp1": 0, "rsi": round(float(rsi_val), 2)}

    if score >= UMBRAL:
        signal = "LONG"
    elif score <= -UMBRAL:
        signal = "SHORT"
    else:
        signal = "ESPERAR"

    if signal == "LONG":
        sl  = round(last * (1 - SL_PCT), 5)
        tp1 = round(last * (1 + TP1_PCT), 5)
    elif signal == "SHORT":
        sl  = round(last * (1 + SL_PCT), 5)
        tp1 = round(last * (1 - TP1_PCT), 5)
    else:
        sl = tp1 = 0

    return {"signal": signal, "score": score, "details": details,
            "entry": float(last), "sl": sl, "tp1": tp1,
            "rsi": round(float(rsi_val), 2)}


def run_scanner(data_15m, data_4h):
    results = {}
    for sym, candles in data_15m.items():
        candles_4h = (data_4h or {}).get(sym)
        try:
            res = _score_symbol(sym, candles, candles_4h)
            if res:
                results[sym] = res
                logger.info(f"[scanner] {sym}: {res['signal']} score={res['score']} rsi={res.get('rsi','?')}")
        except Exception as e:
            logger.error(f"[scanner] {sym}: {e}")
    return results
