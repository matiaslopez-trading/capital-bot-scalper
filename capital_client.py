"""
capital_client.py
Cliente para la API REST de Capital.com (modo demo) — Bot Scalper v3.
"""

import os
import time
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

BASE_URL    = "https://demo-api-capital.backend-capital.com"
SESSION_TTL = 540

# Activos v3: alta volatilidad, distintos al Bot Swing
SYMBOL_MAP = {
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

PCT_POR_SCORE = {2: 0.02, 3: 0.03, 4: 0.05, 5: 0.07, 6: 0.10}

MIN_SIZE = {
    "US100":   0.1,
    "GBPJPY":  1000.0,
    "DOGEUSD": 1000.0,
    "XRPUSD":  100.0,
    "SOLUSD":  1.0,
    "AMZN":    1.0,
    "TSLA":    1.0,
    "AAPL":    1.0,
    "MSFT":    1.0,
}


class CapitalClient:
    def __init__(self):
        self.api_key    = os.environ["CAPITAL_API_KEY"]
        self.password   = os.environ["CAPITAL_PASSWORD"]
        self.identifier = os.environ["CAPITAL_IDENTIFIER"]
        self.cst        = None
        self.x_token    = None
        self.session_ts = 0

    def _headers(self):
        return {
            "X-SECURITY-TOKEN": self.x_token or "",
            "CST":              self.cst or "",
            "Content-Type":     "application/json",
        }

    def ensure_session(self):
        if time.time() - self.session_ts > SESSION_TTL:
            self.login()

    def login(self):
        url  = f"{BASE_URL}/api/v1/session"
        body = {
            "identifier":        self.identifier,
            "password":          self.password,
            "encryptedPassword": False,
        }
        resp = requests.post(
            url, json=body,
            headers={"X-CAP-API-KEY": self.api_key},
            timeout=15
        )
        resp.raise_for_status()
        self.cst        = resp.headers.get("CST")
        self.x_token    = resp.headers.get("X-SECURITY-TOKEN")
        self.session_ts = time.time()
        logger.info("[client] Login OK")

    def get_accounts(self):
        self.ensure_session()
        url  = f"{BASE_URL}/api/v1/accounts"
        resp = requests.get(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json().get("accounts", [])

    def get_balance(self):
        try:
            accounts = self.get_accounts()
            for acc in accounts:
                balance = acc.get("balance", {}).get("available", 0)
                if balance > 0:
                    return float(balance)
        except Exception as e:
            logger.warning(f"[client] No se pudo obtener balance: {e}")
        return 1000.0

    def get_positions(self):
        self.ensure_session()
        url  = f"{BASE_URL}/api/v1/positions"
        resp = requests.get(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json().get("positions", [])

    def get_activity_history(self, days=7):
        self.ensure_session()
        all_activities = []
        now = datetime.utcnow()
        for day in range(days):
            date_to   = now - timedelta(days=day)
            date_from = date_to - timedelta(days=1)
            from_ms   = int(date_from.timestamp() * 1000)
            to_ms     = int(date_to.timestamp() * 1000)
            url = (
                f"{BASE_URL}/api/v1/history/activity"
                f"?from={from_ms}&to={to_ms}&pageSize=500"
            )
            try:
                resp = requests.get(url, headers=self._headers(), timeout=15)
                resp.raise_for_status()
                data = resp.json().get("activities", [])
                all_activities.extend(data)
            except Exception as e:
                logger.warning(f"[client] activity day -{day}: {e}")
        return all_activities

    def open_position(self, symbol, action, entry, sl, tp1, score=2, sizing_mult=1.0):
        epic = SYMBOL_MAP.get(symbol)
        if not epic:
            logger.warning(f"[client] Simbolo desconocido: {symbol}")
            return None
        try:
            positions = self.get_positions()
            for p in positions:
                if p.get("market", {}).get("epic") == epic:
                    logger.info(f"[client] {symbol}: ya tiene posicion abierta, omitiendo")
                    return None
        except Exception as e:
            logger.warning(f"[client] {symbol}: no se pudo verificar posiciones: {e}")
        direction = "BUY" if action == "LONG" else "SELL"
        pct      = PCT_POR_SCORE.get(min(abs(score), 6), 0.02) * sizing_mult
        balance  = self.get_balance()
        risk_usd = balance * pct
        size     = round(risk_usd / entry, 4) if entry > 0 else MIN_SIZE.get(epic, 1.0)
        size     = max(size, MIN_SIZE.get(epic, 1.0))
        self.ensure_session()
        url  = f"{BASE_URL}/api/v1/positions"
        body = {
            "epic":           epic,
            "direction":      direction,
            "size":           size,
            "guaranteedStop": False,
            "stopLevel":      round(sl, 4),
            "profitLevel":    round(tp1, 4),
        }
        try:
            resp = requests.post(url, json=body, headers=self._headers(), timeout=15)
            resp.raise_for_status()
        except requests.HTTPError:
            if resp.status_code == 400:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                logger.warning(
                    f"[client] {symbol}: posicion rechazada (400) — "
                    f"posiblemente mercado cerrado: {detail}"
                )
                return None
            raise
        data = resp.json()
        logger.info(
            f"[client] {symbol}: {direction} size={size} "
            f"({pct*100:.1f}% balance={balance:.0f}) score={score} sl={sl} tp={tp1} - {data}"
        )
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
