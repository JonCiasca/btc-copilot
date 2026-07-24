"""
sesiones.py — Niveles de sesión, toma de liquidez y reacción (esqueleto v0.1)

Idea (de Jon):
    Cada sesión (Asia / Europa / New York) va dejando en el camino
    niveles de referencia: su APERTURA, su CIERRE, su ALTO y su BAJO.
    Cuando el precio vuelve a uno de esos niveles de sesiones
    recientes (no lejanas), puede ABSORBER liquidez ahí (barrido) y
    REACCIONAR. Este módulo detecta esos niveles, los barridos y la
    reacción, apuntando a movimientos de 1.000–1.500 USD (micro /
    intradiario), no a swings largos.

    Confluencia pendiente (TODO): régimen gamma vía opciones de
    Deribit — la reacción esperada NO es la misma en short gamma
    (los dealers persiguen el precio: el barrido tiende a extenderse)
    que en long gamma (los dealers amortiguan: el barrido tiende a
    revertir). Ver hook `confluencia_gamma()` abajo.

Entrada: DataFrame de velas 1m con columnas open_time (datetime UTC),
open, high, low, close, volume — el mismo que ya arma main.py con
obtener_velas("1m"). También funciona alimentado desde ws_hub.

Todo es puro-pandas, sin pedidos de red: pensado para correr tanto en
el proxy (Render) como en el dashboard.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone

import pandas as pd

# ----------------------------------
# CONFIGURACIÓN DE SESIONES (hora UTC)
# ----------------------------------
# Ventanas por defecto; ajustables sin tocar el resto del código.
# BTC opera 24/7, pero la liquidez "institucional" sigue estos husos.

SESIONES_UTC = {
    "Asia":   {"inicio": (0, 0),  "fin": (8, 0)},
    "Europa": {"inicio": (7, 0),  "fin": (16, 0)},
    "NY":     {"inicio": (12, 30), "fin": (21, 0)},
}

# Cuántas sesiones hacia atrás se consideran "recientes" (las que van
# quedando en el camino). Niveles más viejos se descartan.
MAX_SESIONES_ATRAS = 4

# Un nivel se considera BARRIDO cuando el precio lo cruza al menos
# esta distancia (USD) y luego el cierre vuelve del lado original.
PENETRACION_MINIMA_USD = 30.0

# Velas 1m máximas que puede tardar la reacción tras el cruce para
# que cuente como barrido con reacción (y no como ruptura genuina).
VELAS_MAX_REACCION = 15

# Reacción mínima esperada tras el barrido para marcar el evento como
# "reaccionó" (en USD desde el nivel). El objetivo operativo de Jon
# es 1.000–1.500 USD; esto es solo el umbral de confirmación inicial.
REACCION_MINIMA_USD = 150.0


@dataclass
class NivelSesion:
    sesion: str          # "Asia" / "Europa" / "NY"
    fecha: str           # fecha UTC de la sesión, "2026-07-24"
    tipo: str            # "apertura" | "cierre" | "alto" | "bajo"
    precio: float
    barrido: bool = False        # ¿ya fue barrido? (nivel mitigado)
    ts_barrido: str | None = None
    reacciono: bool = False      # ¿hubo reacción tras el barrido?
    reaccion_usd: float = 0.0    # magnitud de la reacción observada


# ----------------------------------
# 1) CONSTRUCCIÓN DE NIVELES
# ----------------------------------

def _ventana_sesion(fecha, nombre):
    cfg = SESIONES_UTC[nombre]
    ini = datetime(fecha.year, fecha.month, fecha.day,
                   *cfg["inicio"], tzinfo=timezone.utc)
    fin = datetime(fecha.year, fecha.month, fecha.day,
                   *cfg["fin"], tzinfo=timezone.utc)
    return ini, fin


def construir_niveles(df_1m, ahora=None):
    """Devuelve la lista de NivelSesion de las últimas sesiones
    COMPLETADAS (hasta MAX_SESIONES_ATRAS por mercado).

    df_1m: velas 1m, open_time en UTC (naive o aware, se normaliza).
    """
    if df_1m is None or df_1m.empty:
        return []

    df = df_1m.copy()
    if df["open_time"].dt.tz is None:
        df["open_time"] = df["open_time"].dt.tz_localize("UTC")

    ahora = ahora or datetime.now(timezone.utc)
    niveles = []

    # Recorremos los días presentes en el df, del más nuevo al más viejo
    fechas = sorted({t.date() for t in df["open_time"]}, reverse=True)

    for nombre in SESIONES_UTC:
        encontradas = 0
        for fecha in fechas:
            if encontradas >= MAX_SESIONES_ATRAS:
                break
            ini, fin = _ventana_sesion(fecha, nombre)
            if fin > ahora:          # sesión todavía abierta o futura
                continue
            bloque = df[(df["open_time"] >= ini) & (df["open_time"] < fin)]
            if bloque.empty:
                continue
            f = fecha.isoformat()
            niveles += [
                NivelSesion(nombre, f, "apertura", float(bloque["open"].iloc[0])),
                NivelSesion(nombre, f, "cierre",   float(bloque["close"].iloc[-1])),
                NivelSesion(nombre, f, "alto",     float(bloque["high"].max())),
                NivelSesion(nombre, f, "bajo",     float(bloque["low"].min())),
            ]
            encontradas += 1

    return niveles


# ----------------------------------
# 2) DETECCIÓN DE BARRIDO + REACCIÓN
# ----------------------------------

def detectar_barridos(df_1m, niveles):
    """Marca in-place qué niveles fueron barridos y si hubo reacción.

    Barrido de un ALTO/cierre-por-arriba: el precio cruza el nivel
    hacia ARRIBA (toma los stops que descansan encima) y el cierre
    vuelve por debajo dentro de VELAS_MAX_REACCION velas.
    Barrido de un BAJO: espejo hacia abajo.

    Para apertura/cierre el lado del barrido se decide según de qué
    lado venía operando el precio antes de tocar el nivel.
    Devuelve la lista de eventos nuevos detectados.
    """
    if df_1m is None or df_1m.empty or not niveles:
        return []

    df = df_1m.copy()
    if df["open_time"].dt.tz is None:
        df["open_time"] = df["open_time"].dt.tz_localize("UTC")

    eventos = []
    for nivel in niveles:
        if nivel.barrido:
            continue

        p = nivel.precio
        # lado del barrido: altos se barren hacia arriba, bajos hacia
        # abajo; aperturas/cierres, según dónde venía el precio.
        if nivel.tipo == "alto":
            lado = "arriba"
        elif nivel.tipo == "bajo":
            lado = "abajo"
        else:
            previas = df[df["close"] != p]
            if previas.empty:
                continue
            lado = "arriba" if previas["close"].iloc[-30:].mean() < p else "abajo"

        if lado == "arriba":
            cruzo = df[df["high"] >= p + PENETRACION_MINIMA_USD]
        else:
            cruzo = df[df["low"] <= p - PENETRACION_MINIMA_USD]
        if cruzo.empty:
            continue

        i0 = cruzo.index[0]
        despues = df.loc[i0:].head(VELAS_MAX_REACCION)
        if despues.empty:
            continue

        if lado == "arriba":
            volvio = despues[despues["close"] < p]
            reaccion = p - float(despues["low"].min())
        else:
            volvio = despues[despues["close"] > p]
            reaccion = float(despues["high"].max()) - p

        if not volvio.empty:
            nivel.barrido = True
            nivel.ts_barrido = str(df.loc[i0, "open_time"])
            nivel.reacciono = reaccion >= REACCION_MINIMA_USD
            nivel.reaccion_usd = round(reaccion, 1)
            eventos.append({
                "nivel": asdict(nivel),
                "lado_barrido": lado,
                "direccion_reaccion": "bajista" if lado == "arriba" else "alcista",
                # TODO: acá entra la confluencia gamma (ver abajo)
                "gamma": confluencia_gamma(),
            })

    return eventos


# ----------------------------------
# 3) CONFLUENCIA GAMMA (hook, TODO)
# ----------------------------------

def confluencia_gamma():
    """TODO (próxima etapa): calcular el régimen gamma desde la cadena
    de opciones BTC de Deribit (get_book_summary_by_currency ya se usa
    en main.py para IV) y devolver algo como:

        {"regimen": "short_gamma" | "long_gamma",
         "gex_neto": ..., "strike_muro": ...}

    Lectura operativa:
      - long gamma  -> dealers amortiguan: el barrido tiende a
        REVERTIR con más probabilidad (favorece el fade del nivel).
      - short gamma -> dealers persiguen: el barrido puede EXTENDERSE
        (ojo con fadear; mejor esperar confirmación de reacción).
    """
    return {"regimen": "sin_datos", "nota": "pendiente integrar Deribit"}


# ----------------------------------
# RESUMEN RÁPIDO (para dashboard / endpoint)
# ----------------------------------

def resumen(df_1m):
    """Todo junto: niveles vigentes + eventos de barrido detectados.
    Pensado para exponerse como endpoint /sesiones en el proxy."""
    niveles = construir_niveles(df_1m)
    eventos = detectar_barridos(df_1m, niveles)
    precio = float(df_1m["close"].iloc[-1]) if df_1m is not None and not df_1m.empty else None
    return {
        "precio_actual": precio,
        "niveles": [asdict(n) for n in niveles],
        "eventos_barrido": eventos,
        "niveles_sin_mitigar_cercanos": sorted(
            (asdict(n) for n in niveles
             if not n.barrido and precio is not None
             and abs(n.precio - precio) <= 1500),
            key=lambda n: abs(n["precio"] - precio),
        )[:8] if precio is not None else [],
    }
