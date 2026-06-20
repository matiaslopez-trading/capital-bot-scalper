"""
regime_detector.py
Detecta el regimen de mercado por activo: ALCISTA, BAJISTA o LATERAL.
Usa velas diarias con EMA20 + ADX(14).
Histeresis: necesita 3 de los ultimos 5 dias para cambiar de regimen.
Persiste el historial en regime_state.json para sobrevivir reinicios de Railway.
"""

import json
import logging
import os
import requests
import numpy as np
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

STATE_FILE  = "regime_state.json"
HISTORY_LEN = 5   # dias de historial por activo
CONFIRM_N   = 3   # cuantos de los ultimos HISTORY_LEN deben coincidir

BASE_URL = "https://demo-api-capital.backend-capital.com"

# Ajuste de parametros segun regimen
# umbral_extra: se suma al UMBRAL base del scanner (+1 = mas restrictivo)
# sizing_mult:  multiplicador del sizing (0.5 = mitad del tamano)
REGIME_PARAMS = {
    "ALCISTA": {"umbral_extra": -1, "sizing_mult": 1.0},
    "BAJISTA": {"umbral_extra": -1, "sizing_mult": 1.0},
    "LATERAL": {"umbral_extra":  1, "sizing_mult": 0.5},
}


# ── Indicadores internos ────────────────────────────────────────

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


def _adx_last(high, low, close, period=14):
    """Calcula el ultimo valor de ADX. Retorna float o nan."""
    n = len(close)
    if n < period * 2 + 2:
        return np.nan
    tr = np.maximum(high[1:] - low[1:],
                    np.maximum(np.abs(high[1:] - close[:-1]),
                               np.abs(low[1:] - close[:-1])))
    up_move   = high[1:] - high[:-1]
    down_move = low[:-1] - low[1:]
    plus_dm  = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    atr_s   = np.full(n - 1, np.nan)
    plus_s  = np.full(n - 1, np.nan)
    minus_s = np.full(n - 1, np.nan)
    atr_s[period - 1]   = np.sum(tr[:period])
    plus_s[period - 1]  = np.sum(plus_dm[:period])
    minus_s[period - 1] = np.sum(minus_dm[:period])
    for i in range(period, n - 1):
        atr_s[i]   = atr_s[i-1]   - atr_s[i-1] / period   + tr[i]
        plus_s[i]  = plus_s[i-1]  - plus_s[i-1] / period  + plus_dm[i]
        minus_s[i] = minus_s[i-1] - minus_s[i-1] / period + minus_dm[i]
    plus_di  = 100 * np.where(atr_s > 0, plus_s / atr_s, 0.0)
    minus_di = 100 * np.where(atr_s > 0, minus_s / atr_s, 0.0)
    dx = 100 * np.abs(plus_di - minus_di) / np.where(
        (plus_di + minus_di) > 0, plus_di + minus_di, 1.0)
    adx_arr = np.full(n - 1, np.nan)
    adx_arr[period * 2 - 2] = np.mean(dx[period - 1: period * 2 - 1])
    for i in range(period * 2 - 1, n - 1):
        adx_arr[i] = (adx_arr[i - 1] * (period - 1) + dx[i]) / period
    val = adx_arr[-1]
    return float(val) if not np.isnan(val) else np.nan


# ── Deteccion de regimen ────────────────────────────────────────

def _detect_raw(candles_daily):
    """
    Calcula el regimen puntual para un activo dado sus velas diarias.
    Retorna: "ALCISTA" | "BAJISTA" | "LATERAL"
    """
    if not candles_daily or len(candles_daily) < 30:
        return "LATERAL"
    close = np.array([c["close"] for c in candles_daily], dtype=float)
    high  = np.array([c["high"]  for c in candles_daily], dtype=float)
    low   = np.array([c["low"]   for c in candles_daily], dtype=float)
    ema20 = _ema(close, 20)
    # Pendiente EMA20 ultimos 5 dias
    slope = float(ema20[-1]) - float(ema20[-5]) if not np.isnan(ema20[-5]) else 0.0
    adx   = _adx_last(high, low, close, 14)
    price = float(close[-1])
    ema20_last = float(ema20[-1]) if not np.isnan(ema20[-1]) else price
    if np.isnan(adx) or adx < 20:
        return "LATERAL"
    if price > ema20_last and slope > 0:
        return "ALCISTA"
    if price < ema20_last and slope < 0:
        return "BAJISTA"
    return "LATERAL"


# ── Fetch de velas diarias ──────────────────────────────────────

def _fetch_daily(epic, client, limit=50):
    """Descarga velas diarias para un epic. Usa la sesion del client."""
    client.ensure_session()
    url     = f"{BASE_URL}/api/v1/prices/{epic}"
    params  = {"resolution": "DAY", "max": limit}
    headers = {
        "CST":              client.cst,
        "X-SECURITY-TOKEN": client.x_token,
        "Content-Type":     "application/json",
    }
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    prices = resp.json().get("prices", [])
    rows = []
    for p in prices:
        def mid(obj):
            bid = float(obj.get("bid", 0) or 0)
            ask = float(obj.get("ask", 0) or 0)
            return (bid + ask) / 2.0 if bid and ask else (bid or ask)
        rows.append({
            "open":   mid(p["openPrice"]),
            "high":   mid(p["highPrice"]),
            "low":    mid(p["lowPrice"]),
            "close":  mid(p["closePrice"]),
            "volume": float(p.get("lastTradedVolume", 0) or 0),
        })
    return rows


# ── Estado persistido ───────────────────────────────────────────

_state = {}
_last_update_date = None


def _load_state():
    global _state
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                _state = json.load(f)
            logger.info(f"[regime] Estado cargado: {list(_state.keys())}")
        except Exception as e:
            logger.warning(f"[regime] No se pudo cargar estado: {e}")
            _state = {}


def _save_state():
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(_state, f)
    except Exception as e:
        logger.warning(f"[regime] No se pudo guardar estado: {e}")


# Cargar estado al importar el modulo
_load_state()


# ── API publica ─────────────────────────────────────────────────

def update_regimes(all_data_daily: dict) -> dict:
    """
    Actualiza el regimen de cada activo usando velas diarias ya descargadas.
    Llamar una vez por dia desde main.py pasando el dict de velas diarias.

    all_data_daily: { sym: [candles] }
    Retorna: { sym: "ALCISTA"|"BAJISTA"|"LATERAL" }
    """
    global _state, _last_update_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _last_update_date == today:
        return get_regimes()
    for sym, candles in all_data_daily.items():
        raw = _detect_raw(candles)
        if sym not in _state:
            _state[sym] = {"history": [], "current": raw}
        hist = _state[sym]["history"]
        hist.append(raw)
        if len(hist) > HISTORY_LEN:
            hist.pop(0)
        # Histeresis: cambiar regimen solo si CONFIRM_N dias lo confirman
        for candidate in ("ALCISTA", "BAJISTA", "LATERAL"):
            if hist.count(candidate) >= CONFIRM_N:
                if _state[sym]["current"] != candidate:
                    logger.info(
                        f"[regime] {sym}: CAMBIO {_state[sym]['current']} → {candidate} "
                        f"(hist={hist})"
                    )
                _state[sym]["current"] = candidate
                break
        logger.info(
            f"[regime] {sym}: raw={raw} regimen={_state[sym]['current']} hist={hist}"
        )
    _save_state()
    _last_update_date = today
    return get_regimes()


def fetch_and_update(client, epics: dict) -> dict:
    """
    Descarga velas diarias y actualiza regimenes.
    Usar en bots que NO tienen velas diarias ya disponibles (ej: Scalper).

    epics: { sym: epic_name }
    Retorna: { sym: "ALCISTA"|"BAJISTA"|"LATERAL" }
    """
    global _last_update_date
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _last_update_date == today:
        return get_regimes()
    daily_data = {}
    for sym, epic in epics.items():
        try:
            daily_data[sym] = _fetch_daily(epic, client, limit=50)
        except Exception as e:
            logger.warning(f"[regime] {sym}: no se pudo obtener velas diarias: {e}")
            daily_data[sym] = None
    # Filtrar solo los que tienen datos
    valid = {sym: c for sym, c in daily_data.items() if c}
    return update_regimes(valid)


def get_regimes() -> dict:
    """Retorna el regimen actual de todos los activos."""
    return {sym: v["current"] for sym, v in _state.items()}


def get_regime(sym: str) -> str:
    """Retorna el regimen actual de un activo. Default: LATERAL."""
    return _state.get(sym, {}).get("current", "LATERAL")


def get_params(sym: str) -> dict:
    """Retorna los ajustes de parametros segun regimen del activo."""
    regime = get_regime(sym)
    return REGIME_PARAMS.get(regime, REGIME_PARAMS["LATERAL"])
