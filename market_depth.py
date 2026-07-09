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

def _interpretar_error_binance(data):
    """
    Binance devuelve errores de rate-limit/ban como {"code": -1003, "msg": "..."},
    NO envueltos en {"error": ...} — a diferencia del resto del proxy. Sin este
    traductor, el usuario ve el dict crudo de Binance en pantalla. code -1003
    específicamente es BAN DE PESO (weight ban): la IP del proxy pidió demasiado
    en poco tiempo y Binance la bloqueó hasta un timestamp determinado — no es
    un error de símbolo/parámetros, es un corte temporal de acceso completo.
    """
    if not isinstance(data, dict):
        return "Respuesta inesperada del proxy"

    if "code" in data and "msg" in data:
        codigo = data["code"]
        if codigo == -1003:
            return (
                "IP del proxy temporalmente baneada por Binance (rate-limit de peso). "
                "Se recupera sola pasado un tiempo — no insistir con más pedidos mientras tanto."
            )
        return f"Binance code {codigo}: {data['msg']}"

    return data.get("error", str(data))


def obtener_profundidad(proxy_url, mercado="spot", symbol="BTCUSDT", limite=20):
    """
    Pide un snapshot de order book vía el proxy de Render.

    mercado: "spot" -> endpoint /depth (Binance spot)
             "futures" -> endpoint /futures/depth (Binance Futures)

    limite baja a 20 por default (antes 100): en Binance Futures el
    weight de /fapi/v1/depth salta de 2 a 5 al pasar de limit=50 a
    limit=100 — con el heatmap guardando como mucho 40 niveles por
    lado igual, pedir 100 era pagar weight extra sin uso real. Bajar
    esto es la mitigación más directa contra los bans -1003.

    Devuelve (snapshot_dict, None) si funciona, o (None, error_str) si
    falla — mismo patrón de retorno que el resto de obtener_* en main.py.
    """

    endpoint = "/depth" if mercado == "spot" else "/futures/depth"
    url = f"{proxy_url}{endpoint}?symbol={symbol}&limit={limite}"

    try:
        respuesta = requests.get(url, timeout=10)
        data = respuesta.json()

        if not isinstance(data, dict) or "bids" not in data or "asks" not in data:
            return None, _interpretar_error_binance(data)

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

def procesar_snapshot_cache(session_state, clave, snapshot_nuevo):
    """
    Cachea el último snapshot válido de una fuente (spot/futures) en
    session_state — mismo patrón que ya usa main.py para funding y OI
    (ultimo_funding_valido, ultimo_oi_valido). Evita que un ban -1003
    temporal borre las métricas de pantalla; en su lugar se muestra el
    último dato bueno conocido, marcado como caché.

    Devuelve (snapshot_a_mostrar, es_cache). snapshot_a_mostrar es None
    solo si NUNCA hubo un snapshot válido en esta sesión.
    """
    clave_cache = f"profundidad_ultimo_valido_{clave}"

    if snapshot_nuevo is not None:
        session_state[clave_cache] = snapshot_nuevo
        return snapshot_nuevo, False

    cache = session_state.get(clave_cache)
    return cache, cache is not None


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

def construir_heatmap_profundidad(historial, precio_actual, ancho_bucket_usd=20, rango_pct=2.0):
    """
    Arma una matriz [bucket_de_precio x snapshot_en_el_tiempo] con el
    tamaño acumulado en cada celda. Bids con signo POSITIVO, asks con
    signo NEGATIVO — así un colorscale divergente (verde/rojo) separa
    ambos lados sin necesitar dos heatmaps superpuestos.

    Devuelve (buckets, tiempos, matriz) o (None, None, None) si no hay
    historial todavía.

    Defaults recalibrados (pedido del usuario, referencia visual tipo
    Bookmap): antes rango_pct=1.0 daba solo ~$640 de ancho total a
    precios de BTC ~64,000 — muy angosto para ver estructura de book
    real. Ahora rango_pct=2.0 da ~$2,500 de ancho total (±$1,250 sobre
    el precio actual), y ancho_bucket_usd sube a 20 para compensar:
    sin esto, el rango más ancho multiplicaría por ~3 la cantidad de
    buckets, y por lo tanto la cantidad de barras 3D a renderizar
    (ver figura_barras_3d_profundidad más abajo).
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


def figura_heatmap_profundidad(buckets, tiempos, matriz, precio_actual, titulo, clave_camara="hm"):
    """
    Construye la figura Plotly del heatmap de profundidad.

    clave_camara: valor fijo para layout.uirevision — es lo que le dice
    a Plotly "aunque los datos cambien en el próximo refresh, esto es
    la MISMA vista para el usuario", así conserva el zoom/pan que haya
    hecho manualmente en vez de resetearlo cada 15s. Tiene que ser el
    mismo string en cada rerun (por eso es un parámetro fijo, no algo
    derivado de los datos).
    """

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
        uirevision=clave_camara,
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


def figura_superficie_profundidad(buckets, tiempos, matriz, precio_actual, titulo):
    """
    Versión "capas apiladas" de la misma matriz que usa el heatmap —
    go.Surface con contornos de proyección en el piso, que es la
    aproximación más honesta lograble con Plotly puro al estilo de
    terreno/capas del ejemplo de referencia (voxel-render real
    requeriría un motor 3D dedicado tipo Three.js, fuera de alcance
    de un gráfico Plotly embebido en Streamlit).

    Misma fuente de datos que figura_heatmap_profundidad (snapshots
    REST cada ~15s) — esto NO agrega resolución temporal nueva, solo
    cambia la lectura visual de la misma información.
    """

    fig = go.Figure(
        data=go.Surface(
            z=matriz,
            x=list(range(len(tiempos))),
            y=buckets,
            colorscale=[[0.0, "#ef4444"], [0.5, "#12151c"], [1.0, "#22c55e"]],
            cmid=0,
            contours={
                "z": {"show": True, "usecolormap": True, "project_z": True}
            },
            showscale=False,
        )
    )

    fig.update_layout(
        title=titulo,
        template="plotly_dark",
        paper_bgcolor="#0e1117",
        scene=dict(
            xaxis_title="snapshots →",
            yaxis_title="precio (USD)",
            zaxis_title="tamaño (ask ⟵ ⟶ bid)",
            bgcolor="#0e1117",
            camera=dict(eye=dict(x=1.6, y=-1.6, z=0.9)),
        ),
        height=480,
        margin=dict(l=0, r=0, t=40, b=0),
    )

    return fig


def figura_barras_3d_profundidad(
    buckets, tiempos, matriz, precio_actual, titulo,
    clave_camara="hm3d", ancho_barra=0.9,
):
    """
    Terreno de PROFUNDIDAD como barras extruidas (voxels de aristas
    duras), en vez del go.Surface suavizado de figura_superficie_
    profundidad — pedido explícito del usuario para acercarse a la
    estética tipo Bookmap (barras cuadradas por celda precio x tiempo,
    no una malla continua interpolada).

    CÓMO SE CONSTRUYE (vectorizado con NumPy, no un loop por celda):
    cada celda (bucket_de_precio, snapshot_de_tiempo) se convierte en
    un cubo (Mesh3d) que va desde z=0 hasta z=valor de la celda (bids
    positivos hacia arriba, asks negativos hacia abajo, mismo criterio
    de signo que el resto del módulo). Se arman TODOS los vértices y
    TODAS las caras de una sola vez con arrays de NumPy — con hasta
    ~100 buckets x 40 snapshots (400 celdas) esto da unos miles de
    triángulos, manejable para Plotly/WebGL en el navegador.

    Solo se dibujan celdas con volumen != 0 (no hace falta un cubo de
    altura cero) — esto además dejar ver "huecos" reales en el book,
    que es información honesta (ausencia de órdenes en ese nivel/
    momento), no un artefacto visual.

    intensity: mismo valor de la celda repetido en sus 8 vértices, con
    el mismo colorscale divergente rojo/verde del resto del módulo —
    así el color de cada barra es uniforme (no interpolado con las
    barras vecinas, a diferencia del Surface).

    clave_camara / ancho_barra: ver figura_heatmap_profundidad para el
    propósito de uirevision (conservar el zoom/pan manual entre
    refreshes). ancho_barra < 1.0 deja un pequeño espacio entre barras
    en el eje de tiempo, como en la referencia visual.

    Devuelve una go.Figure, o None si la matriz no tiene ninguna celda
    con datos (nada que dibujar).
    """

    if matriz is None or matriz.size == 0:
        return None

    n_y, n_x = matriz.shape  # n_y = buckets de precio, n_x = snapshots

    idx_y, idx_x = np.nonzero(matriz)

    if len(idx_y) == 0:
        return None

    valores = matriz[idx_y, idx_x]
    n_boxes = len(valores)

    # --- límites de cada caja en X (tiempo) e Y (precio) ---
    ancho_bucket = buckets[1] - buckets[0] if len(buckets) > 1 else 1.0

    x0 = idx_x - ancho_barra / 2
    x1 = idx_x + ancho_barra / 2
    y_centro = buckets[idx_y]
    y0 = y_centro - ancho_bucket * ancho_barra / 2
    y1 = y_centro + ancho_bucket * ancho_barra / 2
    z0 = np.minimum(0.0, valores)
    z1 = np.maximum(0.0, valores)

    # --- 8 vértices por caja, vectorizado (shape: n_boxes x 8) ---
    vx = np.stack([x0, x1, x1, x0, x0, x1, x1, x0], axis=1)
    vy = np.stack([y0, y0, y1, y1, y0, y0, y1, y1], axis=1)
    vz = np.stack([z0, z0, z0, z0, z1, z1, z1, z1], axis=1)

    x_flat = vx.reshape(-1)
    y_flat = vy.reshape(-1)
    z_flat = vz.reshape(-1)

    # --- 12 triángulos por caja (2 por cara x 6 caras), offset por caja ---
    base_i = np.array([0, 0, 4, 4, 0, 0, 3, 3, 0, 0, 1, 1])
    base_j = np.array([1, 2, 5, 6, 1, 5, 2, 6, 3, 7, 2, 6])
    base_k = np.array([2, 3, 6, 7, 5, 4, 6, 7, 7, 4, 6, 5])

    offsets = (np.arange(n_boxes) * 8)[:, None]
    I = (base_i[None, :] + offsets).reshape(-1)
    J = (base_j[None, :] + offsets).reshape(-1)
    K = (base_k[None, :] + offsets).reshape(-1)

    # Mismo valor de la celda repetido en sus 8 vértices -> color uniforme
    # por barra (no interpolado con las vecinas).
    intensidad = np.repeat(valores, 8)
    limite_color = float(np.max(np.abs(valores))) or 1.0

    fig = go.Figure(
        data=go.Mesh3d(
            x=x_flat, y=y_flat, z=z_flat,
            i=I, j=J, k=K,
            intensity=intensidad,
            colorscale=[[0.0, "#ef4444"], [0.5, "#12151c"], [1.0, "#22c55e"]],
            cmin=-limite_color, cmax=limite_color,
            flatshading=True,
            lighting=dict(ambient=0.55, diffuse=0.7, specular=0.25, roughness=0.6),
            showscale=False,
            hovertemplate="tamaño neto: %{intensity:.2f}<extra></extra>",
        )
    )

    fig.update_layout(
        uirevision=clave_camara,
        title=titulo,
        template="plotly_dark",
        paper_bgcolor="#0e1117",
        scene=dict(
            xaxis_title="snapshots recientes →",
            yaxis_title="precio (USD)",
            zaxis_title="tamaño (ask ⟵ ⟶ bid)",
            bgcolor="#0e1117",
            camera=dict(eye=dict(x=1.7, y=-1.7, z=0.85)),
            aspectmode="manual",
            aspectratio=dict(x=1.6, y=1.0, z=0.5),
        ),
        height=520,
        margin=dict(l=0, r=0, t=40, b=0),
    )

    return fig
