"""
capital_client.py
Cliente para la API REST de Capital.com (modo demo).
"""

import os
import time
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

BASE_URL    = "https://demo-api-capital.backend-capital.com"
SESSION_TTL = 540

SYMBOL_MAP = {
    "BTCUSD":   "BITCOIN",
    "ETHUSD":   "ETHEREUM",
    "NVDA":     "NVDA",
    "NDAQ":     "NDAQ",
    "SILVER":   "SILVER",
    "GBPUSD":   "GBPUSD",
    "GOLD":     "GOLD",
    "USOIL":    "OIL_CRUDE",
    "EURUSD":   "EURUSD",
    "US500":    "US500",
}

MIN_SIZE = {
    "BITCOIN":    0.01,
    "ETHEREUM":   0.1,
    "NVDA":       1.0,
    "NDAQ":       1.0,
    "SILVER":     1.0,
    "GBPUSD":     1000.0,
    "GOLD":       0.1,
    "OIL_CRUDE":  1.0,
    "EURUSD":     1000.0,
    "US500":      0.1,
}

# Sizing dinamico: score mas alto = posicion mas grande
# Score 2 = minimo, Score 6 = 3x el minimo
SCORE_SIZE = {2: 1.0, 3: 1.5, 4: 2.0, 5: 2.5, 6: 3.0}

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
        self.ensure_session()
        url  = f"{BASE_URL}/api/v1/accounts"
        resp = requests.get(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json().get("accounts", [])

    def get_activity_history(self, days=30):
        """Capital.com max range = 1 day; loop dia por dia."""
        import calendar
        self.ensure_session()
        all_activities = []
        now = datetime.utcnow()
        for i in range(days):
            day_end   = now - timedelta(days=i)
            day_start = day_end - timedelta(days=1)
            end_ms    = int(calendar.timegm(day_end.timetuple()) * 1000)
            start_ms  = int(calendar.timegm(day_start.timetuple()) * 1000)
            params = {"from": start_ms, "to": end_ms, "pageSize": 500}
            url = f"{BASE_URL}/api/v1/history/activity"
            try:
                resp = requests.get(url, headers=self._headers(), params=params, timeout=15)
                resp.raise_for_status()
                all_activities.extend(resp.json().get("activities", []))
            except Exception as e:
                logger.warning(f"[client] activity day -{i}: {e}")
                break
        return all_activities

    def open_position(self, symbol, action, entry, sl, tp1, score=2):
        epic = SYMBOL_MAP.get(symbol)
        if not epic:
            logger.warning(f"[client] Simbolo desconocido: {symbol}")
            return None
        positions = self.get_positions()
        for p in positions:
            if p.get("market", {}).get("epic") == epic:
                logger.info(f"[client] {symbol}: ya tiene posicion abierta, omitiendo")
                return None
        direction  = "BUY" if action == "LONG" else "SELL"
        base_size  = MIN_SIZE.get(epic, 1.0)
        multiplier = SCORE_SIZE.get(min(abs(score), 6), 1.0)
        size       = round(base_size * multiplier, 4)
        self.ensure_session()
        url  = f"{BASE_URL}/api/v1/positions"
        body = {
            "epic":           epic,
            "direction":      direction,
            "size":           size,
            "guaranteedStop": False,
            "stopLevel":      round(sl, 5),
            "profitLevel":    round(tp1, 5),
        }
        resp = requests.post(url, json=body, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        data = resp.json()
        logger.info(f"[client] {symbol}: {direction} size={size} (score={score}, x{multiplier}) sl={sl} tp={tp1} - {data}")
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
