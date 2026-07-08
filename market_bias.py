"""
market_bias.py
--------------
Vector de sesgo direccional consolidado (-100 a +100).

Objetivo: responder una sola pregunta con precisión de analista —
"con todo lo que el dashboard ya sabe, ¿hacia dónde se inclina el
mercado ahora, y con cuánta confianza?" — sin agregar UNA sola
llamada nueva a ninguna API. Esta función es pura: recibe los
resultados que main.py YA calculó en el ciclo actual (scores,
absorción, presión, funding, OI, tendencia, flip, imán dorado) y los
consolida en un solo número con desglose.

CONVENCIÓN DE SIGNO: positivo = sesgo alcista, negativo = sesgo
bajista, 0 = neutral. La magnitud NO es una probabilidad de subida;
es una medida de cuántas de las 5 familias de señales independientes
apuntan hacia el mismo lado y con qué fuerza relativa.

LAS 5 FAMILIAS (peso fijo, suman 100):
  1. Absorción + Flip Local (defensa de dealer)........... 25
  2. Presión taker + velocidad (momentum de mercado)....... 25
  3. Funding + OI (apalancamiento institucional)........... 20
  4. Tendencia estructural 1H............................. 20
  5. Imán Dorado / gravedad de precio (confluencia)........ 10

LÍMITE HONESTO: esto es un consolidado ESTADÍSTICO de señales ya
existentes, no una predicción determinística. Un bias de +80 dice
"5 de 5 familias apuntan alcista, con fuerza" — no dice "el precio
va a subir". La lectura textual siempre debe leerse en ese marco.
"""


def _clamp(valor, minimo=-100, maximo=100):
    return max(minimo, min(maximo, valor))


def _componente_absorcion_flip(hay_absorcion, detalle_absorcion, resultado_flip_local,
                                precio_actual, dist_flip_umbral_alto=0.8):
    """
    Defensa de nivel = señal de dealer, la más "cara" de fabricar
    artificialmente. Si hay absorción Y el Flip Local está cerca y del
    lado defendido, es la señal más confiable del set.

    Devuelve (puntos_con_signo de -25 a +25, activo: bool, detalle: str)
    """
    peso_max = 25

    if not resultado_flip_local or not resultado_flip_local.get("flip_point"):
        return 0, False, "Sin Flip Local disponible este ciclo."

    flip_point = resultado_flip_local["flip_point"]
    dist_pct = abs(flip_point - precio_actual) / precio_actual * 100
    lado = "alcista" if flip_point < precio_actual else "bajista"
    signo = 1 if lado == "alcista" else -1

    if dist_pct >= dist_flip_umbral_alto * 2.5:
        # Flip Local demasiado lejos para ser información operable ahora
        return 0, False, f"Flip Local a {dist_pct:.2f}% — demasiado lejos para pesar."

    fuerza = 1.0 if dist_pct < dist_flip_umbral_alto else 0.5

    if hay_absorcion:
        fuerza = 1.0  # absorción confirmada sube la fuerza al máximo
        etiqueta = "con absorción confirmada"
    elif detalle_absorcion and detalle_absorcion.get("score", 0) >= 65:
        fuerza = max(fuerza, 0.7)
        etiqueta = "con absorción en formación"
    else:
        etiqueta = "sin absorción todavía"

    puntos = signo * peso_max * fuerza
    detalle = f"Flip Local {lado} a {dist_pct:.2f}%, {etiqueta}."
    return puntos, True, detalle


def _componente_presion_velocidad(buy_pressure, sell_pressure, estado_velocidad):
    """
    Momentum de mercado: desbalance de presión taker, amplificado si
    el movimiento está acelerando (impulso ganando fuerza) y atenuado
    si está desacelerando (posible absorción/reversión cerca).

    Devuelve (puntos_con_signo de -25 a +25, activo: bool, detalle: str)
    """
    peso_max = 25
    desbalance = (buy_pressure - sell_pressure) / 100  # -1 a +1 aprox

    multiplicador = {
        "acelerando": 1.15,
        "estable": 0.85,
        "desacelerando": 0.55,
        "lateral": 0.55,
    }.get(estado_velocidad, 0.85)

    puntos = _clamp(desbalance * peso_max * multiplicador, -peso_max, peso_max)
    lado = "comprador" if desbalance > 0 else ("vendedor" if desbalance < 0 else "equilibrado")
    detalle = f"Presión {lado} ({buy_pressure:.0f}%/{sell_pressure:.0f}%), velocidad {estado_velocidad}."
    return puntos, True, detalle


def _componente_funding_oi(funding_disponible, funding_valor, oi_disponible, cambio_oi):
    """
    Apalancamiento institucional: funding positivo = largos pagando a
    cortos (mercado inclinado alcista); si además el OI crece, es
    apalancamiento NUEVO entrando (no solo cierre de posiciones).

    Devuelve (puntos_con_signo de -20 a +20, activo: bool, detalle: str)
    """
    peso_max = 20

    if not funding_disponible:
        return 0, False, "Sin funding disponible este ciclo."

    signo_funding = 1 if funding_valor > 0 else (-1 if funding_valor < 0 else 0)
    intensidad_funding = min(abs(funding_valor) / 0.03, 1.0)  # satura en ~0.03%

    factor_oi = 1.0
    if oi_disponible and cambio_oi is not None:
        if (cambio_oi > 0.1 and signo_funding > 0) or (cambio_oi < -0.1 and signo_funding < 0):
            factor_oi = 1.3  # OI confirma la dirección del funding
        elif (cambio_oi > 0.1 and signo_funding < 0) or (cambio_oi < -0.1 and signo_funding > 0):
            factor_oi = 0.5  # OI contradice al funding -- señal débil

    puntos = _clamp(signo_funding * intensidad_funding * peso_max * factor_oi, -peso_max, peso_max)
    detalle = f"Funding {funding_valor:+.4f}%, OI {'confirma' if factor_oi > 1 else ('contradice' if factor_oi < 1 else 'sin dato')}."
    return puntos, True, detalle


def _componente_tendencia_1h(tendencia_1h):
    """
    Tendencia estructural: la más lenta de las 5, pero la que da
    contexto de fondo -- sin ella, un pico de presión de 5 minutos
    puede parecer más importante de lo que es.

    Devuelve (puntos_con_signo de -20 a +20, activo: bool, detalle: str)
    """
    peso_max = 20

    if "Alcista" in tendencia_1h:
        return peso_max, True, "Tendencia 1H alcista."
    elif "Bajista" in tendencia_1h:
        return -peso_max, True, "Tendencia 1H bajista."
    else:
        return 0, True, "Tendencia 1H neutral."


def _componente_iman_dorado(iman_dorado_activo, precio_actual):
    """
    Gravedad de precio: si hay un nivel de confluencia (Imán Dorado)
    cerca, el precio tiende a ser atraído hacia él -- el signo depende
    de si ese nivel está arriba o abajo del precio actual.

    Devuelve (puntos_con_signo de -10 a +10, activo: bool, detalle: str)
    """
    peso_max = 10

    if not iman_dorado_activo:
        return 0, False, "Sin confluencia Imán Dorado activa este ciclo."

    precio_nivel = iman_dorado_activo["precio"]
    fuerza_nivel = iman_dorado_activo["fuerza"]  # 2 o 3 fuentes coincidiendo
    signo = 1 if precio_nivel > precio_actual else -1
    factor_fuerza = fuerza_nivel / 3.0  # 2 fuentes = 0.67, 3 fuentes = 1.0

    puntos = signo * peso_max * factor_fuerza
    detalle = f"Imán Dorado ({fuerza_nivel} fuentes) hacia ${precio_nivel:,.0f}."
    return puntos, True, detalle


def calcular_market_bias(
    precio_actual,
    hay_absorcion, detalle_absorcion, resultado_flip_local,
    buy_pressure, sell_pressure, estado_velocidad,
    funding_disponible, funding_valor, oi_disponible, cambio_oi,
    tendencia_1h,
    iman_dorado_activo,
):
    """
    Consolida las 5 familias en un vector único. No hace ninguna
    llamada a red ni recalcula nada -- todos los parámetros ya
    existen en el ciclo actual de main.py.

    Devuelve dict:
      {
        "bias": -100 a +100,
        "confianza": 0 a 100,
        "componentes": [(nombre, puntos, activo, detalle), ...],
        "lectura": str
      }
    """

    componentes = []

    p1, a1, d1 = _componente_absorcion_flip(hay_absorcion, detalle_absorcion, resultado_flip_local, precio_actual)
    componentes.append(("Absorción + Flip Local", p1, a1, d1))

    p2, a2, d2 = _componente_presion_velocidad(buy_pressure, sell_pressure, estado_velocidad)
    componentes.append(("Presión + Velocidad", p2, a2, d2))

    p3, a3, d3 = _componente_funding_oi(funding_disponible, funding_valor, oi_disponible, cambio_oi)
    componentes.append(("Funding + OI", p3, a3, d3))

    p4, a4, d4 = _componente_tendencia_1h(tendencia_1h)
    componentes.append(("Tendencia 1H", p4, a4, d4))

    p5, a5, d5 = _componente_iman_dorado(iman_dorado_activo, precio_actual)
    componentes.append(("Imán Dorado", p5, a5, d5))

    bias_bruto = sum(c[1] for c in componentes)
    bias = round(_clamp(bias_bruto))

    # Confianza: cuántas familias están activas (tienen dato) Y
    # cuántas de las activas coinciden en el signo del bias total.
    activos = [c for c in componentes if c[2]]
    if not activos:
        confianza = 0
    else:
        signo_bias = 1 if bias > 0 else (-1 if bias < 0 else 0)
        coincidentes = sum(1 for c in activos if (c[1] > 0) == (signo_bias > 0) and c[1] != 0)
        cobertura = len(activos) / 5.0
        acuerdo = coincidentes / len(activos) if activos else 0
        confianza = round(100 * cobertura * (0.4 + 0.6 * acuerdo))

    # Lectura textual, calibrada a los mismos umbrales que ya usás en
    # el resto del dashboard (Scalp Edge, etc.) para mantener
    # consistencia de vocabulario entre paneles.
    if abs(bias) < 15:
        lectura = f"⚪ Sesgo neutral ({bias:+d}) — señales sin acuerdo claro, confianza {confianza}%."
    elif abs(bias) < 40:
        direccion = "alcista" if bias > 0 else "bajista"
        lectura = f"🟡 Sesgo {direccion} leve ({bias:+d}) — confianza {confianza}%."
    elif abs(bias) < 70:
        direccion = "alcista" if bias > 0 else "bajista"
        lectura = f"🟢 Sesgo {direccion} moderado ({bias:+d})" if bias > 0 else f"🔴 Sesgo {direccion} moderado ({bias:+d})"
        lectura += f" — confianza {confianza}%."
    else:
        direccion = "alcista" if bias > 0 else "bajista"
        icono = "🟢🟢" if bias > 0 else "🔴🔴"
        lectura = f"{icono} Sesgo {direccion} fuerte ({bias:+d}) — {confianza}% de confianza, mayoría de familias alineadas."

    return {
        "bias": bias,
        "confianza": confianza,
        "componentes": componentes,
        "lectura": lectura,
    }
