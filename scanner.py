"""
scanner.py â Bot Scalper v3.1
Cambios vs v3:
- ATR_LEN: 7 -> 14  (mejor estimacion de volatilidad real)
- ATR_MULT_SL: 1.5 -> 2.5  (SL mas amplio, fuera del ruido)
- ATR_MULT_TP: 3.0 -> 5.0  (mantiene R:R 2:1 con el nuevo SL)
- ADX filter agregado: ADX < 20 -> ESPERAR (no operar mercados laterales)
- Fix bias_4h: solo bloquea cuando score != 0
"""

import logging
import numpy as np
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

RSI_LEN        = 9
EMA_FAST       = 9
EMA_SLOW       = 21
ATR_LEN        = 14
BB_LEN         = 20
BB_STD         = 2.0
VOL_MULT       = 1.5
ATR_MULT_SL    = 2.5
ATR_MULT_TP    = 5.0
ADX_LEN        = 14
ADX_MIN        = 20
UMBRAL         = 2
COOLDOWN_VELAS = 2

# Activos v3 â alta volatilidad
CORRELATION_GROUPS = [
    {"DOGEUSD", "XRPUSD", "SOLUSD"},  # altcoins correlacionadas
    {"AAPL", "MSFT"},                   # mega cap tech
    {"AMZN", "TSLA"},                   # growth tech
]


def _ema(arr, period):
    k = 2.0 / (period + 1)
    out = np.full(len(arr), np.nan, dtype=float)
    out[0] = arr[0]
    for i in range(1, len(arr)):
        if np.isnan(out[i - 1]):
            out[i] = arr[i]
        else:
            out[i] = arr[i] * k + out[i - 1] * (1 - k)
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
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]),
                               np.abs(low[1:] - close[:-1])))
    atr = np.full(len(close), np.nan)
    if period < len(tr):
        atr[period] = np.mean(tr[:period])
        for i in range(period + 1, len(close)):
            atr[i] = (atr[i - 1] * (period - 1) + tr[i - 1]) / period
    return atr


def _adx(high, low, close, period=ADX_LEN):
    """ADX de Wilder â retorna array del mismo largo que close."""
    n   = len(close)
    out = np.full(n, np.nan)
    if n < period * 2 + 1:
        return out

    up   = high[1:] - high[:-1]
    down = low[:-1] - low[1:]
    tr   = np.maximum(high[1:] - low[1:],
                      np.maximum(np.abs(high[1:] - close[:-1]),
                                 np.abs(low[1:] - close[:-1])))
    dm_p = np.where((up > down) & (up > 0), up, 0.0)
    dm_m = np.where((down > up) & (down > 0), down, 0.0)

    # Inicializar con suma simple de los primeros `period` valores
    atr_s = np.full(len(tr), np.nan)
    dmp_s = np.full(len(tr), np.nan)
    dmm_s = np.full(len(tr), np.nan)

    atr_s[period - 1] = np.sum(tr[:period])
    dmp_s[period - 1] = np.sum(dm_p[:period])
    dmm_s[period - 1] = np.sum(dm_m[:period])

    for i in range(period, len(tr)):
        atr_s[i] = atr_s[i - 1] - atr_s[i - 1] / period + tr[i]
        dmp_s[i] = dmp_s[i - 1] - dmp_s[i - 1] / period + dm_p[i]
        dmm_s[i] = dmm_s[i - 1] - dmm_s[i - 1] / period + dm_m[i]

    denom  = np.where(atr_s == 0, 1e-10, atr_s)
    di_p   = 100 * dmp_s / denom
    di_m   = 100 * dmm_s / denom
    dx_den = np.where((di_p + di_m) == 0, 1e-10, di_p + di_m)
    dx     = 100 * np.abs(di_p - di_m) / dx_den

    # Suavizar DX para obtener ADX
    adx_arr = np.full(len(tr), np.nan)
    start   = 2 * period - 2
    if start < len(dx) and not np.isnan(dx[start - period + 1: start + 1]).any():
        adx_arr[start] = np.mean(dx[start - period + 1: start + 1])
        for i in range(start + 1, len(tr)):
            if not np.isnan(adx_arr[i - 1]) and not np.isnan(dx[i]):
                adx_arr[i] = (adx_arr[i - 1] * (period - 1) + dx[i]) / period

    # Mapear al array de close (tr tiene len-1)
    out[1:] = adx_arr
    return out


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
    close = np.array([c["close"] for c in candles_4h], dtype=float)
    ema20 = _ema(close, 20)
    diff  = (close[-1] - ema20[-1]) / ema20[-1]
    if diff > 0.001:
        return 1
    elif diff < -0.001:
        return -1
    return 0


def _score_symbol(sym, candles_15m, candles_4h, regime=None):
    if not candles_15m or len(candles_15m) < 50:
        return None

    close  = np.array([c["close"]  for c in candles_15m], dtype=float)
    high   = np.array([c["high"]   for c in candles_15m], dtype=float)
    low    = np.array([c["low"]    for c in candles_15m], dtype=float)
    volume = np.array([c["volume"] for c in candles_15m], dtype=float)

    score   = 0
    details = {}

    # ââ ADX filter âââââââââââââââââââââââââââââââââââââââââââââââââ
    adx_arr = _adx(high, low, close, ADX_LEN)
    adx_val = adx_arr[-1]
    details["adx"] = round(float(adx_val), 2) if not np.isnan(adx_val) else 0

    if np.isnan(adx_val) or adx_val < ADX_MIN:
        logger.info(f"[scanner] {sym}: ADX={details['adx']} < {ADX_MIN} â lateral, ESPERAR")
        return {
            "signal": "ESPERAR", "score": 0, "details": details,
            "entry": float(close[-1]), "sl": 0, "tp1": 0,
            "rsi": 0, "atr": 0,
            "filtro": f"adx:{details['adx']}",
            "regime": regime or "LATERAL",
        }

    # ââ EMA ââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    ema_f   = _ema(close, EMA_FAST)
    ema_s   = _ema(close, EMA_SLOW)
    ema_sig = 1 if ema_f[-1] > ema_s[-1] else -1
    score  += ema_sig
    details["ema"] = ema_sig

    # ââ RSI ââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    rsi_arr = _rsi_wilder(close, RSI_LEN)
    rsi_val = rsi_arr[-1]
    if np.isnan(rsi_val):
        rsi_sig = 0
    elif rsi_val < 30:
        score += 2;  rsi_sig = 2
    elif rsi_val > 70:
        score -= 2;  rsi_sig = -2
    elif rsi_val < 45:
        score += 1;  rsi_sig = 1
    elif rsi_val > 55:
        score -= 1;  rsi_sig = -1
    else:
        rsi_sig = 0
    details["rsi"] = rsi_sig

    # ââ Bollinger Bands ââââââââââââââââââââââââââââââââââââââââââââ
    bb_upper, bb_lower = _bollinger(close)
    last = close[-1]
    if last < bb_lower[-1]:
        score += 1;  bb_sig = 1
    elif last > bb_upper[-1]:
        score -= 1;  bb_sig = -1
    else:
        bb_sig = 0
    details["bb"] = bb_sig

    # ââ Volumen ââââââââââââââââââââââââââââââââââââââââââââââââââââ
    vol_avg = np.mean(volume[-20:])
    if vol_avg > 0 and volume[-1] > vol_avg * VOL_MULT:
        if score > 0:
            score += 1;  vol_sig = 1
        elif score < 0:
            score -= 1;  vol_sig = -1
        else:
            vol_sig = 0
    else:
        vol_sig = 0
    details["vol"] = vol_sig

    # ââ ATR ââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
    atr_arr = _atr(high, low, close, ATR_LEN)
    atr_val = atr_arr[-1]
    details["atr"] = round(float(atr_val), 5) if not np.isnan(atr_val) else 0

    # ââ Bias 4H ââââââââââââââââââââââââââââââââââââââââââââââââââââ
    bias = _bias_4h(candles_4h)
    details["bias_4h"] = bias
    if bias != 0 and score != 0 and int(np.sign(score)) != bias:
        logger.info(f"[scanner] {sym}: bloqueado bias 4H ({bias:+d}) score={score}")
        return {
            "signal": "ESPERAR", "score": score, "details": details,
            "entry": float(last), "sl": 0, "tp1": 0,
            "rsi": round(float(rsi_val), 2) if not np.isnan(rsi_val) else 0,
            "atr": details["atr"],
            "filtro": "bias_4h",
            "regime": regime or "LATERAL",
        }

    # ââ Umbral segun regimen âââââââââââââââââââââââââââââââââââââââ
    umbral_extra    = 1 if regime == "LATERAL" else 0
    umbral_efectivo = UMBRAL + umbral_extra

    if score >= umbral_efectivo:
        signal = "LONG"
    elif score <= -umbral_efectivo:
        signal = "SHORT"
    else:
        signal = "ESPERAR"

    # ââ SL / TP con ATR mas amplio âââââââââââââââââââââââââââââââââ
    if signal == "LONG" and not np.isnan(atr_val) and atr_val > 0:
        sl  = round(last - atr_val * ATR_MULT_SL, 5)
        tp1 = round(last + atr_val * ATR_MULT_TP, 5)
    elif signal == "SHORT" and not np.isnan(atr_val) and atr_val > 0:
        sl  = round(last + atr_val * ATR_MULT_SL, 5)
        tp1 = round(last - atr_val * ATR_MULT_TP, 5)
    else:
        sl = tp1 = 0

    return {
        "signal":  signal,
        "score":   score,
        "details": details,
        "entry":   float(last),
        "sl":      sl,
        "tp1":     tp1,
        "rsi":     round(float(rsi_val), 2) if not np.isnan(rsi_val) else 0,
        "atr":     details["atr"],
        "adx":     details["adx"],
        "regime":  regime or "LATERAL",
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
        regime     = regimes.get(sym)
        try:
            res = _score_symbol(sym, candles, candles_4h, regime=regime)
            if res is None:
                results[sym] = {"signal": "ESPERAR", "score": 0, "error": "datos_insuficientes"}
                continue

            if res["signal"] in ("LONG", "SHORT"):
                cd_until = cooldown_until.get(sym)
                if cd_until and now < cd_until:
                    remaining = int((cd_until - now).total_seconds() / 60)
                    logger.info(f"[scanner] {sym}: cooldown activo - {remaining} min restantes")
                    res["signal"] = "ESPERAR"
                    res["filtro"] = f"cooldown:{remaining}min"

            if res["signal"] in ("LONG", "SHORT") and sym in open_positions:
                res["signal"] = "ESPERAR"
                res["filtro"] = "ya_abierto"

            if res["signal"] in ("LONG", "SHORT"):
                for grupo in CORRELATION_GROUPS:
                    if sym in grupo:
                        bloqueado_por = grupo & open_positions
                        if bloqueado_por:
                            logger.info(f"[scanner] {sym}: bloqueado correlacion con {bloqueado_por}")
                            res["signal"] = "ESPERAR"
                            res["filtro"] = f"correlacion:{bloqueado_por}"
                            break

            results[sym] = res
            logger.info(
                f"[scanner] {sym}: {res['signal']} score={res['score']} "
                f"rsi={res.get('rsi','?')} atr={res.get('atr','?')} "
                f"adx={res.get('adx','?')} regime={res.get('regime','?')}"
            )
        except Exception as e:
            logger.error(f"[scanner] {sym}: {e}")
            results[sym] = {"signal": "ESPERAR", "score": 0, "error": str(e)}

    return results
