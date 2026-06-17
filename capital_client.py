"""
capital_client.py — Bot Scalper
Wrapper para la API REST de Capital.com (demo).
Igual al Bot Swing pero con MIN_SIZE calibrado para scalping.
"""

import os
import time
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

BASE_URL = "https://demo-api-capital.backend-capital.com"
SESSION_TTL = 540  # 9 minutos

API_KEY    = os.environ.get("CAPITAL_API_KEY", "")
API_PASS   = os.environ.get("CAPITAL_PASSWORD", "")
API_EMAIL  = os.environ.get("CAPITAL_EMAIL", "")

# Epics de Capital.com para cada simbolo
SYMBOL_MAP = {
    "BTCUSD":  "BITCOIN",
    "ETHUSD":  "ETHEREUM",
    "NVDA":    "NVDA",
    "NDAQ":    "NDAQ",
    "SILVER":  "SILVER",
    "GBPUSD":  "GBPUSD",
    "GOLD":    "GOLD",
    "USOIL":   "OIL_CRUDE",
    "EURUSD":  "EURUSD",
    "US500":   "US500",
}

# Tamano minimo de posicion por instrumento
MIN_SIZE = {
    "BITCOIN":   0.01,
    "ETHEREUM":  0.1,
    "NVDA":      1.0,
    "NDAQ":      1.0,
    "SILVER":    1.0,
    "GBPUSD":    1000.0,
    "GOLD":      0.1,
    "OIL_CRUDE": 1.0,
    "EURUSD":    1000.0,
    "US500":     0.1,
}


class CapitalClient:
    def __init__(self):
        self.api_key    = os.environ["CAPITAL_API_KEY"]
        self.password   = os.environ["CAPITAL_PASSWORD"]
        self.email      = os.environ["CAPITAL_EMAIL"]
        self.cst        = None
        self.security   = None
        self.session_ts = 0

    def _headers(self):
        return {
            "CST":              self.cst,
            "X-SECURITY-TOKEN": self.security,
            "Content-Type":     "application/json",
        }

    def login(self):
        url  = f"{BASE_URL}/api/v1/session"
        body = {
            "identifier":        self.email,
            "password":          self.password,
            "encryptedPassword": False,
        }
        hdrs = {"X-CAP-API-KEY": self.api_key, "Content-Type": "application/json"}
        resp = requests.post(url, json=body, headers=hdrs, timeout=15)
        resp.raise_for_status()
        self.cst        = resp.headers.get("CST")
        self.security   = resp.headers.get("X-SECURITY-TOKEN")
        self.session_ts = time.time()
        logger.info("[client] Login OK")

    def ensure_session(self):
        if time.time() - self.session_ts > SESSION_TTL:
            logger.info("[client] Sesion expirada, re-login...")
            self.login()

    def get_positions(self):
        self.ensure_session()
        url  = f"{BASE_URL}/api/v1/positions"
        resp = requests.get(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json().get("positions", [])

    def get_accounts(self):
        """Retorna info de la cuenta (balance, equity, P&L del dia)."""
        self.ensure_session()
        url  = f"{BASE_URL}/api/v1/accounts"
        resp = requests.get(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json().get("accounts", [])

    def get_activity_history(self, days=90):
        """Retorna historial de posiciones cerradas con P&L."""
        self.ensure_session()
        now     = datetime.utcnow()
        from_dt = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S")
        to_dt   = now.strftime("%Y-%m-%dT%H:%M:%S")
        params  = {
            "from":     from_dt,
            "to":       to_dt,
            "pageSize": 500,
        }
        url  = f"{BASE_URL}/api/v1/history/activity"
        resp = requests.get(url, headers=self._headers(), params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        return data.get("activities", [])

    def open_position(self, symbol, action, entry, sl, tp1):
        epic = SYMBOL_MAP.get(symbol)
        if not epic:
            logger.warning(f"[client] Simbolo desconocido: {symbol}")
            return None

        # Verificar que no haya posicion abierta para este simbolo
        positions = self.get_positions()
        for p in positions:
            if p.get("market", {}).get("epic") == epic:
                logger.info(f"[client] {symbol}: ya tiene posicion abierta, omitiendo")
                return None

        direction = "BUY" if action == "LONG" else "SELL"
        size      = MIN_SIZE.get(epic, 1.0)

        self.ensure_session()
        url  = f"{BASE_URL}/api/v1/positions"
        body = {
            "epic":           epic,
            "direction":      direction,
            "size":           size,
            "guaranteedStop": False,
            "stopLevel":      sl,
            "profitLevel":    tp1,
        }
        resp = requests.post(url, json=body, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"[client] {symbol}: {direction} size={size} sl={sl} tp={tp1} — {data}")
        return data

    def close_position(self, deal_id):
        self.ensure_session()
        url  = f"{BASE_URL}/api/v1/positions/{deal_id}"
        resp = requests.delete(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        logger.info(f"[client] Posicion {deal_id} cerrada")
        return resp.json()

    def close_all(self, symbol):
        epic      = SYMBOL_MAP.get(symbol)
        positions = self.get_positions()
        closed    = False
        for p in positions:
            if p.get("market", {}).get("epic") == epic:
                deal_id = p.get("position", {}).get("dealId")
                if deal_id:
                    try:
                        self.close_position(deal_id)
                        closed = True
                    except Exception as e:
                        logger.error(f"[client] Error cerrando {deal_id}: {e}")
        return closed
