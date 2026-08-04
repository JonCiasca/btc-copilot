"""
confluencia_flow.py — Confluencia by JonFlowMDQ

Dos señales de confluencia direccional, pensadas para COMPLEMENTAR
(no reemplazar) el Scalp Edge Score existente -- que sigue sin estar
bien calibrado, así que esto arranca 100% aislado: paneles
informativos que no tocan scalp_edge ni el resto del scoring hasta
que se validen en vivo.

Setup 1 -- Confluencia MTF (3m / 5m / 15m):
    Sobre la última vela CERRADA de cada temporalidad, evalúa 4
    criterios (cuerpo amplio, volumen elevado, delta de flujo taker
    a favor, Open Interest alineado) y arma un score de confluencia
    entre las 3 temporalidades.

Setup 2 -- Retest de Vacío:
    Después de un quiebre importante que arranca en una zona clave
    (Imán / Gamma Pinning / Flip) y deja un vacío grande en la
    dirección contraria al quiebre, evalúa si el retest de ese
    vacío barre liquidez y recupera impulso en la dirección
    ORIGINAL del quiebre -- eso confirma la entrada de continuación.

UMBRALES: son puntos de partida (default), NO están validados en
vivo todavía -- mismo criterio que se usó con ICEBERG_*/IMAN_* en
bookmap3d: arrancan con un valor razonable y se ajustan según
feedback real, no son gospel.

SUPUESTOS DE SETUP 2 (a confirmar/ajustar con Jon, elegidos para
poder arrancar sin bloquear el resto del build):
  - Vacío = distancia entre el nivel clave donde arrancó el quiebre
    y el precio de "aterrizaje" tras el impulso, medida en múltiplos
    de ATR(14) -- en vez de un Fair Value Gap clásico de 3 velas, por
    ser más robusto a ruido en cripto y no necesitar velas de baja
    temporalidad extra.
  - Corre en 15m (quiebre estructural, no timing de scalp -- para
    timing rápido está Setup 1).
  - "Toma liquidez" en el retest = el precio, al volver a la zona
    del vacío, tiene al menos un nivel de detectar_niveles_liquidez()
    (el detector que YA existe en main.py) dentro de esa zona -- no
    se construyó un detector de liquidez nuevo.
"""

import pandas as pd


# ==========================================================
# SETUP 1 — CONFLUENCIA MTF (3m / 5m / 15m)
# ==========================================================

UMBRAL_CUERPO_PCT = 0.65        # cuerpo/rango >= 65% -> vela "de cuerpo amplio"
UMBRAL_VOLUMEN_X = 1.2          # volumen >= 1.2x promedio móvil reciente (mismo umbral que Absorción)
UMBRAL_DELTA_PCT = 0.55         # taker buy% (o sell%) >= 55% a favor de la dirección de la vela
VENTANA_VOLUMEN_PROMEDIO = 20   # velas hacia atrás para el promedio de volumen


def _evaluar_vela_confluencia(df, oi_cambio_pct=None):
    """
    Evalúa la ÚLTIMA vela CERRADA de un df de klines (obtener_velas
    devuelve la vela en curso como última fila mientras no cierra, por
    eso se usa iloc[-2] -- la fila anterior a la última -- como "última
    vela cerrada").

    oi_cambio_pct: cambio % de Open Interest medido en la MISMA
    ventana de tiempo que este TF (ej: OI ahora vs. OI hace 3 minutos
    para el TF de 3m). Si no se pasa, ese criterio no suma ni resta
    (el resultado sigue siendo válido, solo con 3 criterios en vez de 4).

    Devuelve dict con dirección, puntos (0-4) y desglose por criterio.
    """
    resultado = {
        "direccion": "neutral",
        "puntos": 0,
        "cuerpo_ok": False,
        "volumen_ok": False,
        "delta_ok": False,
        "oi_ok": False,
        "cuerpo_pct": 0.0,
        "volumen_x": 0.0,
        "delta_pct": 0.0,
    }

    if df is None or len(df) < VENTANA_VOLUMEN_PROMEDIO + 2:
        return resultado

    vela = df.iloc[-2]
    rango = vela["high"] - vela["low"]

    if rango <= 0:
        return resultado

    cuerpo = vela["close"] - vela["open"]
    cuerpo_pct = abs(cuerpo) / rango
    resultado["cuerpo_pct"] = round(float(cuerpo_pct), 3)

    if cuerpo_pct < UMBRAL_CUERPO_PCT:
        return resultado  # sin dirección clara -- no tiene sentido seguir evaluando

    resultado["direccion"] = "alcista" if cuerpo > 0 else "bajista"
    resultado["cuerpo_ok"] = True
    resultado["puntos"] += 1

    # Volumen: promedio de las N velas ANTERIORES a la última cerrada
    # (excluye la vela en curso y la última cerrada del propio promedio)
    promedio_vol = df["volume"].iloc[-(VENTANA_VOLUMEN_PROMEDIO + 2):-2].mean()
    if promedio_vol and promedio_vol > 0:
        volumen_x = vela["volume"] / promedio_vol
        resultado["volumen_x"] = round(float(volumen_x), 2)
        if volumen_x >= UMBRAL_VOLUMEN_X:
            resultado["volumen_ok"] = True
            resultado["puntos"] += 1

    # Delta de flujo: taker_buy_base ya viene en el kline de Binance
    # (mismo campo que usa buy_pressure/sell_pressure en main.py) --
    # NO es el delta de Black-Scholes de Opciones, ojo con no
    # confundir nombres en el resto del código.
    if vela["volume"] and vela["volume"] > 0:
        buy_pct = vela["taker_buy_base"] / vela["volume"]
        delta_a_favor = buy_pct if resultado["direccion"] == "alcista" else (1 - buy_pct)
        resultado["delta_pct"] = round(float(delta_a_favor) * 100, 1)
        if delta_a_favor >= UMBRAL_DELTA_PCT:
            resultado["delta_ok"] = True
            resultado["puntos"] += 1

    # OI alineado: sube en la ventana del TF, sin importar si la vela
    # es alcista o bajista -- OI subiendo = posiciones NUEVAS entrando
    # a favor del movimiento (confluencia real). OI bajando con precio
    # moviéndose = cierre de posiciones (short squeeze / liquidación de
    # longs) -- más débil, misma lectura que ya usa el bloque de Flow
    # existente en main.py -- por eso acá NO suma.
    if oi_cambio_pct is not None and oi_cambio_pct > 0:
        resultado["oi_ok"] = True
        resultado["puntos"] += 1

    return resultado


def calcular_confluencia_mtf(df_3m, df_5m, df_15m,
                              oi_cambio_3m=None, oi_cambio_5m=None, oi_cambio_15m=None):
    """
    Punto de entrada de Setup 1. Devuelve el detalle por TF más el
    resumen de confluencia (cuántos TFs coinciden en dirección) y
    fuerza (suma de puntos de los TFs alineados, máx 12).
    """
    tf3 = _evaluar_vela_confluencia(df_3m, oi_cambio_3m)
    tf5 = _evaluar_vela_confluencia(df_5m, oi_cambio_5m)
    tf15 = _evaluar_vela_confluencia(df_15m, oi_cambio_15m)

    direcciones = [tf3["direccion"], tf5["direccion"], tf15["direccion"]]
    alcistas = direcciones.count("alcista")
    bajistas = direcciones.count("bajista")

    if alcistas == 0 and bajistas == 0:
        direccion_confluencia = "sin confluencia"
        coincidentes = 0
    elif alcistas >= bajistas:
        direccion_confluencia = "alcista"
        coincidentes = alcistas
    else:
        direccion_confluencia = "bajista"
        coincidentes = bajistas

    fuerza = sum(
        tf["puntos"] for tf in (tf3, tf5, tf15) if tf["direccion"] == direccion_confluencia
    )

    return {
        "tf_3m": tf3,
        "tf_5m": tf5,
        "tf_15m": tf15,
        "direccion": direccion_confluencia,
        "coincidentes": coincidentes,   # de 3
        "fuerza": fuerza,                # de 12
    }


def calcular_cambio_oi_pct(historial_oi, minutos):
    """
    historial_oi: lista de tuplas (timestamp datetime, valor_oi),
    ordenada de más vieja a más nueva -- main.py la arma en
    session_state (ver nota de integración), acá solo se lee.

    Devuelve el cambio % de OI entre el valor más cercano a
    "ahora - minutos" y el valor más reciente. None si no hay
    suficiente historial todavía (recién arrancado el dashboard).
    """
    if not historial_oi or len(historial_oi) < 2:
        return None

    ahora_ts, ahora_valor = historial_oi[-1]
    objetivo = ahora_ts - pd.Timedelta(minutes=minutos)

    candidato = None
    for ts, valor in historial_oi:
        if ts <= objetivo:
            candidato = valor
        else:
            break

    if candidato is None or candidato == 0:
        return None

    return ((ahora_valor - candidato) / candidato) * 100


# ==========================================================
# SETUP 2 — RETEST DE VACÍO
# ==========================================================

UMBRAL_ATR_VACIO = 1.5           # vacío >= 1.5x ATR(14) para contar como "grande"
VENTANA_ATR = 14
VELAS_ATRAS_BUSQUEDA_QUIEBRE = 10
TOLERANCIA_NIVEL_CLAVE_PCT = 0.35  # mismo umbral que ya usa _detectar_iman_dorado en main.py


def _calcular_atr(df, ventana=VENTANA_ATR):
    if df is None or len(df) < ventana + 1:
        return None

    alto = df["high"]
    bajo = df["low"]
    cierre_prev = df["close"].shift(1)

    tr = pd.concat([
        alto - bajo,
        (alto - cierre_prev).abs(),
        (bajo - cierre_prev).abs(),
    ], axis=1).max(axis=1)

    atr = tr.rolling(ventana).mean().iloc[-1]
    return float(atr) if pd.notna(atr) else None


def detectar_quiebre_en_zona_clave(df, niveles_clave, tolerancia_pct=TOLERANCIA_NIVEL_CLAVE_PCT):
    """
    df: velas de 15m (ver supuesto de temporalidad en el docstring del
    módulo).
    niveles_clave: lista de precios -- pasar acá los mismos que ya arma
    main.py para iman_dorado_grupos (Imán dorado, strike_pin de Gamma
    Pinning, flip_point de Flip Local/Global). No se recalculan zonas
    nuevas, se reusan las que ya existen.

    Busca, en las últimas VELAS_ATRAS_BUSQUEDA_QUIEBRE velas, la vela
    de impulso más reciente (cuerpo amplio, mismo umbral que Setup 1)
    cuya apertura esté pegada a un nivel clave. Si el precio actual
    quedó a >= UMBRAL_ATR_VACIO x ATR de esa apertura, hay "vacío
    grande" -- devuelve el quiebre. Si no encuentra nada, devuelve None
    (no hay quiebre reciente relevante, no es un error).
    """
    if df is None or len(df) < VENTANA_ATR + VELAS_ATRAS_BUSQUEDA_QUIEBRE or not niveles_clave:
        return None

    atr = _calcular_atr(df)
    if not atr or atr <= 0:
        return None

    inicio_busqueda = max(len(df) - VELAS_ATRAS_BUSQUEDA_QUIEBRE - 1, VENTANA_ATR)

    for i in range(len(df) - 2, inicio_busqueda, -1):
        vela = df.iloc[i]
        rango = vela["high"] - vela["low"]
        if rango <= 0:
            continue

        cuerpo = vela["close"] - vela["open"]
        cuerpo_pct = abs(cuerpo) / rango
        if cuerpo_pct < UMBRAL_CUERPO_PCT:
            continue

        origen = vela["open"]
        if origen == 0:
            continue

        cerca_de_nivel_clave = any(
            abs(origen - nivel) / nivel * 100 <= tolerancia_pct
            for nivel in niveles_clave
        )
        if not cerca_de_nivel_clave:
            continue

        direccion = "bajista" if cuerpo < 0 else "alcista"
        precio_aterrizaje = df["close"].iloc[-1]
        tamano_vacio = abs(origen - precio_aterrizaje)

        if tamano_vacio < UMBRAL_ATR_VACIO * atr:
            continue  # el vacío no es "grande" todavía -- no cuenta como setup

        return {
            "direccion_quiebre": direccion,
            "nivel_origen": float(origen),
            "vacio_desde": float(min(origen, precio_aterrizaje)),
            "vacio_hasta": float(max(origen, precio_aterrizaje)),
            "tamano_vacio_usd": round(float(tamano_vacio), 1),
            "tamano_vacio_atr": round(float(tamano_vacio / atr), 2),
        }

    return None


def evaluar_retest_vacio(df, quiebre, niveles_liquidez_soporte, niveles_liquidez_resistencia,
                          estado_velocidad):
    """
    quiebre: dict devuelto por detectar_quiebre_en_zona_clave (None si
    no hay quiebre vigente -- en ese caso esta función no hace nada).
    niveles_liquidez_soporte / resistencia: exactamente lo que ya
    devuelve detectar_niveles_liquidez() en main.py -- no se recalcula
    nada nuevo acá.
    estado_velocidad: el mismo string que ya calcula main.py
    ("acelerando", etc.) para el bloque de Flow/Cerebro Copilot.

    Confirma entrada si: el precio actual está DENTRO del rango del
    vacío, hay al menos un nivel de liquidez existente dentro de esa
    zona (= "hay algo para barrer"), y estado_velocidad indica impulso
    recuperándose -- en la dirección ORIGINAL del quiebre, no en la del
    retroceso.
    """
    if not quiebre:
        return {"en_retest": False, "confirmado": False, "quiebre": None}

    precio_actual = float(df["close"].iloc[-1])
    en_vacio = quiebre["vacio_desde"] <= precio_actual <= quiebre["vacio_hasta"]

    if not en_vacio:
        return {"en_retest": False, "confirmado": False, "quiebre": quiebre}

    niveles_en_vacio = [
        n for n in list(niveles_liquidez_soporte) + list(niveles_liquidez_resistencia)
        if quiebre["vacio_desde"] <= n <= quiebre["vacio_hasta"]
    ]

    hay_liquidez_para_barrer = len(niveles_en_vacio) > 0
    impulso_recuperado = estado_velocidad == "acelerando"

    confirmado = hay_liquidez_para_barrer and impulso_recuperado

    return {
        "en_retest": True,
        "confirmado": confirmado,
        "direccion": quiebre["direccion_quiebre"],
        "niveles_en_vacio": niveles_en_vacio,
        "impulso_recuperado": impulso_recuperado,
        "quiebre": quiebre,
    }
