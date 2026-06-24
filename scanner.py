"""
scanner.py - Bot Scalper v6
RSI adaptativo por regimen 4H con pullback validation y candle confirmation.
"""
import logging
import numpy as np
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

RSI_LEN              = 14
BULL_DIP_LEVEL       = 45
BULL_TRIGGER         = 50
BULL_TP_RSI          = 70
BEAR_RALLY_LEVEL     = 55
BEAR_TRIGGER         = 50
BEAR_TP_RSI          = 30
NEUTRAL_LONG_TRIG    = 35
NEUTRAL_SHORT_TRIG   = 65
NEUTRAL_LONG_TP      = 65
NEUTRAL_SHORT_TP     = 35
MOMENTUM_FADE_LEVEL  = 50
PULLBACK_LOOKBACK    = 6
ATR_LEN              = 14
ATR_MULT_SL          = 2.0
ATR_MULT_TP          = 4.0
COOLDOWN_VELAS       = 3
TIME_STOP_BARS       = 10

CORRELATION_GROUPS = [
    {"DOGEUSD", "XRPUSD", "SOLUSD"},
    {"AAPL", "MSFT"},
    {"AMZN", "TSLA"},
]


def _ema(arr, period):
    k = 2.0 / (period + 1)
    out = np.full(len(arr), np.nan, dtype=float)
    start = 0
    for i, v in enumerate(arr):
        if not np.isnan(v):
            out[i] = v
            start = i + 1
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
    delta = np.diff(close.astype(float))
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    avg_g = np.full(len(gain), np.nan)
    avg_l = np.full(len(loss), np.nan)
    if period <= len(gain):
        avg_g[period - 1] = np.mean(gain[:period])
        avg_l[period - 1] = np.mean(loss[:period])
        for i in range(period, len(gain)):
            avg_g[i] = (avg_g[i - 1] * (period - 1) + gain[i]) / period
            avg_l[i] = (avg_l[i - 1] * (period - 1) + loss[i]) / period
    rs = np.where(avg_l == 0, 100.0, avg_g / avg_l)
    rsi = 100.0 - 100.0 / (1 + rs)
    return np.concatenate([[np.nan], rsi])


def _atr(high, low, close, period=ATR_LEN):
    tr = np.maximum(
        high[1:] - low[1:],
        np.maximum(np.abs(high[1:] - close[:-1]),
                   np.abs(low[1:] - close[:-1]))
    )
    atr = np.full(len(close), np.nan)
    if period < len(tr):
        atr[period] = np.mean(tr[:period])
        for i in range(period + 1, len(close)):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i - 1]) / period
    return atr


def _classify_4h_regime(candles_4h):
    if not candles_4h or len(candles_4h) < 25:
        return "neutral"
    close = np.array([c["close"] for c in candles_4h], dtype=float)
    ema20 = _ema(close, 20)
    last_close = close[-1]
    last_ema = ema20[-1]
    if np.isnan(last_ema) or last_ema == 0:
        return "neutral"
    dist = (last_close - last_ema) / last_ema
    slope_up = ema20[-1] > ema20[-4] if len(ema20) >= 4 else False
    slope_down = ema20[-1] < ema20[-4] if len(ema20) >= 4 else False
    NEUTRAL_BUFFER = 0.001
    if dist > NEUTRAL_BUFFER and slope_up:
        return "bullish"
    if dist < -NEUTRAL_BUFFER and slope_down:
        return "bearish"
    return "neutral"


def _score_symbol(sym, candles_15m, candles_4h):
    if not candles_15m or len(candles_15m) < 60:
        return None

    close = np.array([c["close"] for c in candles_15m], dtype=float)
    open_ = np.array([c["open"] for c in candles_15m], dtype=float)
    high = np.array([c["high"] for c in candles_15m], dtype=float)
    low = np.array([c["low"] for c in candles_15m], dtype=float)

    rsi = _rsi_wilder(close, RSI_LEN)
    if np.isnan(rsi[-1]) or np.isnan(rsi[-2]):
        return None

    rsi_curr = float(rsi[-1])
    rsi_prev = float(rsi[-2])

    atr_arr = _atr(high, low, close, ATR_LEN)
    atr_val = float(atr_arr[-1]) if not np.isnan(atr_arr[-1]) else 0.0
    last = float(close[-1])

    green_candle = close[-1] > open_[-1]
    red_candle = close[-1] < open_[-1]

    regime = _classify_4h_regime(candles_4h)

    lookback_start = -(PULLBACK_LOOKBACK + 1)
    recent_rsi = rsi[lookback_start:-1]
    recent_rsi_valid = recent_rsi[~np.isnan(recent_rsi)]

    signal = "ESPERAR"
    long_tp = BULL_TP_RSI
    short_tp = BEAR_TP_RSI
    filtro = None

    if regime == "bullish":
        long_tp = BULL_TP_RSI
        short_tp = BULL_TP_RSI
        had_pullback = len(recent_rsi_valid) > 0 and np.any(recent_rsi_valid <= BULL_DIP_LEVEL)
        crossed_up = rsi_prev < BULL_TRIGGER and rsi_curr >= BULL_TRIGGER
        if had_pullback and crossed_up and green_candle:
            signal = "LONG"
        elif not had_pullback and crossed_up:
            filtro = "sin_pullback_previo"
        elif not crossed_up:
            filtro = "RSI_no_cruzo_50_curr={:.1f}".format(rsi_curr)

    elif regime == "bearish":
        long_tp = BEAR_TP_RSI
        short_tp = BEAR_TP_RSI
        had_rally = len(recent_rsi_valid) > 0 and np.any(recent_rsi_valid >= BEAR_RALLY_LEVEL)
        crossed_dn = rsi_prev > BEAR_TRIGGER and rsi_curr <= BEAR_TRIGGER
        if had_rally and crossed_dn and red_candle:
            signal = "SHORT"
        elif not had_rally and crossed_dn:
            filtro = "sin_rally_previo"
        elif not crossed_dn:
            filtro = "RSI_no_cruzo_50_curr={:.1f}".format(rsi_curr)

    else:
        long_tp = NEUTRAL_LONG_TP
        short_tp = NEUTRAL_SHORT_TP
        if rsi_prev < NEUTRAL_LONG_TRIG and rsi_curr >= NEUTRAL_LONG_TRIG and green_candle:
            signal = "LONG"
        elif rsi_prev > NEUTRAL_SHORT_TRIG and rsi_curr <= NEUTRAL_SHORT_TRIG and red_candle:
            signal = "SHORT"

    if signal == "LONG" and atr_val > 0:
        sl = round(last - atr_val * ATR_MULT_SL, 5)
        tp1 = round(last + atr_val * ATR_MULT_TP, 5)
    elif signal == "SHORT" and atr_val > 0:
        sl = round(last + atr_val * ATR_MULT_SL, 5)
        tp1 = round(last - atr_val * ATR_MULT_TP, 5)
    else:
        sl = tp1 = 0.0

    result = {
        "signal": signal,
        "rsi": round(rsi_curr, 2),
        "rsi_prev": round(rsi_prev, 2),
        "regime": regime,
        "long_tp_rsi": long_tp,
        "short_tp_rsi": short_tp,
        "momentum_fade_level": MOMENTUM_FADE_LEVEL,
        "entry": last,
        "sl": sl,
        "tp1": tp1,
        "atr": round(atr_val, 5),
        "green_candle": green_candle,
    }
    if filtro:
        result["filtro"] = filtro
    return result


def run_scanner(data_15m, data_4h, open_positions=None, cooldown_until=None, regimes=None):
    if open_positions is None:
        open_positions = set()
    if cooldown_until is None:
        cooldown_until = {}

    now = datetime.now(timezone.utc)
    results = {}

    for sym, candles in data_15m.items():
        candles_4h = (data_4h or {}).get(sym)
        try:
            res = _score_symbol(sym, candles, candles_4h)
            if res is None:
                results[sym] = {
                    "signal": "ESPERAR", "rsi": 0, "regime": "neutral",
                    "long_tp_rsi": BULL_TP_RSI, "short_tp_rsi": BEAR_TP_RSI,
                    "momentum_fade_level": MOMENTUM_FADE_LEVEL,
                    "error": "datos_insuficientes",
                }
                continue

            if res["signal"] in ("LONG", "SHORT"):
                cd = cooldown_until.get(sym)
                if cd and now < cd:
                    remaining = int((cd - now).total_seconds() / 60)
                    res["signal"] = "ESPERAR"
                    res["filtro"] = "cooldown:{}min".format(remaining)

            if res["signal"] in ("LONG", "SHORT") and sym in open_positions:
                res["signal"] = "ESPERAR"
                res["filtro"] = "ya_abierto"

            if res["signal"] in ("LONG", "SHORT"):
                for grupo in CORRELATION_GROUPS:
                    if sym in grupo:
                        bloq = grupo & open_positions
                        if bloq:
                            res["signal"] = "ESPERAR"
                            res["filtro"] = "correlacion:{}".format(bloq)
                            break

            results[sym] = res
            logger.info(
                "[scanner] {}: {} | RSI={:.1f} prev={:.1f} | regime={} | filtro={}".format(
                    sym, res["signal"], res["rsi"], res["rsi_prev"],
                    res["regime"], res.get("filtro", "-")
                )
            )

        except Exception as e:
            logger.error("[scanner] {}: {}".format(sym, e))
            results[sym] = {
                "signal": "ESPERAR", "rsi": 0, "regime": "neutral",
                "long_tp_rsi": BULL_TP_RSI, "short_tp_rsi": BEAR_TP_RSI,
                "momentum_fade_level": MOMENTUM_FADE_LEVEL,
                "error": str(e),
            }

    return results
