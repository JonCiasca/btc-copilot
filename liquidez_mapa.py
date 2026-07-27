"""
liquidez_mapa.py — Mapa visual de liquidez de sesiones (solapa Sesiones)

Idea (de Jon):
    La tabla de niveles obliga a leer fila por fila. Este módulo arma
    UN gráfico que se interpreta de un vistazo: qué liquidez ya fue
    obtenida/superada (barrida) y — sobre todo — qué sitios PENDIENTES
    de liquidar/absorber tienen mayor probabilidad de interacción.

Entrada: la salida de sesiones.resumen() que main.py ya calcula en la
solapa (niveles + precio) y el mismo df de velas. Sin llamadas nuevas.

Dos piezas:
    puntuar_niveles(niveles, precio_actual)  -> niveles enriquecidos con
        prob_interaccion (0-100) para los NO mitigados
    figura_liquidez(df_velas, niveles_punt, precio_actual) -> go.Figure
        precio reciente a la izquierda + niveles proyectados a la
        derecha: pendientes coloreados por probabilidad, barridos
        apagados en gris con su marca de mitigado

HEURÍSTICA DE PROBABILIDAD (honesta, sin calibración estadística aún):
    - Cercanía al precio (decae exponencial, escala ~$1.200)... 55 pts
    - Tipo de nivel (alto/bajo = stops reales descansando)...... 20 pts
      (apertura/cierre = referencia, menos stops)................ 12 pts
    - Recencia de la sesión (hoy > ayer > anteayer)............. 15 pts
    - Clúster (otro nivel sin mitigar a < $200: imán doble)..... 10 pts
    Es un ORDENAMIENTO relativo ("cuál mirar primero"), no una
    probabilidad estadística real — misma advertencia que market_bias.
"""

from datetime import datetime, timezone

import math
import pandas as pd
import plotly.graph_objects as go

# Paleta oficial del dashboard (ver _inyectar_estilos en main.py)
_COL_FONDO = "#0e1117"
_COL_PRECIO = "#8ab4f8"
_COL_BARRIDO = "#4b5263"
_COL_DORADO = "#f0a500"

# Escala de probabilidad: fría (baja) -> dorada/roja (alta)
_ESCALA_PROB = [
    (0.0, "#3d4457"),
    (0.4, "#8a6d1f"),
    (0.7, "#f0a500"),
    (1.0, "#ef4444"),
]

ESCALA_DISTANCIA_USD = 1200.0   # decaimiento de cercanía
RADIO_CLUSTER_USD = 200.0       # dos niveles a < esto = imán doble
MAX_ETIQUETAS = 14              # niveles rotulados como máximo


def _color_prob(p):
    """Interpola _ESCALA_PROB en p (0-1) -> color hex."""
    p = max(0.0, min(1.0, p))
    for (p0, c0), (p1, c1) in zip(_ESCALA_PROB, _ESCALA_PROB[1:]):
        if p <= p1:
            f = 0 if p1 == p0 else (p - p0) / (p1 - p0)
            rgb0 = tuple(int(c0[i:i + 2], 16) for i in (1, 3, 5))
            rgb1 = tuple(int(c1[i:i + 2], 16) for i in (1, 3, 5))
            mez = tuple(round(a + (b - a) * f) for a, b in zip(rgb0, rgb1))
            return "#%02x%02x%02x" % mez
    return _ESCALA_PROB[-1][1]


def puntuar_niveles(niveles, precio_actual, ahora_utc=None):
    """Enriquece la lista de niveles (dicts de sesiones.resumen) con
    prob_interaccion (solo para los no barridos). Devuelve lista nueva
    ordenada: pendientes por probabilidad desc, después los barridos."""
    if not niveles or precio_actual is None:
        return []

    ahora = pd.Timestamp(ahora_utc or datetime.now(timezone.utc))
    if ahora.tzinfo is None:
        ahora = ahora.tz_localize("UTC")
    hoy = ahora.date()

    pendientes = [dict(n) for n in niveles if not n.get("barrido")]
    barridos = [dict(n) for n in niveles if n.get("barrido")]

    for n in pendientes:
        dist = abs(n["precio"] - precio_actual)

        pts_cerca = 55.0 * math.exp(-dist / ESCALA_DISTANCIA_USD)
        pts_tipo = 20.0 if n["tipo"] in ("alto", "bajo") else 12.0

        try:
            dias = (hoy - pd.Timestamp(n["fecha"]).date()).days
        except Exception:
            dias = 2
        pts_rec = 15.0 if dias <= 0 else (10.0 if dias == 1 else 5.0)

        vecinos = sum(
            1 for m in pendientes
            if m is not n and abs(m["precio"] - n["precio"]) <= RADIO_CLUSTER_USD
        )
        pts_cluster = 10.0 if vecinos else 0.0

        n["prob_interaccion"] = round(
            min(100.0, pts_cerca + pts_tipo + pts_rec + pts_cluster), 1
        )
        n["distancia_usd"] = round(n["precio"] - precio_actual, 1)
        n["lado"] = "arriba" if n["precio"] >= precio_actual else "abajo"

    pendientes.sort(key=lambda n: -n["prob_interaccion"])
    return pendientes + barridos


def figura_liquidez(df_velas, niveles_punt, precio_actual, horas_precio=10):
    """Gráfico de lectura rápida: línea de precio reciente + niveles.

    df_velas: el mismo df 5m de la solapa (open_time, close, ...).
    niveles_punt: salida de puntuar_niveles().
    """
    fig = go.Figure()

    # --- Línea de precio reciente (contexto, tercio izquierdo) ---
    t_fin_precio = None
    if df_velas is not None and not df_velas.empty:
        df = df_velas.copy()
        if df["open_time"].dt.tz is None:
            df["open_time"] = df["open_time"].dt.tz_localize("UTC")
        corte = df["open_time"].iloc[-1] - pd.Timedelta(hours=horas_precio)
        df = df[df["open_time"] >= corte]
        fig.add_trace(go.Scatter(
            x=df["open_time"], y=df["close"], mode="lines",
            line=dict(color=_COL_PRECIO, width=1.6),
            name="Precio (5m)", hoverinfo="skip",
        ))
        t_ini = df["open_time"].iloc[0]
        t_fin_precio = df["open_time"].iloc[-1]
        # zona de proyección a la derecha (donde viven los niveles)
        t_fin_mapa = t_fin_precio + (t_fin_precio - t_ini) * 0.55
    else:
        t_fin_precio = pd.Timestamp(datetime.now(timezone.utc))
        t_fin_mapa = t_fin_precio + pd.Timedelta(hours=5)

    pendientes = [n for n in niveles_punt if not n.get("barrido")]
    barridos = [n for n in niveles_punt if n.get("barrido")]

    # --- Niveles BARRIDOS: apagados, línea punteada corta ---
    for i, n in enumerate(barridos):
        fig.add_trace(go.Scatter(
            x=[t_fin_precio, t_fin_mapa], y=[n["precio"], n["precio"]],
            mode="lines",
            line=dict(color=_COL_BARRIDO, width=1, dash="dot"),
            opacity=0.45,
            name="Barrido / mitigado", legendgroup="barridos",
            showlegend=(i == 0),
            hovertemplate=(
                f"✔ BARRIDO — {n['tipo'].upper()} {n['sesion']} {n['fecha']}"
                f"<br>${n['precio']:,.0f} · reacción {n.get('reaccion_usd', 0):,.0f} USD"
                "<extra></extra>"
            ),
        ))

    # --- Niveles PENDIENTES: color e intensidad por probabilidad ---
    pendientes_orden = sorted(pendientes, key=lambda n: -n["prob_interaccion"])

    # Anti-colisión de etiquetas: si dos niveles están casi pegados, la
    # etiqueta se la queda el de mayor probabilidad (el hover conserva
    # el detalle de todos). Umbral relativo al rango de precios visible.
    precios_vis = [n["precio"] for n in niveles_punt] + ([precio_actual] if precio_actual else [])
    rango_y = (max(precios_vis) - min(precios_vis)) if len(precios_vis) > 1 else 1000.0
    gap_min = max(60.0, rango_y * 0.028)
    y_etiquetados = []

    for i, n in enumerate(pendientes_orden):
        p = n["prob_interaccion"] / 100.0
        color = _color_prob(p)
        destacado = i < 3  # el top-3 se dibuja más grueso
        fig.add_trace(go.Scatter(
            x=[t_fin_precio, t_fin_mapa], y=[n["precio"], n["precio"]],
            mode="lines",
            line=dict(color=color, width=3.2 if destacado else 1.6,
                      dash="solid" if destacado else "dash"),
            opacity=0.55 + 0.45 * p,
            name="Pendiente (color = prob.)", legendgroup="pendientes",
            showlegend=(i == 0),
            hovertemplate=(
                f"🧲 PENDIENTE — {n['tipo'].upper()} {n['sesion']} {n['fecha']}"
                f"<br>${n['precio']:,.0f} ({n['distancia_usd']:+,.0f} USD)"
                f"<br>prob. interacción: {n['prob_interaccion']:.0f}/100"
                "<extra></extra>"
            ),
        ))
        colisiona = any(abs(n["precio"] - y) < gap_min for y in y_etiquetados)
        if i < MAX_ETIQUETAS and not colisiona:
            y_etiquetados.append(n["precio"])
            fig.add_annotation(
                x=t_fin_mapa, y=n["precio"], xanchor="left", showarrow=False,
                text=(f"<b>{n['prob_interaccion']:.0f}</b> · "
                      f"{n['tipo'][:4].upper()} {n['sesion']}"),
                font=dict(size=10 if destacado else 9, color=color,
                          family="JetBrains Mono, monospace"),
                bgcolor="rgba(14,17,23,0.75)",
            )

    # --- Precio actual ---
    if precio_actual is not None:
        fig.add_hline(
            y=precio_actual, line_color="#e8eaed", line_width=1,
            line_dash="dash",
            annotation_text=f"  ${precio_actual:,.0f}",
            annotation_position="left",
            annotation_font=dict(color="#e8eaed", size=11,
                                 family="JetBrains Mono, monospace"),
        )

    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor=_COL_FONDO, plot_bgcolor=_COL_FONDO,
        height=520,
        margin=dict(l=55, r=120, t=30, b=10),
        legend=dict(orientation="h", yanchor="bottom", y=1.01,
                    xanchor="left", x=0, font=dict(size=10)),
        xaxis=dict(showgrid=False, title=None),
        yaxis=dict(showgrid=True, gridcolor="#1c2130", title=None,
                   tickformat=",.0f", side="left"),
        hoverlabel=dict(bgcolor="#161b26",
                        font=dict(family="JetBrains Mono, monospace", size=11)),
    )
    return fig
