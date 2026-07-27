"""
resumen_diario.py — Lectura interpretada del día (solapa Macro & Geo)

Idea (de Jon):
    El calendario y los titulares ya están en la solapa, pero son datos
    CRUDOS: hay que leerlos y deducir. Este módulo los interpreta y
    responde la pregunta operativa directa: "¿qué puede mover BTC HOY,
    a qué hora, y en qué dirección probable?".

Entrada: exactamente lo que main.py ya obtiene del proxy —
    eventos_cal: lista de dicts {date, impact, country, title,
                 forecast, previous} (calendario ForexFactory)
    noticias:    lista de dicts {titulo, link, fuente}
Sin UNA sola llamada nueva a ninguna API: función pura sobre datos que
el dashboard ya tiene en el ciclo. Mismo criterio que market_bias.py.

Salida (dict):
    eventos_hoy     -> eventos de hoy con hora ARG y lectura operativa
    ventanas_vol    -> ventanas de volatilidad del día (sesiones/bolsas)
    sesgo_titulares -> {puntaje -100..100, etiqueta, drivers}
    sintesis        -> líneas de interpretación listas para mostrar

LÍMITE HONESTO: esto es interpretación HEURÍSTICA por palabras clave y
categoría de evento — no lee el contenido de las noticias ni predice el
dato. Un sesgo "risk-off" dice "los titulares recientes acumulan más
señales negativas que positivas", no "BTC va a bajar hoy".
"""

from datetime import datetime, timezone, timedelta

import pandas as pd

TZ_ARG = "America/Argentina/Buenos_Aires"

# ----------------------------------
# 1) LECTURA DE EVENTOS DEL CALENDARIO
# ----------------------------------
# Categorías de evento -> qué suele hacerle a BTC la sorpresa del dato.
# La lectura es condicional ("si sale por encima / por debajo") porque
# el dato en sí no se puede predecir — lo que sí se sabe de antemano es
# el MECANISMO de transmisión y la ventana de volatilidad.

_CATEGORIAS_EVENTO = [
    (("cpi", "inflation", "pce", "ppi"), "inflación",
     "Dato de inflación: por ENCIMA de lo esperado = dólar fuerte / presión "
     "bajista para BTC; por DEBAJO = alivio de tasas / impulso alcista. "
     "Barrido violento de liquidez en los primeros minutos."),
    (("non-farm", "nonfarm", "payroll", "unemployment", "jobless", "employment", "jolts"), "empleo",
     "Dato de empleo USA: empleo FUERTE = Fed sin apuro para recortar = "
     "presión bajista; empleo DÉBIL = expectativa de recortes = impulso "
     "alcista (salvo lectura de recesión, que es risk-off puro)."),
    (("fomc", "federal funds", "rate decision", "interest rate", "fed chair", "powell"), "Fed / tasas",
     "Evento de la Fed: el driver más pesado del mes. Tono dovish (recortes "
     "más cerca) = alcista para BTC; tono hawkish = bajista. La reacción "
     "inicial suele REVERTIRSE durante la conferencia — no perseguir la "
     "primera vela."),
    (("gdp", "pib"), "crecimiento",
     "PIB: por encima = economía firme (leve soporte risk-on); muy por "
     "debajo = miedo a recesión = risk-off que también golpea a BTC."),
    (("pmi", "ism", "manufacturing", "services"), "actividad",
     "PMI/ISM: dato de actividad de segunda línea — mueve si sorprende "
     "fuerte. >50 expansión (risk-on suave), <50 contracción (risk-off)."),
    (("retail sales", "consumer confidence", "sentiment"), "consumo",
     "Consumo USA: fuerte = economía firme pero Fed más dura (efecto mixto); "
     "débil = expectativa de recortes. Impacto usualmente moderado en BTC."),
    (("auction", "bond", "treasury"), "deuda",
     "Subasta/deuda del Tesoro: mueve tasas largas. Demanda floja = yields "
     "arriba = presión bajista para activos de riesgo, BTC incluido."),
]


def _leer_evento(titulo):
    """Devuelve (categoria, lectura) para un título de evento del
    calendario, o una lectura genérica si no matchea ninguna categoría."""
    t = (titulo or "").lower()
    for claves, categoria, lectura in _CATEGORIAS_EVENTO:
        if any(k in t for k in claves):
            return categoria, lectura
    return "otro", (
        "Evento de impacto en USD: esperar volatilidad alrededor de la "
        "publicación y evitar abrir posición nueva en los ±15 minutos."
    )


def _eventos_de_hoy(eventos_cal, ahora_utc):
    """Filtra los eventos del calendario que caen HOY (día local ARG,
    que es el día operativo de Jon) con impacto High/Medium, y les
    agrega hora local + lectura interpretada."""
    hoy_arg = pd.Timestamp(ahora_utc).tz_convert(TZ_ARG).date()
    salida = []
    for e in eventos_cal or []:
        if e.get("impact") not in ("High", "Medium"):
            continue
        try:
            ts = pd.to_datetime(e.get("date"))
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
            ts_arg = ts.tz_convert(TZ_ARG)
        except Exception:
            continue
        if ts_arg.date() != hoy_arg:
            continue
        categoria, lectura = _leer_evento(e.get("title"))
        salida.append({
            "hora_arg": ts_arg.strftime("%H:%M"),
            "ts_utc": ts.tz_convert("UTC"),
            "pendiente": ts >= pd.Timestamp(ahora_utc),
            "impacto": e.get("impact"),
            "pais": e.get("country", ""),
            "titulo": e.get("title", ""),
            "categoria": categoria,
            "lectura": lectura,
            "prevision": e.get("forecast") or "—",
            "previo": e.get("previous") or "—",
        })
    salida.sort(key=lambda x: x["hora_arg"])
    return salida


# ----------------------------------
# 2) VENTANAS DE VOLATILIDAD ESTRUCTURALES DEL DÍA
# ----------------------------------
# Independientes del calendario: aperturas/cierres de bolsas que todos
# los días concentran flujo. Complementan la solapa de Sesiones (esa
# trabaja niveles de precio; acá solo importan como catalizador horario).

_VENTANAS_FIJAS_UTC = [
    ((7, 0), "Apertura Europa", "entra el flujo institucional europeo — suele definirse la dirección de la mañana"),
    ((13, 30), "Apertura NYSE/Nasdaq", "la correlación BTC-equities se activa — el primer movimiento de NY suele barrer los extremos de Asia/Europa"),
    ((14, 30), "Primera hora NY cerrada", "fin del ruido de apertura — los movimientos desde acá tienden a ser más direccionales"),
    ((20, 0), "Cierre NYSE/Nasdaq", "rebalanceos de cierre — última ventana de volatilidad fuerte del día antes del rango asiático"),
]


def _ventanas_vol(ahora_utc):
    filas = []
    ahora = pd.Timestamp(ahora_utc)
    for (h, m), nombre, nota in _VENTANAS_FIJAS_UTC:
        ts = ahora.normalize().replace(hour=h, minute=m)
        filas.append({
            "hora_arg": ts.tz_convert(TZ_ARG).strftime("%H:%M"),
            "nombre": nombre,
            "nota": nota,
            "pendiente": ts >= ahora,
        })
    return filas


# ----------------------------------
# 3) SESGO DE TITULARES (heurístico por palabras clave)
# ----------------------------------
# Fuentes en inglés (CoinDesk, Cointelegraph, BBC) -> claves en inglés.
# Un titular cripto pesa DOBLE que uno de mundo: la transmisión a BTC
# es directa, no vía apetito de riesgo general.

_CLAVES_RISK_OFF = (
    "war", "attack", "strike", "missile", "invasion", "escalat", "conflict",
    "sanction", "nuclear", "troops", "hack", "exploit", "breach", "stolen",
    "lawsuit", "sues", "sued", "charges", "ban", "crackdown", "fraud",
    "liquidation", "crash", "plunge", "selloff", "sell-off", "tumble",
    "default", "tariff", "recession", "outflow", "dump", "fear", "collapse",
)
_CLAVES_RISK_ON = (
    "etf", "inflow", "approval", "approve", "adoption", "adopt", "rally",
    "surge", "record", "all-time high", "ath", "institutional", "accumulat",
    "rate cut", "cuts rates", "stimulus", "easing", "bullish", "buys",
    "reserve", "treasury adds", "partnership", "launch", "breakout",
    "recovery", "rebound", "soars", "jumps", "gains",
)
_CLAVES_CRIPTO = ("bitcoin", "btc", "crypto", "ethereum", "eth", "stablecoin",
                  "blockchain", "defi", "binance", "coinbase")


def _sesgo_titulares(noticias):
    puntaje, drivers = 0, []
    for nt in (noticias or [])[:20]:
        titulo = (nt.get("titulo") or "").lower()
        peso = 2 if any(k in titulo for k in _CLAVES_CRIPTO) else 1
        p_on = sum(1 for k in _CLAVES_RISK_ON if k in titulo)
        p_off = sum(1 for k in _CLAVES_RISK_OFF if k in titulo)
        # saturación por titular: un título con 5 keywords no vale 5
        # veces más que uno con 1 — vale "claramente positivo/negativo"
        neto = max(-2, min(2, p_on - p_off)) * peso
        if neto:
            puntaje += neto * 8
            drivers.append({
                "titulo": nt.get("titulo", ""),
                "fuente": nt.get("fuente", ""),
                "aporte": neto * 8,
            })
    puntaje = max(-100, min(100, puntaje))
    if puntaje >= 25:
        etiqueta = "RISK-ON"
    elif puntaje <= -25:
        etiqueta = "RISK-OFF"
    elif puntaje == 0 and not drivers:
        etiqueta = "SIN SEÑAL"
    else:
        etiqueta = "MIXTO"
    drivers.sort(key=lambda d: abs(d["aporte"]), reverse=True)
    return {"puntaje": puntaje, "etiqueta": etiqueta, "drivers": drivers[:5]}


# ----------------------------------
# 4) SÍNTESIS DEL DÍA
# ----------------------------------

def _sintetizar(eventos_hoy, sesgo, ventanas, ahora_utc):
    lineas = []

    pendientes = [e for e in eventos_hoy if e["pendiente"]]
    altos = [e for e in pendientes if e["impacto"] == "High"]

    # Driver principal del día
    if altos:
        e = altos[0]
        lineas.append(
            f"🎯 **Driver principal de hoy: {e['titulo']} ({e['pais']}) a las "
            f"{e['hora_arg']} ARG.** {e['lectura']}"
        )
        if len(altos) > 1:
            resto = ", ".join(f"{x['titulo']} ({x['hora_arg']})" for x in altos[1:])
            lineas.append(f"🔴 También de impacto alto hoy: {resto}.")
    elif pendientes:
        e = pendientes[0]
        lineas.append(
            f"🟠 Sin datos de impacto alto hoy. Lo más relevante pendiente: "
            f"{e['titulo']} ({e['pais']}) a las {e['hora_arg']} ARG — {e['lectura']}"
        )
    elif eventos_hoy:
        publicados = ", ".join(f"{x['titulo']} ({x['hora_arg']})" for x in eventos_hoy[-3:])
        lineas.append(
            f"✅ **Los datos del día ya se publicaron** ({publicados}). Sin "
            "catalizador de calendario por delante: lo que quede de movimiento "
            "es digestión del dato + flujo técnico — los niveles de la solapa "
            "Sesiones vuelven a ser el mapa principal."
        )
    else:
        lineas.append(
            "📭 **Día sin datos macro relevantes en el calendario.** Sin "
            "catalizador de calendario, el precio queda a merced del flujo "
            "técnico: los niveles de liquidez de la solapa Sesiones pasan a "
            "ser el mapa principal del día."
        )

    # Sesgo de titulares -> traducción operativa
    if sesgo["etiqueta"] == "RISK-OFF":
        lineas.append(
            f"📰 Titulares con sesgo **RISK-OFF** ({sesgo['puntaje']:+d}): el "
            "contexto de noticias rema en contra — desconfiar de rupturas "
            "alcistas sin volumen y dar más peso a los fades en resistencias."
        )
    elif sesgo["etiqueta"] == "RISK-ON":
        lineas.append(
            f"📰 Titulares con sesgo **RISK-ON** ({sesgo['puntaje']:+d}): el "
            "contexto de noticias acompaña — los retrocesos a soportes tienen "
            "más chance de ser comprados que de extenderse."
        )
    elif sesgo["etiqueta"] == "MIXTO":
        lineas.append(
            f"📰 Titulares **mixtos** ({sesgo['puntaje']:+d}): sin sesgo "
            "dominante en noticias — que el contexto no decida por vos, manda "
            "el nivel técnico."
        )
    else:
        lineas.append(
            "📰 Titulares sin señal direccional clara en este ciclo."
        )

    # Próxima ventana de volatilidad estructural
    prox = next((v for v in ventanas if v["pendiente"]), None)
    if prox:
        lineas.append(
            f"⏰ Próxima ventana de volatilidad estructural: **{prox['nombre']} "
            f"a las {prox['hora_arg']} ARG** — {prox['nota']}."
        )
    else:
        lineas.append(
            "🌙 Ya pasaron todas las ventanas estructurales del día — queda el "
            "rango asiático: volumen fino, movimientos más lentos y barridos "
            "de baja calidad. Exigirle más confirmación a cualquier señal."
        )

    return lineas


# ----------------------------------
# API PRINCIPAL
# ----------------------------------

def generar_resumen(eventos_cal, noticias, ahora_utc=None):
    """Todo junto, pensado para renderizarse arriba de la solapa Macro.
    Función pura: mismos datos de entrada -> misma salida."""
    ahora_utc = pd.Timestamp(ahora_utc or datetime.now(timezone.utc))
    if ahora_utc.tzinfo is None:
        ahora_utc = ahora_utc.tz_localize("UTC")

    eventos_hoy = _eventos_de_hoy(eventos_cal, ahora_utc)
    ventanas = _ventanas_vol(ahora_utc)
    sesgo = _sesgo_titulares(noticias)
    sintesis = _sintetizar(eventos_hoy, sesgo, ventanas, ahora_utc)

    return {
        "eventos_hoy": eventos_hoy,
        "ventanas_vol": ventanas,
        "sesgo_titulares": sesgo,
        "sintesis": sintesis,
    }
