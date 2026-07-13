"""
market_projection.py
---------------------
Proyección probabilística de RÉGIMEN (-100 a +100): a diferencia de
market_bias.py (que responde "¿hacia qué lado se inclina el precio?"),
este módulo responde una pregunta distinta y complementaria — "¿cómo
se va a COMPORTAR el precio dentro de la estructura de opciones que
ya tenemos calculada: contenido en rango, o habilitado a expandirse?"

Objetivo (pedido del usuario): catalogar Call Wall, Put Wall, Gamma
Pinning y el régimen del Flip Global en un solo apartado de
"Proyección / Escenario de Mercado", cruzando esa estructura con las
señales que el dashboard YA calcula (Market Bias, Participantes de
Mercado) en vez de mirar cada pieza por separado. Cero llamadas
nuevas a red — pura consolidación de datos existentes.

CONVENCIÓN DE SIGNO: positivo = estructura favorece RANGO/CONTENCIÓN
(operar extremos, walls y pinning conteniendo el precio). Negativo =
estructura favorece RUPTURA/MOMENTUM (walls no contienen, conviene
acompañar la fuerza del movimiento en vez de buscar reversión). Al
igual que en market_bias, la magnitud NO es una probabilidad de que
"pase" — es cuántas familias independientes de señales apuntan al
mismo comportamiento y con qué fuerza relativa.

LAS 4 FAMILIAS (peso fijo, suman 100):
  1. Régimen Gamma (Flip Global — signo del GEX en spot)....... 35
  2. Posición del precio en el rango Put Wall–Call Wall........ 30
  3. Gamma Pinning (magnetismo simétrico hacia el strike)...... 20
  4. Confluencia con Market Bias + Participantes dominante..... 15

REGLA EXPLÍCITA DEL USUARIO (Short Gamma): en régimen Short Gamma NO
se busca reversión en los extremos — se lee como habilitación de
momentum, y el texto acompaña la fuerza del movimiento (velocidad +
presión taker), nunca sugiere "comprar la baja / vender la suba".

REGLA EXPLÍCITA DEL USUARIO (Pinning simétrico): el efecto magneto de
Gamma Pinning se trata IGUAL si el precio está arriba o abajo del
strike — la única asimetría real es cuánto falta para el vencimiento
(a más días, el efecto es marginal; esto ya está documentado en
calcular_gamma_pinning).

LÍMITE HONESTO: cada componente solo suma si hay DATO real detrás
(nunca un valor inventado por ausencia de información) y cada
"detalle" reporta el número que lo sostiene, no una frase vacía. Si
una familia no tiene datos ese ciclo, queda excluida del cálculo y
así se declara explícitamente.
"""


def _clamp(valor, minimo=-100, maximo=100):
    return max(minimo, min(maximo, valor))


def _componente_regimen_gamma(resultado_flip_global):
    """
    Régimen del Flip Global: Long Gamma (dealers compran caídas /
    venden subas -> comprime el rango -> favorece CONTENCIÓN) o Short
    Gamma (dealers hacen lo contrario -> amplifica el movimiento ->
    favorece RUPTURA/MOMENTUM). Usa el mismo criterio de signo que ya
    se muestra en el panel de Flip Semanal (gex_spot > 0 = Long Gamma).

    Devuelve (puntos de -35 a +35, activo: bool, detalle: str)
    """
    peso_max = 35

    if not resultado_flip_global or resultado_flip_global.get("gex_spot") is None:
        return 0, False, "Sin Flip Global disponible este ciclo."

    gex_spot = resultado_flip_global["gex_spot"]

    if gex_spot > 0:
        detalle = (
            f"Long Gamma (GEX spot {gex_spot:,.0f} > 0) — dealers amortiguan "
            f"el movimiento, estructura favorece rango."
        )
        return peso_max, True, detalle
    else:
        detalle = (
            f"Short Gamma (GEX spot {gex_spot:,.0f} < 0) — dealers amplifican "
            f"el movimiento, estructura favorece momentum/ruptura."
        )
        return -peso_max, True, detalle


def _componente_posicion_walls(precio_actual, call_wall, put_wall, margen_decision_pct=0.3):
    """
    Ubica el precio actual dentro (o fuera) del rango Put Wall–Call
    Wall. Contenido = evidencia de rango. Por fuera de cualquiera de
    los dos extremos = evidencia de ruptura ya en curso (el wall dejó
    de contener). Muy cerca de un extremo (dentro de
    margen_decision_pct) = zona de decisión, no se cuenta a favor de
    ningún lado todavía porque el desenlace no está definido.

    Devuelve (puntos de -30 a +30, activo: bool, detalle: str)
    """
    peso_max = 30

    if not call_wall or not put_wall:
        return 0, False, "Sin Call Wall y/o Put Wall disponibles este ciclo."

    techo = call_wall["strike"]
    piso = put_wall["strike"]

    if techo <= piso:
        return 0, False, "Call Wall y Put Wall invertidos/superpuestos — sin rango válido."

    dist_al_techo_pct = (techo - precio_actual) / precio_actual * 100
    dist_al_piso_pct = (precio_actual - piso) / precio_actual * 100

    if precio_actual > techo:
        exceso = (precio_actual - techo) / techo * 100
        detalle = (
            f"Precio ${precio_actual:,.0f} ya superó la Call Wall (${techo:,.0f}) "
            f"por {exceso:.2f}% — resistencia no está conteniendo."
        )
        return -peso_max, True, detalle

    if precio_actual < piso:
        exceso = (piso - precio_actual) / piso * 100
        detalle = (
            f"Precio ${precio_actual:,.0f} ya perdió la Put Wall (${piso:,.0f}) "
            f"por {exceso:.2f}% — soporte no está conteniendo."
        )
        return -peso_max, True, detalle

    if dist_al_techo_pct < margen_decision_pct or dist_al_piso_pct < margen_decision_pct:
        lado = "Call Wall" if dist_al_techo_pct < dist_al_piso_pct else "Put Wall"
        detalle = (
            f"Precio a menos de {margen_decision_pct:.1f}% de la {lado} — zona de "
            f"decisión, todavía sin confirmar contención ni ruptura."
        )
        return 0, True, detalle

    detalle = (
        f"Precio ${precio_actual:,.0f} dentro del rango Put Wall (${piso:,.0f}) – "
        f"Call Wall (${techo:,.0f}), a {dist_al_piso_pct:.2f}% del piso y "
        f"{dist_al_techo_pct:.2f}% del techo."
    )
    return peso_max, True, detalle


def _componente_gamma_pinning(precio_actual, resultado_gamma_pinning, ahora, dias_relevancia=3.0):
    """
    Efecto magneto del Gamma Pinning, tratado SIMÉTRICO: da lo mismo
    si el strike de pinning está arriba o abajo del precio actual —
    en ambos casos el hedging de dealers tiende a acercar el precio
    hacia ahí a medida que se acerca el vencimiento. La única
    asimetría real y documentada es el TIEMPO a vencimiento: si faltan
    varios días el efecto es marginal (ver docstring de
    calcular_gamma_pinning en main.py).

    Siempre suma hacia CONTENCIÓN (nunca hacia ruptura) porque, por
    definición, el pinning es una fuerza de convergencia — pero su
    peso se escala por cuán cerca está el vencimiento.

    Devuelve (puntos de 0 a +20, activo: bool, detalle: str)
    """
    peso_max = 20

    if not resultado_gamma_pinning or resultado_gamma_pinning.get("strike_pin") is None:
        return 0, False, "Sin Gamma Pinning disponible este ciclo."

    strike_pin = resultado_gamma_pinning["strike_pin"]
    vencimiento_usado = resultado_gamma_pinning.get("vencimiento_usado")

    if vencimiento_usado is None:
        return 0, False, "Gamma Pinning sin vencimiento asociado este ciclo."

    dias_a_vencimiento = (vencimiento_usado - ahora).total_seconds() / 86400.0
    dias_a_vencimiento = max(dias_a_vencimiento, 0.0)

    dist_pct = (strike_pin - precio_actual) / precio_actual * 100
    lado = "por encima" if dist_pct > 0 else ("por debajo" if dist_pct < 0 else "sobre el precio actual")

    factor_tiempo = max(0.0, 1.0 - (dias_a_vencimiento / dias_relevancia))
    factor_tiempo = min(factor_tiempo, 1.0)

    if factor_tiempo <= 0:
        detalle = (
            f"Pin en ${strike_pin:,.0f} ({dist_pct:+.2f}%, {lado}) pero a "
            f"{dias_a_vencimiento:.1f} días de vencer — efecto todavía marginal."
        )
        return 0, True, detalle

    puntos = peso_max * factor_tiempo
    detalle = (
        f"Pin en ${strike_pin:,.0f} ({dist_pct:+.2f}%, {lado}), vence en "
        f"{dias_a_vencimiento:.1f} días — magnetismo simétrico hacia ese strike "
        f"a medida que se acerca el vencimiento."
    )
    return puntos, True, detalle


def _componente_confluencia_bias_participantes(resultado_bias, pct_mm, pct_retail):
    """
    Cruza el Market Bias (dirección/fuerza) y el panel de Participantes
    de Mercado (quién domina el flujo) con la lectura de régimen:
      - Bias fuerte + Retail dominante -> refuerza RUPTURA (el impulso
        direccional no tiene freno de dealers defendiendo nivel).
      - Bias débil/neutral + Market Maker dominante -> refuerza
        CONTENCIÓN (flujo sin dirección clara, dealers defendiendo).
      - Bias fuerte + Market Maker dominante -> señal mixta/débil (el
        dealer todavía defiende pese al impulso), se pondera bajo.

    Devuelve (puntos de -15 a +15, activo: bool, detalle: str)
    """
    peso_max = 15

    if resultado_bias is None or pct_mm is None or pct_retail is None:
        return 0, False, "Sin Market Bias y/o Participantes disponibles este ciclo."

    bias = resultado_bias["bias"]
    confianza_bias = resultado_bias["confianza"]
    bias_fuerte = abs(bias) >= 40 and confianza_bias >= 50

    if bias_fuerte and pct_retail > pct_mm:
        detalle = (
            f"Bias {bias:+d} (confianza {confianza_bias}%) con Retail dominante "
            f"({pct_retail}% vs {pct_mm}% MM) — impulso sin freno de dealer."
        )
        return -peso_max, True, detalle

    if not bias_fuerte and pct_mm > pct_retail:
        detalle = (
            f"Bias {bias:+d} (confianza {confianza_bias}%) débil, con Market Maker "
            f"dominante ({pct_mm}% vs {pct_retail}% Retail) — flujo sin dirección, "
            f"dealer defendiendo."
        )
        return peso_max, True, detalle

    if bias_fuerte and pct_mm >= pct_retail:
        detalle = (
            f"Bias {bias:+d} (confianza {confianza_bias}%) fuerte pero Market Maker "
            f"todavía domina ({pct_mm}% vs {pct_retail}% Retail) — señal mixta, "
            f"dealer resiste el impulso."
        )
        return -peso_max * 0.3, True, detalle

    detalle = (
        f"Bias {bias:+d} (confianza {confianza_bias}%), Retail {pct_retail}% / "
        f"MM {pct_mm}% — sin combinación clara a favor de ningún escenario."
    )
    return 0, True, detalle


def calcular_proyeccion_mercado(
    precio_actual,
    resultado_flip_global,
    call_wall, put_wall,
    resultado_gamma_pinning, ahora,
    resultado_bias, pct_mm, pct_retail,
    estado_velocidad, buy_pressure, sell_pressure,
):
    """
    Consolida las 4 familias en un vector único de RÉGIMEN (rango vs
    ruptura). No hace ninguna llamada a red ni recalcula nada -- todos
    los parámetros ya existen en el ciclo actual de main.py.

    Devuelve dict:
      {
        "score": -100 a +100,
        "confianza": 0 a 100,
        "escenario": "contencion" | "ruptura" | "transicion",
        "componentes": [(nombre, puntos, activo, detalle), ...],
        "lectura": str,
        "guia_operativa": str,
      }
    """

    componentes = []

    p1, a1, d1 = _componente_regimen_gamma(resultado_flip_global)
    componentes.append(("Régimen Gamma (Flip Global)", p1, a1, d1))

    p2, a2, d2 = _componente_posicion_walls(precio_actual, call_wall, put_wall)
    componentes.append(("Posición en rango Walls", p2, a2, d2))

    p3, a3, d3 = _componente_gamma_pinning(precio_actual, resultado_gamma_pinning, ahora)
    componentes.append(("Gamma Pinning (magnetismo)", p3, a3, d3))

    p4, a4, d4 = _componente_confluencia_bias_participantes(resultado_bias, pct_mm, pct_retail)
    componentes.append(("Confluencia Bias + Participantes", p4, a4, d4))

    score_bruto = sum(c[1] for c in componentes)
    score = round(_clamp(score_bruto))

    activos = [c for c in componentes if c[2]]
    if not activos:
        confianza = 0
    else:
        signo_score = 1 if score > 0 else (-1 if score < 0 else 0)
        coincidentes = sum(1 for c in activos if (c[1] > 0) == (signo_score > 0) and c[1] != 0)
        cobertura = len(activos) / 4.0
        acuerdo = coincidentes / len(activos) if activos else 0
        confianza = round(100 * cobertura * (0.4 + 0.6 * acuerdo))

    if score >= 15:
        escenario = "contencion"
    elif score <= -15:
        escenario = "ruptura"
    else:
        escenario = "transicion"

    # Guía operativa: separada de la "lectura" porque acá se traduce
    # el escenario a comportamiento esperado, no solo al número. Es
    # donde se aplica explícitamente la regla de Short Gamma (acompañar
    # la fuerza del movimiento, no buscar reversión).
    if escenario == "contencion":
        lectura = (
            f"🔵 Escenario de RANGO / CONTENCIÓN ({score:+d}) — confianza {confianza}%. "
            f"Walls y/o Pinning conteniendo el precio, régimen favorece operar extremos."
        )
        guia_operativa = (
            "Estructura sugiere rango: los extremos (Walls) tienden a contener, y el "
            "Gamma Pinning (si está activo) actúa como imán hacia el centro a medida "
            "que se acerca el vencimiento. Puede evaluarse operar hacia los extremos "
            "del rango, con especial cuidado si el precio se acerca al strike de "
            "pinning — si lo pierde con fuerza y los dealers no logran contener, el "
            "régimen puede virar a Short Gamma y el rango dejar de ser válido."
        )
    elif escenario == "ruptura":
        iconos_velocidad = {
            "acelerando": "acelerando",
            "desacelerando": "desacelerando",
            "estable": "estable",
            "lateral": "estable",
        }
        vel_txt = iconos_velocidad.get(estado_velocidad, estado_velocidad)
        lado_presion = "compradora" if buy_pressure > sell_pressure else "vendedora"
        lectura = (
            f"🟠 Escenario de RUPTURA / MOMENTUM ({score:+d}) — confianza {confianza}%. "
            f"Walls no están conteniendo y/o régimen Short Gamma amplificando el movimiento."
        )
        guia_operativa = (
            f"Estructura NO favorece buscar reversión en extremos — en este régimen los "
            f"dealers amplifican el movimiento en vez de amortiguarlo. Presión "
            f"{lado_presion} con velocidad {vel_txt}: la lectura correcta acá es "
            f"acompañar la fuerza del movimiento, no anticipar rebote en Walls ni "
            f"Pinning. Los niveles pasan a leerse como referencia de aceleración "
            f"(si se pierden con volumen) más que como soporte/resistencia clásicos."
        )
    else:
        lectura = (
            f"⚪ Escenario en TRANSICIÓN ({score:+d}) — confianza {confianza}%. "
            f"Señales sin mayoría clara entre contención y ruptura."
        )
        guia_operativa = (
            "Sin mayoría clara: alguna(s) familia(s) sin dato este ciclo, o señales "
            "repartidas entre contención y ruptura. Conviene esperar más confirmación "
            "(ej. reacción real del precio en Wall o Pin) antes de operar el escenario "
            "como si estuviera definido."
        )

    return {
        "score": score,
        "confianza": confianza,
        "escenario": escenario,
        "componentes": componentes,
        "lectura": lectura,
        "guia_operativa": guia_operativa,
    }
