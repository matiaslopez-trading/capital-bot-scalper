"""
capital_client.py — Bot Scalper v7
Cliente para la API REST de Capital.com (modo demo, dinero ficticio).

Cambios v7:
- Se elimina el bloqueo automático de "posición ya abierta" en open_position():
  ahora el bot puede tener hasta 2 posiciones simultáneas por símbolo
  (el control de cuántas hay abiertas lo lleva main.py con own_positions).
"""

import os
import time
import logging
import requests
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

BASE_URL    = "https://demo-api-capital.backend-capital.com"
SESSION_TTL = 540

# Activos del Scalper — universo ampliado v7.10 (20 activos, separado del Bot
# Swing). Se suman 9 criptomonedas nuevas (epics y minDealSize verificados
# en vivo contra la API de Capital.com vía /debug-markets antes de sumarlas)
# para (a) generar mas señales de calidad por dia y (b) cubrir las horas
# "muertas" fuera del horario de NYSE, ya que crypto opera 24/7. Se evito
# a proposito pisar el universo del Bot Swing (BTCUSD, ETHUSD, etc).
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
    # v7.10 — nuevos, todos crypto 24/7
    "ADAUSD":  "ADAUSD",
    "LTCUSD":  "LTCUSD",
    "LINKUSD": "LINKUSD",
    "DOTUSD":  "DOTUSD",
    "AVAXUSD": "AVAXUSD",
    "MATICUSD": "MATICUSD",
    "ATOMUSD": "ATOMUSD",
    "XLMUSD":  "XLMUSD",
    "BNBUSD":  "BNBUSD",
}

# % del balance a arriesgar por operación (score fijo 2 en v7: muchas
# operaciones chicas, no pocas operaciones grandes)
PCT_POR_SCORE = {2: 0.015, 3: 0.03, 4: 0.05, 5: 0.07, 6: 0.10}

# v7.3d: exposicion (notional) maxima permitida por operacion, como % del
# balance. Protege cuentas chicas: algunos activos (ej. US100, GBPJPY)
# tienen un tamaño minimo de Capital.com que representa una exposicion
# desproporcionada si el balance es bajo (con $1000, el minimo de US100
# ya son ~$2800 de exposicion). Si el minimo obligatorio de la plataforma
# supera este %, la operacion se aborta en vez de forzarla.
MAX_EXPOSURE_PCT = 0.10

# Tamaño mínimo Capital.com (minDealSize real, verificado via /debug-market-full).
# v7.12 FIX: los 9 valores originales (US100...MSFT) estaban mal cargados
# de antes de esta sesion - eran hasta 100x mas grandes que el minimo real
# de la plataforma (ej. XRPUSD tenia 100 hardcodeado, el real es 1; DOGEUSD
# tenia 1000, el real es 10; US100 tenia 0.1, el real es 0.001). Esto
# forzaba exposicion innecesariamente grande en cada operacion de esos
# activos y contribuia a que el guardrail de exposicion los abortara mas
# seguido de lo necesario. Los 9 activos agregados en v7.10 (ADAUSD en
# adelante) ya estaban verificados correctamente.
MIN_SIZE = {
    "US100":   0.001,
    "GBPJPY":  100.0,
    "DOGEUSD": 10.0,
    "XRPUSD":  1.0,
    "SOLUSD":  0.1,
    "AMZN":    0.1,
    "TSLA":    0.1,
    "AAPL":    0.1,
    "MSFT":    0.01,
    # v7.10
    "ADAUSD":   10.0,
    "LTCUSD":   0.1,
    "LINKUSD":  1.0,
    "DOTUSD":   1.0,
    "AVAXUSD":  0.1,
    "MATICUSD": 10.0,
    "ATOMUSD":  1.0,
    "XLMUSD":   10.0,
    "BNBUSD":   0.01,
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
            timeout=15,
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
        """Retorna el balance disponible."""
        try:
            accounts = self.get_accounts()
            for acc in accounts:
                balance = acc.get("balance", {}).get("available", 0)
                if balance > 0:
                    return float(balance)
        except Exception as e:
            logger.warning(f"[client] No se pudo obtener balance: {e}")
        return 1000.0  # fallback conservador

    def get_positions(self):
        self.ensure_session()
        url  = f"{BASE_URL}/api/v1/positions"
        resp = requests.get(url, headers=self._headers(), timeout=15)
        resp.raise_for_status()
        return resp.json().get("positions", [])

    def get_transactions(self, from_iso, to_iso):
        """
        v7.9 (debug/PnL): GET /history/transactions - historial de
        transacciones (trades cerrados, depositos, etc.) con su monto.
        from_iso/to_iso formato "YYYY-MM-DDTHH:MM:SS".
        """
        self.ensure_session()
        url = f"{BASE_URL}/api/v1/history/transactions?from={from_iso}&to={to_iso}"
        resp = requests.get(url, headers=self._headers(), timeout=20)
        resp.raise_for_status()
        return resp.json().get("transactions", [])

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
                if resp.status_code == 400:
                    logger.warning("[client] activity: endpoint no disponible (400) — omitiendo")
                    break
                resp.raise_for_status()
                all_activities.extend(resp.json().get("activities", []))
            except Exception as e:
                logger.warning(f"[client] activity day -{day}: {e}")
                break
        return all_activities

    def open_position(self, symbol, action, entry, sl, tp1, score=2, sizing_mult=1.0):
        """
        v7: ya NO bloquea automáticamente si hay una posición abierta en el
        mismo símbolo — el límite de 2 posiciones simultáneas lo controla
        main.py antes de llamar a esta función.
        """
        epic = SYMBOL_MAP.get(symbol)
        if not epic:
            logger.warning(f"[client] Simbolo desconocido: {symbol}")
            return None

        direction = "BUY" if action == "LONG" else "SELL"

        # Sizing: arriesgar risk_usd exactos sobre la distancia al SL
        pct      = PCT_POR_SCORE.get(min(abs(score), 6), 0.015) * sizing_mult
        balance  = self.get_balance()
        risk_usd = balance * pct
        sl_dist  = abs(entry - sl) if sl and sl != entry else 0
        if sl_dist > 0:
            size = round(risk_usd / sl_dist, 4)
        else:
            size = round(risk_usd / entry, 4) if entry > 0 else MIN_SIZE.get(epic, 1.0)
        # Cap: no más del 10% del balance en margen por posición individual.
        # v7.10.1 fix: antes era 15%, pero eso chocaba directo contra el
        # guardrail de exposicion maxima (MAX_EXPOSURE_PCT=10%, ver abajo) -
        # cada vez que este cap era el que mandaba (SL ajustado, tipico en
        # scalping), la exposicion resultante quedaba fija en 15% del
        # balance, que SIEMPRE supera el limite del 10% del guardrail y
        # aborta la operacion. Confirmado en vivo (TSLA, 21/07/2026): un
        # short valido con ATR ajustado se calculaba en 8.06 acciones
        # ($3018 de exposicion, 15% de $20.121) y el guardrail lo rechazaba
        # porque el limite es $2012 (10%). Bajar este cap a 10% hace que
        # ambos limites sean consistentes: el guardrail solo actua para su
        # proposito original (tamaño minimo de la plataforma
        # desproporcionado), no como un choque estructural contra el
        # sizing normal.
        if entry > 0:
            size = min(size, round((balance * MAX_EXPOSURE_PCT) / entry, 4))
        size = max(size, MIN_SIZE.get(epic, 1.0))

        # v7.3d: guardrail de exposicion maxima. El paso anterior puede
        # haber forzado size hacia arriba (MIN_SIZE de la plataforma) por
        # encima del cap de 15% recien aplicado. Si la exposicion final
        # (size x precio) supera MAX_EXPOSURE_PCT del balance, se aborta
        # en vez de abrir una posicion desproporcionada para la cuenta.
        exposure = size * entry if entry > 0 else 0
        max_exposure = balance * MAX_EXPOSURE_PCT
        if exposure > max_exposure:
            logger.warning(
                f"[client] {symbol}: operacion abortada - tamaño mínimo de la "
                f"plataforma ({size}) implica exposición ${exposure:.2f}, "
                f"por encima del {MAX_EXPOSURE_PCT*100:.0f}% del balance "
                f"(${max_exposure:.2f} con balance=${balance:.2f}). "
                f"Activo no operable con este capital."
            )
            return None

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

        data     = resp.json()
        deal_ref = data.get("dealReference")

        # El POST /positions sólo devuelve dealReference, no el dealId real.
        # Hay que confirmarlo — si no, close_position()/update_sl() fallan con 400.
        deal_id = None
        if deal_ref:
            try:
                conf_url  = f"{BASE_URL}/api/v1/confirms/{deal_ref}"
                conf_resp = requests.get(conf_url, headers=self._headers(), timeout=10)
                conf_resp.raise_for_status()
                conf = conf_resp.json()
                deal_id = conf.get("dealId") or conf.get("affectedDeals", [{}])[0].get("dealId")
            except Exception as e:
                logger.warning(f"[client] {symbol}: no se pudo confirmar dealId — {e}")

        data["dealId"] = deal_id
        logger.info(
            f"[client] {symbol}: {direction} size={size} "
            f"({pct*100:.1f}% balance={balance:.0f}) sl={sl} tp={tp1} "
            f"dealId={deal_id} — {data}"
        )
        return data

    def update_sl(self, deal_id, new_sl):
        """Mueve el Stop Loss de una posición abierta (trailing stop)."""
        self.ensure_session()
        url  = f"{BASE_URL}/api/v1/positions/{deal_id}"
        body = {"stopLevel": round(new_sl, 5)}
        try:
            resp = requests.put(url, json=body, headers=self._headers(), timeout=15)
            resp.raise_for_status()
            logger.info(f"[client] Trailing SL actualizado deal={deal_id} new_sl={new_sl}")
            return resp.json()
        except requests.HTTPError:
            if resp.status_code == 400:
                try:
                    detail = resp.json()
                except Exception:
                    detail = resp.text
                logger.warning(f"[client] update_sl rechazado (400): {detail}")
                return None
            raise

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
