"""
data_feed.py — Bot Scalper v7
Descarga velas de 5 minutos de Capital.com (confirmado: la API soporta
resolution=MINUTE_5 sin restricciones de cuenta demo/real).

v7 elimina el fetch de 4H — ya no hay filtro de tendencia superior.
El scalper opera mean-reversion pura en ambas direcciones.
"""

import logging
import requests

logger = logging.getLogger(__name__)

BASE_URL = "https://demo-api-capital.backend-capital.com"

# v7.19: universo reconstruido — se sacan las 12 criptomonedas y GBPJPY
# (spread ~0.5%, 5-15x mas ancho que el resto, correlacionado con las
# peores perdidas del 22/07/2026 — ver detalle en capital_client.py) y se
# suman activos verificados en vivo con mejor spread y mejor ajuste al
# target de $2-3 por operacion. DEBE mantenerse en sync con SYMBOL_MAP de
# capital_client.py (mismas claves).
CAPITAL_EPICS = {
    "US100":   "US100",
    "AMZN":    "AMZN",
    "TSLA":    "TSLA",
    "AAPL":    "AAPL",
    "MSFT":    "MSFT",
    "META":    "META",
    "NFLX":    "NFLX",
    "COIN":    "COIN",
    "JPM":     "JPM",
    "NATGAS":  "NATURALGAS",
    "GOLD":    "GOLD",
    "SILVER":  "SILVER",
    "USOIL":   "OIL_CRUDE",
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
    Descarga velas de 5 minutos para los 13 activos del scalper (v7.19).
    100 velas = ~8.3 horas de historia, suficiente para RSI(14) y ATR(14).
    Retorna: data_5m: { sym: [candles] | None }
    """
    data_5m = {}

    for sym, epic in CAPITAL_EPICS.items():
        try:
            data_5m[sym] = _fetch(epic, client, "MINUTE_5", 100)
            logger.info(f"[data_feed] {sym} 5m: {len(data_5m[sym])} velas OK")
        except Exception as e:
            logger.warning(f"[data_feed] {sym} 5m: ERROR — {e}")
            data_5m[sym] = None

    return data_5m
