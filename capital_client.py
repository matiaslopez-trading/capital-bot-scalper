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
import math
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

# DEPRECADO desde v7.13 - el sizing por % de riesgo fue reemplazado por el
# sizing basado en stop garantizado (ver GUARANTEED_STOP_PCT mas abajo).
# Se deja definido por si algo externo todavia lo importa, pero
# open_position() ya no lo usa.
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

# v7.13: distancia minima de stop GARANTIZADO por activo, como % del precio
# de entrada. Verificada en vivo contra la API real (varia muchisimo entre
# activos: 0.1% en US100/GBPJPY hasta 75% en ATOMUSD/BNBUSD). Determina el
# tope de perdida REAL y GARANTIZADO por operacion - a diferencia del SL
# nativo comun (que puede sufrir slippage en movimientos rapidos), un stop
# garantizado de Capital.com ejecuta EXACTO al precio pactado sin importar
# gaps. Reemplaza el SL basado en ATR (que, combinado con el guardrail de
# exposicion, produjo perdidas de hasta -$12 en vez de los $2 pactados
# el 21/07/2026 - ver MEMORIA_PROYECTO.md).
GUARANTEED_STOP_PCT = {
    "US100":    0.1,
    "GBPJPY":   0.1,
    "DOGEUSD":  2.0,
    "XRPUSD":   0.5,
    "SOLUSD":   2.0,
    "AMZN":     7.5,
    "TSLA":     20.0,
    "AAPL":     7.5,
    "MSFT":     7.5,
    "ADAUSD":   2.0,
    "LTCUSD":   0.5,
    "LINKUSD":  2.0,
    "DOTUSD":   2.0,
    "AVAXUSD":  2.0,
    "MATICUSD": 2.0,
    "ATOMUSD":  75.0,
    "XLMUSD":   2.0,
    "BNBUSD":   75.0,
}

# v7.14 FIX CRITICO: Capital.com exige que el tamaño de la posición sea un
# MULTIPLO EXACTO de este incremento por activo (dealingRules.minSizeIncrement).
# El sizing de v7.13 calculaba un tamaño "objetivo" (FIXED_SL_USD / distancia
# del stop garantizado) que casi nunca cae justo en un múltiplo válido — para
# 13 de los 18 activos (ADAUSD, AMZN, ATOMUSD, AVAXUSD, DOGEUSD, DOTUSD,
# LINKUSD, LTCUSD, MSFT, SOLUSD, US100, XLMUSD, XRPUSD) el tamaño calculado
# no era múltiplo del incremento exigido, lo que la API de Capital.com
# rechazaba silenciosamente (400) — la señal parecía valida en el dashboard
# pero la operación nunca se abría. Verificado en vivo el 21/07/2026 con
# ATOMUSD (RSI en sobreventa, señal LONG, pero ninguna posición se creaba).
# Ahora open_position() redondea el tamaño HACIA ARRIBA al múltiplo válido
# más cercano antes de enviar la orden.
MIN_SIZE_INCREMENT = {
    "US100":    0.001,
    "GBPJPY":   100.0,
    "DOGEUSD":  1.0,
    "XRPUSD":   1.0,
    "SOLUSD":   0.1,
    "AMZN":     0.1,
    "TSLA":     0.1,
    "AAPL":     0.01,
    "MSFT":     0.01,
    "ADAUSD":   1.0,
    "LTCUSD":   0.1,
    "LINKUSD":  1.0,
    "DOTUSD":   1.0,
    "AVAXUSD":  0.1,
    "MATICUSD": 0.1,
    "ATOMUSD":  1.0,
    "XLMUSD":   1.0,
    "BNBUSD":   0.01,
}

# v7.13: capital de referencia FIJO para el sizing del Scalper - a proposito
# NO es el balance real de la cuenta demo (~$20.000). Matias pidio que todo
# el sizing se calcule como si el capital fuera el que va a usar cuando pase
# a plata real (~$1000), para que el riesgo por operacion sea realista desde
# ya y no dependa de que la demo tenga de casualidad un balance mucho mayor.
EFFECTIVE_BALANCE = 1000.0

# v7.3/v7.13: techo de PnL en dolares objetivo por operacion del Scalper.
# Con el stop garantizado, el tope REAL de perdida varia por activo (ver
# GUARANTEED_STOP_PCT + MIN_SIZE + MAX_EXPOSURE_PCT) - este valor es el
# objetivo que persigue el sizing, no una promesa exacta para todos los
# activos (algunos, como TSLA o BNBUSD, no pueden bajar de ~$4-8 por como
# esta armado el minimo de la plataforma - queda documentado, no oculto).
FIXED_TP_USD = 2.0
FIXED_SL_USD = 2.0


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

        v7.13: reescrito de raíz. Antes el tamaño se calculaba por % de
        riesgo sobre el balance real (~$20.000 en la demo) y el SL/TP nativo
        usaba la distancia del ATR — una combinación que, con posiciones
        grandes, dejaba que el SL/TP nativo (no garantizado) sufriera
        slippage: el 21/07/2026 esto produjo pérdidas de hasta -$12 en vez
        de los $2 pactados. Ahora:
          1. El tamaño se calcula con un capital de referencia FIJO
             (EFFECTIVE_BALANCE=$1000, no el balance real de la demo).
          2. El SL es un STOP GARANTIZADO de Capital.com — ejecuta EXACTO
             al precio pactado, sin importar gaps o velocidad del mercado.
          3. La distancia de ese stop está fijada por la plataforma (varía
             por activo, GUARANTEED_STOP_PCT) — el tamaño se elige para que
             esa distancia equivalga a FIXED_SL_USD en dólares. Si el
             tamaño mínimo de la plataforma o el guardrail de exposición
             no lo permiten, el tope real puede quedar por encima de
             FIXED_SL_USD (ej. TSLA ronda ~$7-8) — pero SIEMPRE conocido
             de antemano, nunca una sorpresa como antes.
          4. El TP usa la MISMA distancia que el SL (R:R 1:1 simétrico,
             "gana X pierde X" como pidió Matias) como orden límite normal
             (no necesita ser garantizada, no hay riesgo de slippage
             ejecutando a favor).
        """
        epic = SYMBOL_MAP.get(symbol)
        if not epic:
            logger.warning(f"[client] Simbolo desconocido: {symbol}")
            return None
        if entry <= 0:
            logger.warning(f"[client] {symbol}: entry invalido ({entry})")
            return None

        direction = "BUY" if action == "LONG" else "SELL"
        min_size  = MIN_SIZE.get(epic, 1.0)
        gstop_pct = GUARANTEED_STOP_PCT.get(epic, 5.0)  # 5% fallback conservador

        # Distancia de precio que exige el stop garantizado para este activo.
        guar_dist_price = entry * (gstop_pct / 100.0)
        if guar_dist_price <= 0:
            logger.warning(f"[client] {symbol}: distancia de stop garantizado invalida")
            return None

        # Tamaño objetivo: el que hace que esa distancia equivalga a
        # FIXED_SL_USD en dolares.
        target_size = FIXED_SL_USD / guar_dist_price

        # Piso: no menos del minimo de la plataforma.
        size = max(target_size, min_size)

        # Techo: no mas del guardrail de exposicion, sobre el capital de
        # referencia fijo (no el balance real de la demo).
        max_exposure = EFFECTIVE_BALANCE * MAX_EXPOSURE_PCT
        size_cap     = max_exposure / entry
        size         = min(size, size_cap)

        if size < min_size:
            # El guardrail de exposicion (10% de $1000) no alcanza ni para
            # el tamaño minimo que exige la plataforma en este activo -
            # no es operable con este capital de referencia. IMPORTANTE:
            # este chequeo va ANTES de redondear al incremento — si se
            # hiciera despues, el redondeo hacia arriba forzaria el tamaño
            # de vuelta al minimo (ej. GBPJPY: min=100, cap real da 0.46,
            # pero redondear 0.46 al incremento de 100 da... 100 de nuevo,
            # anulando por completo el guardrail de exposicion).
            logger.warning(
                f"[client] {symbol}: operacion abortada - el minimo de la "
                f"plataforma ({min_size}) implica exposicion "
                f"${min_size*entry:.2f}, por encima del {MAX_EXPOSURE_PCT*100:.0f}% "
                f"del capital de referencia (${max_exposure:.2f} sobre "
                f"${EFFECTIVE_BALANCE:.0f}). Activo no operable con este capital."
            )
            return None

        # v7.14 FIX: redondear HACIA ARRIBA al multiplo valido de
        # minSizeIncrement (Capital.com rechaza tamaños que no sean un
        # multiplo exacto — esto era lo que impedia ejecutar 13 de los 18
        # activos, incl. ATOMUSD, aun con señal y capital validos). Se aplica
        # DESPUES del chequeo de abort de arriba, asi nunca revive una
        # operacion que el guardrail de exposicion ya descarto.
        increment = MIN_SIZE_INCREMENT.get(epic, min_size)
        if increment > 0:
            size = math.ceil(round(size / increment, 6)) * increment
        size = round(size, 6)

        # Perdida/ganancia REAL que va a resultar si se toca el stop/TP,
        # dado el tamaño final (puede ser distinto de FIXED_SL_USD si el
        # minimo de la plataforma o el guardrail de exposicion mandaron).
        real_usd_at_stop = round(size * guar_dist_price, 2)

        if direction == "BUY":
            sl_final = round(entry - guar_dist_price, 5)
            tp_final = round(entry + guar_dist_price, 5)
        else:
            sl_final = round(entry + guar_dist_price, 5)
            tp_final = round(entry - guar_dist_price, 5)

        logger.info(
            f"[client] {symbol}: sizing v7.13 - size={size} "
            f"(target={target_size:.4f}, min={min_size}, cap={size_cap:.4f}) | "
            f"stop garantizado @ {gstop_pct}% = {guar_dist_price:.6f} de distancia | "
            f"real ${real_usd_at_stop} por lado (objetivo ${FIXED_SL_USD})"
        )

        self.ensure_session()
        url  = f"{BASE_URL}/api/v1/positions"
        body = {
            "epic":           epic,
            "direction":      direction,
            "size":           size,
            "guaranteedStop": True,
            "stopLevel":      sl_final,
            "profitLevel":    tp_final,
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
            f"sl={sl_final} tp={tp_final} real_usd=${real_usd_at_stop} "
            f"(guaranteedStop=True) dealId={deal_id} — {data}"
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
