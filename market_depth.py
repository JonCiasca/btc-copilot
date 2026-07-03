"""
market_depth.py
----------------
Módulo de PROFUNDIDAD DE MERCADO (order book) — spot y futuros.

Separado de main.py a propósito: es el primer paso de modularización
del proyecto (Head Hub venía siendo un único archivo). Esta es la
"nueva raíz" para todo lo relacionado a book/depth; a futuro puede
sumar trade-tape (WebSocket) sin tocar main.py, solo importando
funciones nuevas de acá.

MÉTODO ACTUAL (Fase 1 — REST snapshot, no WebSocket):
Cada refresh del dashboard (~15s) se pide UNA foto del order book
(bids/asks vigentes en ese instante) vía el proxy de Render — Binance
y Binance Futures bloquean la IP de Streamlit Cloud, mismo motivo por
el que ya existe el proxy para klines/ticker/OI.

Esto NO es un book en vivo tipo Bookmap (eso requiere WebSocket
persistente, con reconexión y buffer de eventos de profundidad
incremental — diffDepth). Lo que se arma acá es una SECUENCIA de fotos
guardadas en session_state, que ya permite ver:
  - cómo se mueve el desequilibrio bid/ask en el tiempo
  - dónde se concentran órdenes grandes (paredes) cerca del precio
  - un heatmap aproximado de profundidad por precio x tiempo

LÍMITE HONESTO: entre una foto y la siguiente (15s) pueden aparecer y
desaparecer órdenes grandes sin que se registren (spoofing típico de
mercados de futuros). Esto es un proxy razonable para lectura de
contexto, no un reemplazo de un feed de depth incremental real.
Migración a WebSocket: requiere agregar un relay WS en el proxy de
Render (ver instrucciones aparte) — cuando esté, este módulo cambia
SOLO la función obtener_profundidad, el resto (métricas, heatmap) no
se toca.
"""

import requests
import numpy as np
import plotly.graph_objects as go
from datetime import datetime, timezone


# ----------------------------------
# FETCH (REST, vía proxy)
# ----------------------------------

def obtener_profundidad(proxy_url, mercado="spot", symbol="BTCUSDT", limite=100):
    """
    Pide un snapshot de order book vía el proxy de Render.

    mercado: "spot" -> endpoint /depth (Binance spot)
             "futures" -> endpoint /futures/depth (Binance Futures)

    Devuelve (snapshot_dict, None) si funciona, o (None, error_str) si
    falla — mismo patrón de retorno que el resto de obtener_* en main.py.

    Espera del proxy una respuesta JSON con "bids" y "asks", cada uno
    lista de [precio_str, cantidad_str] (formato nativo de Binance
    /api/v3/depth y /fapi/v1/depth). Ver instrucciones de proxy aparte
    para el endpoint exacto a agregar en Render.
    """

    endpoint = "/depth" if mercado == "spot" else "/futures/depth"
    url = f"{proxy_url}{endpoint}?symbol={symbol}&limit={limite}"

    try:
        respuesta = requests.get(url, timeout=10)
        data = respuesta.json()

        if not isinstance(data, dict) or "bids" not in data or "asks" not in data:
            msg = data.get("error", str(data)) if isinstance(data, dict) else "Respuesta inesperada del proxy"
            return None, msg

        bids = [(float(p), float(q)) for p, q in data["bids"]]
        asks = [(float(p), float(q)) for p, q in data["asks"]]

        if not bids or not asks:
            return None, "Order book vacío en la respuesta del proxy"

        return {
            "bids": sorted(bids, key=lambda x: -x[0]),  # de mayor a menor precio
            "asks": sorted(asks, key=lambda x: x[0]),   # de menor a mayor precio
            "ts": datetime.now(timezone.utc),
        }, None

    except Exception as e:
        return None, str(e)


# ----------------------------------
# MÉTRICAS DE DESEQUILIBRIO (imbalance)
# ----------------------------------

def calcular_metricas_profundidad(snapshot, rango_pct=0.5):
    """
    A partir de UN snapshot (bids/asks), calcula:
      - mid price, mejor bid, mejor ask, spread
      - volumen acumulado de bids y asks dentro de +-rango_pct del mid
      - imbalance_pct: (vol_bid - vol_ask) / (vol_bid + vol_ask) * 100
        (positivo = más compradores cerca del precio, negativo = más
        vendedores cerca del precio — lectura de PRESIÓN LATENTE, no de
        trades ya ejecutados, complementa a buy_pressure/sell_pressure
        que ya calculás sobre velas ejecutadas en main.py)

    Devuelve None si el snapshot no tiene datos suficientes.
    """

    if not snapshot:
        return None

    bids = snapshot["bids"]
    asks = snapshot["asks"]

    if not bids or not asks:
        return None

    mejor_bid = bids[0][0]
    mejor_ask = asks[0][0]
    mid = (mejor_bid + mejor_ask) / 2

    limite_inf = mid * (1 - rango_pct / 100)
    limite_sup = mid * (1 + rango_pct / 100)

    vol_bid = sum(q for p, q in bids if p >= limite_inf)
    vol_ask = sum(q for p, q in asks if p <= limite_sup)
    total = vol_bid + vol_ask

    imbalance_pct = ((vol_bid - vol_ask) / total * 100) if total > 0 else 0.0

    spread = mejor_ask - mejor_bid
    spread_pct = (spread / mid * 100) if mid > 0 else 0.0

    return {
        "mid": mid,
        "mejor_bid": mejor_bid,
        "mejor_ask": mejor_ask,
        "spread": spread,
        "spread_pct": spread_pct,
        "vol_bid": vol_bid,
        "vol_ask": vol_ask,
        "imbalance_pct": round(imbalance_pct, 1),
    }


def lectura_imbalance(metricas):
    """
    Texto corto de interpretación del imbalance — mismo criterio de
    honestidad que el resto del dashboard: describe presión LATENTE
    (órdenes puestas, no ejecutadas), no una predicción de dirección.
    """

    if metricas is None:
        return "Sin datos suficientes de book."

    imb = metricas["imbalance_pct"]

    if imb > 25:
        return f"🟢 Presión compradora latente fuerte ({imb:+.1f}%) — más volumen apilado del lado bid cerca del precio."
    elif imb > 8:
        return f"🟢 Presión compradora latente moderada ({imb:+.1f}%)."
    elif imb < -25:
        return f"🔴 Presión vendedora latente fuerte ({imb:+.1f}%) — más volumen apilado del lado ask cerca del precio."
    elif imb < -8:
        return f"🔴 Presión vendedora latente moderada ({imb:+.1f}%)."
    else:
        return f"🟡 Book equilibrado ({imb:+.1f}%) — sin sesgo claro de órdenes en descanso."


# ----------------------------------
# HISTORIAL (para el heatmap precio x tiempo)
# ----------------------------------

def agregar_snapshot_historial(historial, snapshot, niveles_guardados=40, tope=40):
    """
    Guarda una versión recortada del snapshot en una lista de
    session_state (uno por refresh). niveles_guardados limita cuántos
    niveles de cada lado se guardan (no hace falta el book completo de
    100+ niveles para el heatmap visible). tope limita el largo del
    historial (ventana de ~tope refreshes ≈ tope*15s hacia atrás).
    """

    if snapshot is None:
        return

    historial.append({
        "ts": snapshot["ts"],
        "bids": snapshot["bids"][:niveles_guardados],
        "asks": snapshot["asks"][:niveles_guardados],
    })

    if len(historial) > tope:
        historial.pop(0)


# ----------------------------------
# HEATMAP DE PROFUNDIDAD (precio x tiempo)
# ----------------------------------

def construir_heatmap_profundidad(historial, precio_actual, ancho_bucket_usd=15, rango_pct=1.0):
    """
    Arma una matriz [bucket_de_precio x snapshot_en_el_tiempo] con el
    tamaño acumulado en cada celda. Bids con signo POSITIVO, asks con
    signo NEGATIVO — así un colorscale divergente (verde/rojo) separa
    ambos lados sin necesitar dos heatmaps superpuestos.

    Devuelve (buckets, tiempos, matriz) o (None, None, None) si no hay
    historial todavía.
    """

    if not historial:
        return None, None, None

    precio_min = precio_actual * (1 - rango_pct / 100)
    precio_max = precio_actual * (1 + rango_pct / 100)
    n_buckets = max(int((precio_max - precio_min) / ancho_bucket_usd), 10)
    buckets = np.linspace(precio_min, precio_max, n_buckets)

    matriz = np.zeros((n_buckets, len(historial)))
    tiempos = [snap["ts"] for snap in historial]

    for j, snap in enumerate(historial):
        for precio, cantidad in snap["bids"]:
            if precio_min <= precio <= precio_max:
                idx = min(int(np.searchsorted(buckets, precio)), n_buckets - 1)
                matriz[idx, j] += cantidad
        for precio, cantidad in snap["asks"]:
            if precio_min <= precio <= precio_max:
                idx = min(int(np.searchsorted(buckets, precio)), n_buckets - 1)
                matriz[idx, j] -= cantidad

    return buckets, tiempos, matriz


def figura_heatmap_profundidad(buckets, tiempos, matriz, precio_actual, titulo):
    """Construye la figura Plotly del heatmap de profundidad."""

    fig = go.Figure(
        data=go.Heatmap(
            z=matriz,
            x=list(range(len(tiempos))),
            y=buckets,
            colorscale=[[0.0, "#ef4444"], [0.5, "#12151c"], [1.0, "#22c55e"]],
            zmid=0,
            colorbar=dict(title="ask ⟵ ⟶ bid"),
            hovertemplate="precio: $%{y:,.0f}<br>tamaño neto: %{z:.2f}<extra></extra>",
        )
    )

    fig.add_hline(
        y=precio_actual, line_dash="dot", line_color="#ff8c00",
        annotation_text=f"${precio_actual:,.0f}", annotation_position="right",
    )

    fig.update_layout(
        title=titulo,
        template="plotly_dark",
        paper_bgcolor="#0e1117",
        plot_bgcolor="#0e1117",
        height=420,
        xaxis_title="snapshots recientes (cada ~15s) →",
        yaxis_title="precio (USD)",
        margin=dict(l=10, r=10, t=40, b=30),
    )

    return fig