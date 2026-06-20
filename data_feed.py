"""
data_feed.py — Bot Scalper v3
Descarga velas OHLCV de Capital.com:
- 15min: para el scanner (100 velas)
- 4H:    para el bias direccional (50 velas)

Activos v3: alta volatilidad, separados del Bot Swing.
"""

import logging
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://demo-api-capital.backend-capital.com"

# 9 activos del Bot Scalper v3 — alta volatilidad, distintos al Swing
CAPITAL_EPICS = {
    "US100":   "US100",
    "GBPJPY":  "GBPJPY",
    "DOGEUSD": "DOGEUSD",
    "XRPUSD":  "XRPUSD",
    "SOLUSD":  "SOLUSD",
    "AMZN":    "AMZN",
    "TSLA":    "TSLA",
    "AAPL":    "AAPL",
    "MSFT":    "MSFT",
}


def _mid(price_obj):
    bid = float(price_obj.get("bid", 0) or 0)
    ask = float(price_obj.get("ask", 0) or 0)
    if bid and ask:
        return (bid + ask) / 2.0
    return bid or ask


def _fetch(epic, client, resolution, limit):
    client.ensure_session()
    url    = f"{BASE_URL}/api/v1/prices/{epic}"
    params = {"resolution": resolution, "max": limit}
    headers = {
        "CST":              client.cst,
        "X-SECURITY-TOKEN": client.x_token,
        "Content-Type":     "application/json",
    }
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    data   = resp.json()
    prices = data.get("prices", [])
    if not prices:
        raise ValueError(f"Sin datos para {epic} [{resolution}]")
    rows = []
    for p in prices:
        rows.append({
            "open":   _mid(p["openPrice"]),
            "high":   _mid(p["highPrice"]),
            "low":    _mid(p["lowPrice"]),
            "close":  _mid(p["closePrice"]),
            "volume": float(p.get("lastTradedVolume", 0) or 0),
        })
    return rows


def get_all_ohlcv(client):
    """
    Descarga velas 15min y 4H para los 9 activos.
    Retorna:
        data_15m: { sym: [candles] | None }
        data_4h:  { sym: [candles] | None }
    """
    data_15m = {}
    data_4h  = {}

    for sym, epic in CAPITAL_EPICS.items():
        try:
            data_15m[sym] = _fetch(epic, client, "MINUTE_15", 100)
            logger.info(f"[data_feed] {sym} 15m: {len(data_15m[sym])} velas OK")
        except Exception as e:
            logger.warning(f"[data_feed] {sym} 15m: ERROR — {e}")
            data_15m[sym] = None

        try:
            data_4h[sym] = _fetch(epic, client, "HOUR_4", 50)
            logger.info(f"[data_feed] {sym} 4H: {len(data_4h[sym])} velas OK")
        except Exception as e:
            logger.warning(f"[data_feed] {sym} 4H: ERROR — {e}")
            data_4h[sym] = None

    return data_15m, data_4h
