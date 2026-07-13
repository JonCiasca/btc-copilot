"""
iv_structure.py
----------------
Volatilidad Implícita (IV): Skew y Term Structure, usando el mark_iv
real que Deribit ya entrega por instrumento (obtener_instrumentos_deribit
lo trae en cada ciclo -- este módulo no agrega NINGUNA llamada nueva a
red, solo lee el campo "iv" que ya viaja en cada instrumento).

POR QUÉ ES UN CONCEPTO DISTINTO A LO QUE YA CALCULA EL DASHBOARD:
Walls, Pinning y Flip miran DÓNDE está la posición cargada (tamaño de
OI por strike). La IV mira CUÁNTO está pagando el mercado por la
incertidumbre en cada strike/vencimiento -- es el precio del riesgo,
no el tamaño de la posición. Son dos dimensiones independientes de la
misma cadena de opciones.

DOS LECTURAS, cada una responde una pregunta distinta:

  1. SKEW (por vencimiento): compara la IV promedio de puts OTM contra
     la IV promedio de calls OTM (ponderado por OI, dentro de una banda
     de distancia al spot). Puts más caras que calls = el mercado paga
     más por protección a la baja (cobertura de caída priced-in).
     Calls más caras = sesgo hacia protección de subida (menos común
     en BTC, pero ocurre en rallies fuertes / FOMO).

  2. TERM STRUCTURE: compara la IV "at-the-money" del vencimiento más
     próximo (corto plazo) contra la de un vencimiento más lejano
     (mediano plazo, el mismo que usa el Flip Global). Corto plazo más
     caro que el lejano ("invertida"/backwardation) = el mercado está
     pricing un evento o riesgo cercano en el tiempo. Corto plazo más
     barato ("normal"/contango) = calma relativa, sin evento inminente
     priced todavía.

LÍMITE HONESTO: BTC options en Deribit no siempre tienen suficientes
strikes líquidos en cada banda de distancia -- cada resultado reporta
CUÁNTOS strikes (n) sostienen el promedio. Con n bajo (1-2 strikes) el
dato es real pero frágil; el módulo lo declara explícitamente en vez
de mostrar un número prolijo sin contexto de cuán sólido es.
"""


def _promedio_iv_ponderado(items):
    """
    items: lista de (strike, oi, iv). Devuelve (iv_promedio_ponderado, n)
    o (None, 0) si la lista está vacía o el OI total es cero.
    """
    if not items:
        return None, 0

    oi_total = sum(oi for _, oi, _ in items)
    if oi_total <= 0:
        return None, 0

    iv_ponderada = sum(iv * oi for _, oi, iv in items) / oi_total
    return iv_ponderada, len(items)


def _iv_atm(filtrados, precio_actual, banda_atm_pct=0.03):
    """
    IV "at-the-money": promedio ponderado por OI de todos los strikes
    (calls + puts) dentro de banda_atm_pct de distancia al spot. Si no
    hay ninguno tan cerca, cae al strike individual más próximo al
    spot (siempre hay uno, salvo lista vacía).

    Devuelve (iv, n_strikes, fue_fallback: bool)
    """
    if not filtrados:
        return None, 0, False

    cercanos = [
        (i["strike"], i["oi"], i["iv"]) for i in filtrados
        if abs(i["strike"] - precio_actual) / precio_actual <= banda_atm_pct
    ]

    iv, n = _promedio_iv_ponderado(cercanos)
    if iv is not None:
        return iv, n, False

    # Fallback: strike individual más cercano al spot, sin importar banda
    mas_cercano = min(filtrados, key=lambda i: abs(i["strike"] - precio_actual))
    return mas_cercano["iv"], 1, True


def _skew_otm(filtrados, precio_actual, banda_min_pct=0.03, banda_max_pct=0.15):
    """
    Compara IV promedio (ponderada por OI) de puts OTM (strike < spot)
    contra calls OTM (strike > spot), ambos dentro de la banda de
    distancia [banda_min_pct, banda_max_pct] del spot -- se excluye la
    zona ATM angosta a propósito, para no mezclar skew con el nivel
    base de IV que ya captura _iv_atm.

    Devuelve dict con skew (puts - calls, en puntos de vol decimales),
    iv_puts, n_puts, iv_calls, n_calls -- o None si falta algún lado.
    """
    if not filtrados:
        return None

    puts_otm = [
        (i["strike"], i["oi"], i["iv"]) for i in filtrados
        if i["tipo"] == "put"
        and banda_min_pct <= (precio_actual - i["strike"]) / precio_actual <= banda_max_pct
    ]
    calls_otm = [
        (i["strike"], i["oi"], i["iv"]) for i in filtrados
        if i["tipo"] == "call"
        and banda_min_pct <= (i["strike"] - precio_actual) / precio_actual <= banda_max_pct
    ]

    iv_puts, n_puts = _promedio_iv_ponderado(puts_otm)
    iv_calls, n_calls = _promedio_iv_ponderado(calls_otm)

    if iv_puts is None or iv_calls is None:
        return {
            "skew": None, "iv_puts": iv_puts, "n_puts": n_puts,
            "iv_calls": iv_calls, "n_calls": n_calls,
        }

    return {
        "skew": iv_puts - iv_calls,
        "iv_puts": iv_puts, "n_puts": n_puts,
        "iv_calls": iv_calls, "n_calls": n_calls,
    }


def _lectura_skew(resultado_skew, etiqueta_plazo, umbral_vol_pts=0.02):
    """
    Traduce un resultado de _skew_otm a texto, con el número real
    citado siempre -- nunca una frase sin el dato que la sostiene.
    umbral_vol_pts: diferencia mínima (en puntos de vol decimales,
    0.02 = 2 puntos de vol) para considerar el skew significativo en
    vez de ruido -- umbral de lectura, no una calibración externa.
    """
    if resultado_skew is None or resultado_skew["skew"] is None:
        faltante = []
        if resultado_skew is None or resultado_skew.get("n_puts", 0) == 0:
            faltante.append("puts OTM")
        if resultado_skew is None or resultado_skew.get("n_calls", 0) == 0:
            faltante.append("calls OTM")
        return f"Sin strikes suficientes en {etiqueta_plazo} ({', '.join(faltante) or 'ambos lados'})."

    skew = resultado_skew["skew"]
    n_puts, n_calls = resultado_skew["n_puts"], resultado_skew["n_calls"]
    fragilidad = " (muestra chica, leer con cautela)" if min(n_puts, n_calls) <= 2 else ""

    if skew >= umbral_vol_pts:
        return (
            f"Skew {etiqueta_plazo}: +{skew*100:.1f} pts de vol hacia puts "
            f"(puts {resultado_skew['iv_puts']*100:.1f}% IV / {n_puts} strikes vs "
            f"calls {resultado_skew['iv_calls']*100:.1f}% IV / {n_calls} strikes) — "
            f"protección a la baja más cara, sesgo bajista priced-in{fragilidad}."
        )
    elif skew <= -umbral_vol_pts:
        return (
            f"Skew {etiqueta_plazo}: {skew*100:.1f} pts de vol hacia calls "
            f"(calls {resultado_skew['iv_calls']*100:.1f}% IV / {n_calls} strikes vs "
            f"puts {resultado_skew['iv_puts']*100:.1f}% IV / {n_puts} strikes) — "
            f"protección a la suba más cara, sesgo de melt-up priced-in{fragilidad}."
        )
    else:
        return (
            f"Skew {etiqueta_plazo}: {skew*100:+.1f} pts de vol "
            f"(puts {resultado_skew['iv_puts']*100:.1f}% / {n_puts} strikes vs "
            f"calls {resultado_skew['iv_calls']*100:.1f}% / {n_calls} strikes) — "
            f"neutral, sin sesgo direccional claro en la cobertura{fragilidad}."
        )


def lectura_asociativa_iv(cambio_iv_corto_pct, gex_spot_global, iv_atm_corto, umbral_cambio_pct=3.0):
    """
    Traducción directa tipo "IV bajando = probable reacción": cruza la
    DIRECCIÓN del cambio de IV corto plazo (subiendo/bajando/estable,
    medido vs ~10 refreshes atrás) con el RÉGIMEN de Gamma (Flip
    Global) para dar una asociación de una línea, en el mismo espíritu
    que las lecturas "🟢 Compradores entrando" que ya usa el dashboard
    en Flow Intelligence.

    cambio_iv_corto_pct: % de cambio de iv_atm_corto vs la ventana de
    historial (lo calcula main.py con el mismo helper que ya usa para
    el cambio % de OI de las Walls -- ver _actualizar_y_calcular_cambio_oi).
    None si todavía no hay suficiente historial en la sesión.

    gex_spot_global: signo del GEX en spot del Flip Global (positivo =
    Long Gamma, negativo = Short Gamma). None si Deribit no respondió.

    Devuelve (icono_texto: str, detalle: str). El detalle siempre cita
    el número real (% de cambio) que sostiene la lectura.
    """

    if gex_spot_global is None:
        return "⚪ Sin régimen disponible", "Falta el Flip Global este ciclo para cruzar con la IV."

    if cambio_iv_corto_pct is None:
        return (
            "⚪ Sin historial suficiente",
            "Todavía no hay suficientes refreshes acumulados en la sesión para medir "
            "si la IV está subiendo o bajando (se necesita ~10 ciclos)."
        )

    long_gamma = gex_spot_global > 0

    if cambio_iv_corto_pct >= umbral_cambio_pct:
        direccion = "subiendo"
    elif cambio_iv_corto_pct <= -umbral_cambio_pct:
        direccion = "bajando"
    else:
        direccion = "estable"

    detalle_numero = f"IV corto plazo {iv_atm_corto*100:.1f}% ({cambio_iv_corto_pct:+.1f}% vs ventana previa)"

    if long_gamma and direccion == "bajando":
        return (
            "🔵🔵 Contención reforzada",
            f"{detalle_numero} — Long Gamma sosteniendo el rango, y encima el mercado "
            f"relaja la cobertura. Escenario de rango con mayor respaldo."
        )
    if long_gamma and direccion == "subiendo":
        return (
            "🟡 Contención bajo presión",
            f"{detalle_numero} — Long Gamma todavía sostiene, pero el mercado empieza a "
            f"pagar más por cobertura. Vigilar si el régimen vira a Short Gamma."
        )
    if long_gamma and direccion == "estable":
        return (
            "🔵 Contención sin cambios",
            f"{detalle_numero} — Long Gamma sin presión adicional de IV que la refuerce "
            f"ni la debilite."
        )
    if (not long_gamma) and direccion == "subiendo":
        return (
            "🔴🔴 Expansión reforzada",
            f"{detalle_numero} — Short Gamma amplificando, y el mercado pagando más por "
            f"el riesgo: mayor confianza para acompañar la fuerza del movimiento."
        )
    if (not long_gamma) and direccion == "bajando":
        return (
            "🟠 Probable reacción / falsa alarma",
            f"{detalle_numero} — el régimen amplifica, pero el mercado NO está pricing "
            f"más riesgo. El movimiento puede perder fuerza sin nueva demanda de cobertura."
        )
    # Short Gamma + IV estable
    return (
        "🟠 Momentum sin refuerzo de IV",
        f"{detalle_numero} — Short Gamma activo, pero sin cambio de IV que lo confirme "
        f"o lo contradiga. Leer con velocidad/presión, no con esto solo."
    )


def calcular_estructura_iv(
    instrumentos, precio_actual,
    vencimiento_corto, vencimiento_largo,
    banda_atm_pct=0.03, banda_otm_min_pct=0.03, banda_otm_max_pct=0.15,
    umbral_term_structure_pts=0.02,
):
    """
    Punto de entrada único. No hace ninguna llamada a red -- usa la
    lista de instrumentos que main.py YA descargó este ciclo
    (obtener_instrumentos_deribit).

    vencimiento_corto / vencimiento_largo: datetimes de vencimiento a
    comparar. En main.py, "corto" puede ser el mismo vencimiento que
    usa Gamma Pinning (el más próximo) y "largo" el último de los 5
    vencimientos semanales del Flip Global -- así no se inventa
    ningún criterio nuevo de selección de vencimientos, se reusa el
    que ya existe.

    Devuelve dict:
      {
        "iv_atm_corto": float o None, "iv_atm_largo": float o None,
        "term_structure": float o None,  # corto - largo, en puntos de vol decimales
        "term_structure_lectura": str,
        "skew_corto": dict (ver _skew_otm), "skew_corto_lectura": str,
        "skew_largo": dict (ver _skew_otm), "skew_largo_lectura": str,
        "vencimiento_corto": datetime, "vencimiento_largo": datetime,
      }
    """

    if not instrumentos or vencimiento_corto is None or vencimiento_largo is None:
        return {
            "iv_atm_corto": None, "iv_atm_largo": None,
            "term_structure": None,
            "term_structure_lectura": "Sin datos suficientes de Deribit para term structure este ciclo.",
            "skew_corto": None, "skew_corto_lectura": "Sin datos suficientes este ciclo.",
            "skew_largo": None, "skew_largo_lectura": "Sin datos suficientes este ciclo.",
            "vencimiento_corto": vencimiento_corto, "vencimiento_largo": vencimiento_largo,
        }

    filtrados_corto = [i for i in instrumentos if i["vencimiento"] == vencimiento_corto]
    filtrados_largo = [i for i in instrumentos if i["vencimiento"] == vencimiento_largo]

    iv_atm_corto, n_atm_corto, fb_corto = _iv_atm(filtrados_corto, precio_actual, banda_atm_pct)
    iv_atm_largo, n_atm_largo, fb_largo = _iv_atm(filtrados_largo, precio_actual, banda_atm_pct)

    if vencimiento_corto == vencimiento_largo:
        term_structure = None
        term_lectura = "Vencimiento corto y largo coinciden este ciclo — sin term structure que comparar."
    elif iv_atm_corto is None or iv_atm_largo is None:
        term_structure = None
        term_lectura = "Sin IV ATM suficiente en alguno de los dos vencimientos para comparar."
    else:
        term_structure = iv_atm_corto - iv_atm_largo
        nota_fb = " (fallback a strike más cercano, sin strikes en banda ATM)" if (fb_corto or fb_largo) else ""
        if term_structure >= umbral_term_structure_pts:
            term_lectura = (
                f"Term structure INVERTIDA: IV corto plazo {iv_atm_corto*100:.1f}% > "
                f"IV largo plazo {iv_atm_largo*100:.1f}% por {term_structure*100:.1f} pts de vol{nota_fb} — "
                f"el mercado de opciones está pricing riesgo/evento cercano en el tiempo."
            )
        elif term_structure <= -umbral_term_structure_pts:
            term_lectura = (
                f"Term structure NORMAL (contango): IV largo plazo {iv_atm_largo*100:.1f}% > "
                f"IV corto plazo {iv_atm_corto*100:.1f}% por {abs(term_structure)*100:.1f} pts de vol{nota_fb} — "
                f"calma relativa, sin evento inminente priced todavía."
            )
        else:
            term_lectura = (
                f"Term structure PLANA: corto {iv_atm_corto*100:.1f}% vs largo "
                f"{iv_atm_largo*100:.1f}% ({term_structure*100:+.1f} pts de vol){nota_fb} — "
                f"sin sesgo temporal claro."
            )

    skew_corto = _skew_otm(filtrados_corto, precio_actual, banda_otm_min_pct, banda_otm_max_pct)
    skew_largo = _skew_otm(filtrados_largo, precio_actual, banda_otm_min_pct, banda_otm_max_pct)

    skew_corto_lectura = _lectura_skew(skew_corto, "corto plazo")
    skew_largo_lectura = _lectura_skew(skew_largo, "mediano plazo")

    return {
        "iv_atm_corto": iv_atm_corto, "iv_atm_largo": iv_atm_largo,
        "term_structure": term_structure,
        "term_structure_lectura": term_lectura,
        "skew_corto": skew_corto, "skew_corto_lectura": skew_corto_lectura,
        "skew_largo": skew_largo, "skew_largo_lectura": skew_largo_lectura,
        "vencimiento_corto": vencimiento_corto, "vencimiento_largo": vencimiento_largo,
    }
