from streamlit_autorefresh import st_autorefresh
import streamlit as st
import requests
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import math
import json
import os
from datetime import datetime, timezone

# ----------------------------------
# CONFIG
# ----------------------------------

st.set_page_config(
    page_title="BTC Copilot",
    page_icon="📈",
    layout="wide"
)

st_autorefresh(interval=15000, key="btc_refresh")

# ----------------------------------
# CONTADOR DE SESIONES (visible solo en panel de admin oculto)
# ----------------------------------
#
# Cuenta cuántas veces se abrió la app en total (sesiones nuevas, no
# refreshes de 15s dentro de la misma sesión). Se guarda en un archivo
# JSON simple en el mismo directorio del proyecto.
#
# LIMITACIÓN HONESTA: en el plan gratuito de Streamlit Community Cloud
# este archivo puede resetearse a cero en un redeploy, o si la app se
# duerme por inactividad y el entorno se reinicia. Para testing con un
# grupo chico es suficiente; si más adelante necesitás un conteo
# realmente persistente a largo plazo, hay que migrar esto a una base
# de datos externa (ej. Google Sheets como planilla-base, Supabase,
# etc.) en vez de un archivo local.

RUTA_CONTADOR = "contador_sesiones.json"


def _leer_contador():
    if os.path.exists(RUTA_CONTADOR):
        try:
            with open(RUTA_CONTADOR, "r") as f:
                return json.load(f).get("total_sesiones", 0)
        except Exception:
            return 0
    return 0


def _incrementar_contador():
    total = _leer_contador() + 1
    try:
        with open(RUTA_CONTADOR, "w") as f:
            json.dump({"total_sesiones": total}, f)
    except Exception:
        pass  # si falla escribir (ej. filesystem read-only), no rompemos la app
    return total


# "session_state" vive por sesión de navegador: si esta clave no existe
# todavía, es la PRIMERA carga de esta sesión (no un refresh de los
# 15s, que no recrea session_state). Así evitamos contar de más.
if "sesion_contada" not in st.session_state:
    st.session_state.sesion_contada = True
    _incrementar_contador()

# ----------------------------------
# PANEL DE ADMIN OCULTO
# ----------------------------------
# Solo aparece si la URL incluye el parámetro correcto, ej:
#   https://tu-app.streamlit.app/?admin=tuClaveSecreta
# Cambiá CLAVE_ADMIN por algo propio antes de publicar el sitio.
# Nadie sin ese parámetro exacto en la URL ve este panel.

CLAVE_ADMIN = "flowmdq2026"  # 🔑 CAMBIAR antes de publicar

parametros_url = st.query_params
es_admin = parametros_url.get("admin") == CLAVE_ADMIN

if es_admin:
    with st.expander("🔐 Panel de admin (solo visible con clave)", expanded=True):
        st.metric("Sesiones abiertas (histórico acumulado)", _leer_contador())
        st.caption(
            "Cuenta sesiones nuevas, no refreshes de 15s. Puede resetearse en un "
            "redeploy del plan gratuito de Streamlit Cloud — no es un dato 100% "
            "persistente a largo plazo, sirve como referencia para este testing."
        )

st.title("📈 BTC Copilot")

# ----------------------------------
# MODO OPERATIVO
# ----------------------------------

if "modo" not in st.session_state:
    st.session_state.modo = "Normal"

if "timeframe" not in st.session_state:
    st.session_state.timeframe = "15m"

if "oi_historial" not in st.session_state:
    st.session_state.oi_historial = []


c_normal, c_scalp = st.columns(2)

with c_normal:
    if st.button(
        "🧠 Normal",
        help=(
            "Pensado para operativa intradiaria/swing. Usa temporalidades de "
            "5M, 15M y 1H tal cual, y el Flip de gamma toma como referencia el "
            "Flip Semanal (Global). Prioriza estructura, tendencia y "
            "participación por sobre el microflujo de las últimas velas."
        ),
    ):
        st.session_state.modo = "Normal"
        st.rerun()

with c_scalp:
    if st.button(
        "⚡ Scalp",
        help=(
            "Pensado para operativa de muy corto plazo. Las mismas temporalidades "
            "(5M/15M/1H) se traducen a velas más chicas (1M/3M/5M) para reaccionar "
            "más rápido. Prioriza microflujo, presión inmediata y reacción del "
            "precio sobre niveles cercanos — usá el Flip Cercano (Local) y la "
            "capa ABSORB del gráfico como referencia principal en este modo."
        ),
    ):
        st.session_state.modo = "Scalp"
        st.rerun()


modo = st.session_state.modo
timeframe = st.session_state.timeframe

# -----------------------------
# CABINA MODO ACTIVO
# -----------------------------

if modo == "Scalp":

    if timeframe == "5m":
        temporalidad_analizada = "⚡1M"
        data_timeframe = "1m"

    elif timeframe == "15m":
        temporalidad_analizada = "⚡3M"
        data_timeframe = "3m"

    elif timeframe == "1h":
        temporalidad_analizada = "⚡5M"
        data_timeframe = "5m"

else:
    temporalidad_analizada = timeframe.upper()
    data_timeframe = timeframe


st.info(
    f"""
        🧠 Modo activo: {modo}     /      📊 Temporalidad analizada: {temporalidad_analizada}
"""
)

# ----------------------------------
# FUNCIONES
# ----------------------------------

def _get_via_proxy(url_binance, timeout=10):
    """
    Hace un GET a una URL de Binance PASANDO POR un proxy CORS gratuito
    (allorigins.win), en vez de pegarle directo.

    Por qué hace falta esto: Binance bloquea explícitamente el acceso
    a su API pública desde la infraestructura cloud donde corre
    Streamlit Community Cloud (ver 'b. Eligibility' en
    binance.com/en/terms — no es un bloqueo técnico, es una decisión
    de Binance basada en el origen del pedido). El proxy reenvía el
    pedido desde SU propia IP, que no está en esa lista de bloqueo, y
    nos devuelve la respuesta de Binance intacta.

    Riesgo conocido y aceptado: allorigins.win es un servicio gratuito
    de terceros, no de Binance ni de Anthropic. Puede caerse, cambiar
    sus límites, o desaparecer sin aviso. Si en el futuro esto empieza
    a fallar seguido, la alternativa más robusta es armar un proxy
    propio en un servicio cloud (Render, Railway, Cloudflare Workers).

    Devuelve el JSON ya parseado, o lanza la excepción para que cada
    función que llama a esto decida cómo manejarla (ya tienen sus
    propios try/except).
    """

    # URL-encode de la URL anidada: sin esto, los "&" y "?" de los
    # parámetros de Binance (ej. "?symbol=BTCUSDT&interval=5m") podrían
    # interpretarse como parte de los parámetros del PROXY en vez de
    # como parte de la URL que el proxy tiene que reenviar.
    url_binance_encoded = requests.utils.quote(url_binance, safe="")
    url_proxy = f"https://api.allorigins.win/raw?url={url_binance_encoded}"
    respuesta = requests.get(url_proxy, timeout=timeout)
    return respuesta.json()


def obtener_ticker():
    """
    Vuelve a Binance (vía proxy CORS, ver _get_via_proxy) en vez de
    Bybit: Binance daba el desglose real de compra/venta en klines
    (taker_buy_base), que Bybit no expone — esa pérdida de precisión
    en el panel de Presión fue el motivo de volver atrás.
    """

    try:
        cuerpo = _get_via_proxy(
            "https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT"
        )

        if not isinstance(cuerpo, dict) or "lastPrice" not in cuerpo:
            msg = cuerpo.get("msg", str(cuerpo)) if isinstance(cuerpo, dict) else "Respuesta inesperada del proxy"
            return {"error": msg}

        return cuerpo

    except Exception as e:
        return {"error": str(e)}



def obtener_velas(intervalo, limite=100):
    """
    Vuelve a Binance (vía proxy CORS, ver _get_via_proxy), que sí da
    el desglose real de compra/venta dentro de cada vela
    (taker_buy_base), necesario para el panel de Presión real.

    Devuelve un DataFrame con la estructura esperada, o un DataFrame
    VACÍO (mismas columnas, 0 filas) si el pedido falla — nunca lanza
    una excepción hacia afuera, para que el resto del dashboard pueda
    mostrar un aviso claro en vez de un traceback ilegible.
    """

    columnas = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ]

    url_binance = (
        f"https://api.binance.com/api/v3/klines"
        f"?symbol=BTCUSDT&interval={intervalo}&limit={limite}"
    )

    try:
        cuerpo = _get_via_proxy(url_binance)

        # Binance devuelve un dict con "code"/"msg" cuando hay un
        # error, en vez de la lista de velas esperada.
        if isinstance(cuerpo, dict):
            st.session_state["error_binance_velas"] = cuerpo.get("msg", str(cuerpo))
            return pd.DataFrame(columns=columnas)

        if not cuerpo:  # lista vacía
            st.session_state["error_binance_velas"] = "Respuesta vacía del proxy/Binance"
            return pd.DataFrame(columns=columnas)

        datos = cuerpo

    except Exception as e:
        st.session_state["error_binance_velas"] = str(e)
        return pd.DataFrame(columns=columnas)

    df = pd.DataFrame(datos, columns=columnas)

    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")

    for col in [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "taker_buy_base"
    ]:
        df[col] = df[col].astype(float)

    return df


def obtener_tendencia_desde_df(df):
    """Calcula tendencia a partir de un df de velas ya obtenido (evita refetch)."""

    if df is None or df.empty:
        return "⚪ Sin datos"

    sma20 = df["close"].tail(20).mean()
    ultimo = df["close"].iloc[-1]

    if ultimo > sma20:
        return "🟢 Alcista"
    elif ultimo < sma20:
        return "🔴 Bajista"
    else:
        return "🟡 Neutral"


def obtener_tendencia(intervalo):
    """Wrapper que sí pega a la API. Usar solo cuando no haya un df ya cacheado."""

    df = obtener_velas(intervalo, 50)
    return obtener_tendencia_desde_df(df)


def obtener_funding():
    """
    Vuelve a Binance Futures (vía proxy CORS, ver _get_via_proxy).
    """

    try:
        cuerpo = _get_via_proxy(
            "https://fapi.binance.com/fapi/v1/premiumIndex?symbol=BTCUSDT"
        )
        return float(cuerpo["lastFundingRate"]) * 100

    except Exception:
        return None


def obtener_open_interest():
    """
    Vuelve a Binance Futures (vía proxy CORS, ver _get_via_proxy).
    """

    try:
        cuerpo = _get_via_proxy(
            "https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT"
        )
        return float(cuerpo["openInterest"])

    except Exception:
        return None


# ----------------------------------
# GAMMA EXPOSURE REAL (Deribit + Black-Scholes)
# ----------------------------------
#
# Reemplaza el proxy anterior (velas) por un cálculo real de GEX
# (Gamma Exposure) usando instrumentos de Deribit + Black-Scholes.
#
# Metodología (estándar de mercado, no un hecho verificable de quién
# está posicionado cómo, sino una CONVENCIÓN ampliamente usada):
#   - Se asume que los dealers/market makers están, en neto,
#     LONG calls y SHORT puts (al revés de lo que retail suele tener).
#   - GEX de un strike = gamma_opcion * OI * tamaño_contrato * spot^2 * 0.01
#     (la convención de "x spot^2 x 0.01" expresa el GEX en $ por cada
#     1% de movimiento del spot, que es la lectura típica en este tipo
#     de dashboards).
#   - Calls aportan GEX positivo, puts aportan GEX negativo.
#   - El "Gamma Flip Point" es el precio hipotético del spot donde el
#     GEX TOTAL acumulado (sumando todos los strikes, recalculando la
#     gamma de cada uno para ESE precio hipotético) cruza de negativo
#     a positivo.
#
# Limitaciones honestas:
#   - Asume tasa libre de riesgo ≈ 0 (estándar para crypto perpetuo/cripto opciones).
#   - Usa la IV que reporta Deribit por instrumento (mark_iv), no una
#     superficie de vol recalibrada.
#   - No filtra por liquidez del instrumento (toma todo el OI reportado).
#   - Es un proxy de gamma de DEALER, no un dato confirmado de posicionamiento real.

CONTRATO_BTC = 1.0  # En Deribit, 1 contrato de opción BTC = 1 BTC nominal


def _norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def _gamma_black_scholes(spot, strike, vol_anual, dias_a_vencimiento, tasa=0.0):
    """
    Gamma de Black-Scholes (igual para call y put al mismo strike/vencimiento).
    spot, strike: precio en USD
    vol_anual: volatilidad implícita anualizada (ej. 0.55 = 55%)
    dias_a_vencimiento: días calendario restantes
    """

    if dias_a_vencimiento <= 0 or vol_anual <= 0 or spot <= 0 or strike <= 0:
        return 0.0

    t = dias_a_vencimiento / 365.0

    try:
        d1 = (
            math.log(spot / strike) + (tasa + 0.5 * vol_anual ** 2) * t
        ) / (vol_anual * math.sqrt(t))
    except (ValueError, ZeroDivisionError):
        return 0.0

    gamma = _norm_pdf(d1) / (spot * vol_anual * math.sqrt(t))
    return gamma


def obtener_instrumentos_deribit():
    """
    Descarga la lista de opciones BTC activas en Deribit junto con su
    Open Interest y volatilidad implícita (mark_iv) por instrumento.

    Devuelve una lista de dicts: strike, tipo (call/put), oi, iv, vencimiento (timestamp ms)
    o None si falla la conexión.
    """

    try:
        url_resumen = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option"
        resumen = requests.get(url_resumen, timeout=8).json()["result"]

        instrumentos = []

        for item in resumen:
            nombre = item.get("instrument_name", "")
            # Formato típico: BTC-27JUN26-65000-C
            partes = nombre.split("-")
            if len(partes) != 4:
                continue

            _, vencimiento_str, strike_str, tipo_letra = partes

            try:
                strike = float(strike_str)
            except ValueError:
                continue

            oi = item.get("open_interest", 0) or 0
            iv = item.get("mark_iv", None)

            if iv is None or oi <= 0:
                continue

            try:
                fecha_venc = datetime.strptime(vencimiento_str, "%d%b%y").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                continue

            instrumentos.append({
                "strike": strike,
                "tipo": "call" if tipo_letra == "C" else "put",
                "oi": float(oi),
                "iv": float(iv) / 100.0,  # Deribit lo da en %, lo pasamos a decimal
                "vencimiento": fecha_venc,
            })

        return instrumentos

    except Exception:
        return None


def calcular_gex_en_spot(instrumentos, spot_hipotetico, ahora, ponderar_por_tiempo=False):
    """
    Versión escalar (un solo precio), mantenida por compatibilidad y
    para casos puntuales. El cálculo de la grilla completa (60 precios)
    usa la versión vectorizada _calcular_curva_gex_vectorizada, mucho
    más rápida — ver esa función para el cálculo real que corre en
    cada refresh del dashboard.
    """

    gex_total = 0.0

    for inst in instrumentos:

        dias = (inst["vencimiento"] - ahora).total_seconds() / 86400.0
        if dias <= 0:
            continue

        gamma = _gamma_black_scholes(
            spot=spot_hipotetico,
            strike=inst["strike"],
            vol_anual=inst["iv"],
            dias_a_vencimiento=dias,
        )

        gex_strike = gamma * inst["oi"] * CONTRATO_BTC * (spot_hipotetico ** 2) * 0.01

        if ponderar_por_tiempo:
            gex_strike = gex_strike / math.sqrt(dias)

        if inst["tipo"] == "call":
            gex_total += gex_strike
        else:
            gex_total -= gex_strike

    return gex_total


def _gamma_black_scholes_vectorizada(spot_grilla, strikes, vol_anual, dias, tasa=0.0):
    """
    Versión vectorizada de _gamma_black_scholes: calcula la gamma de
    TODOS los instrumentos para TODOS los precios de la grilla en una
    sola operación matricial, en vez de un loop anidado precio x
    instrumento.

    spot_grilla: array 1D de shape (P,) — los P precios hipotéticos.
    strikes, vol_anual, dias: arrays 1D de shape (N,) — uno por
    instrumento (N instrumentos).

    Devuelve: matriz de shape (P, N) con la gamma de cada instrumento
    en cada precio de grilla.

    Por qué esto importa para performance: con ~60 precios de grilla
    y varios cientos de instrumentos (Deribit suele tener 300-600
    opciones de BTC activas), la versión con loops de Python hace
    decenas de miles de evaluaciones de Black-Scholes por refresh,
    DOS veces (Flip Local y Flip Global) cada 15 segundos. Con NumPy,
    la misma cuenta es un puñado de operaciones vectoriales sobre
    arrays, sin loops de Python en el camino caliente.
    """

    # Broadcasting: spot_grilla como columna (P,1), strikes/vol/dias
    # como fila (1,N) -> resultado (P,N)
    spot = spot_grilla[:, None]          # (P, 1)
    K = strikes[None, :]                  # (1, N)
    vol = vol_anual[None, :]              # (1, N)
    t = (dias[None, :]) / 365.0           # (1, N)

    # Evitamos división por cero / log de negativos con un mínimo seguro;
    # esos casos ya se filtran antes de llegar acá (dias > 0, vol > 0).
    sqrt_t = np.sqrt(t)
    d1 = (np.log(spot / K) + (tasa + 0.5 * vol ** 2) * t) / (vol * sqrt_t)
    norm_pdf_d1 = np.exp(-0.5 * d1 ** 2) / np.sqrt(2 * np.pi)
    gamma = norm_pdf_d1 / (spot * vol * sqrt_t)

    return gamma  # shape (P, N)


def _calcular_curva_gex_vectorizada(instrumentos, spot_actual, ahora, rango_pct=0.15, pasos=61, ponderar_por_tiempo=False):
    """
    Calcula la curva completa de GEX (gex por cada precio de la
    grilla) y el GEX al spot actual, todo en una sola pasada
    vectorizada con NumPy. Reemplaza el loop "por cada precio, por
    cada instrumento" por operaciones matriciales.

    Devuelve (precios_grilla: np.array, gex_por_precio: np.array, gex_spot: float)
    o (None, None, None) si no hay instrumentos vigentes (todos vencidos
    o sin datos válidos).
    """

    if not instrumentos:
        return None, None, None

    # Armamos arrays con los datos de cada instrumento, descartando
    # los que ya vencieron (dias <= 0).
    strikes = []
    vols = []
    dias_list = []
    signos = []  # +1 para call, -1 para put
    ois = []

    for inst in instrumentos:
        dias = (inst["vencimiento"] - ahora).total_seconds() / 86400.0
        if dias <= 0:
            continue
        strikes.append(inst["strike"])
        vols.append(inst["iv"])
        dias_list.append(dias)
        ois.append(inst["oi"])
        signos.append(1.0 if inst["tipo"] == "call" else -1.0)

    if not strikes:
        return None, None, None

    strikes = np.array(strikes, dtype=np.float64)
    vols = np.array(vols, dtype=np.float64)
    dias_arr = np.array(dias_list, dtype=np.float64)
    ois = np.array(ois, dtype=np.float64)
    signos = np.array(signos, dtype=np.float64)

    # Grilla de precios hipotéticos, incluyendo el spot actual al final
    # para reusar la misma matriz y sacar gex_spot sin un cálculo aparte.
    precio_min = spot_actual * (1 - rango_pct)
    precio_max = spot_actual * (1 + rango_pct)
    precios_grilla = np.linspace(precio_min, precio_max, pasos)
    precios_completos = np.append(precios_grilla, spot_actual)  # (P+1,)

    gammas = _gamma_black_scholes_vectorizada(precios_completos, strikes, vols, dias_arr)  # (P+1, N)

    # GEX por instrumento y precio: gamma * OI * 1 (contrato BTC) * spot^2 * 0.01
    spot_col = precios_completos[:, None]  # (P+1, 1)
    gex_matriz = gammas * ois[None, :] * CONTRATO_BTC * (spot_col ** 2) * 0.01  # (P+1, N)

    if ponderar_por_tiempo:
        # Pondera cada instrumento por 1/sqrt(dias) para que los
        # vencimientos lejanos (gamma baja, OI a veces gigante) no
        # ahoguen el cálculo del Flip Global con peso desproporcionado.
        gex_matriz = gex_matriz / np.sqrt(dias_arr[None, :])

    gex_matriz = gex_matriz * signos[None, :]  # calls suman, puts restan

    gex_total_por_precio = gex_matriz.sum(axis=1)  # (P+1,)

    gex_por_precio = gex_total_por_precio[:-1]  # los primeros P son la grilla
    gex_spot = float(gex_total_por_precio[-1])  # el último es el spot actual

    return precios_grilla, gex_por_precio, gex_spot


def calcular_gamma_exposure(instrumentos, spot_actual, rango_pct=0.15, pasos=61, ponderar_por_tiempo=False):
    """
    Calcula:
      - gex_spot: GEX total al precio actual.
      - flip_point: precio hipotético más cercano al spot actual donde
        el GEX total cruza de signo (None si no hay cruce dentro del rango).
      - curva: lista de (precio, gex) para graficar, dentro de
        +-rango_pct alrededor del spot.

    rango_pct: cuánto % arriba/abajo del spot se explora (default 15%).
    pasos: cuántos puntos de la grilla (más pasos = más preciso, más lento).
    ponderar_por_tiempo: si True, pondera cada instrumento por
        1/sqrt(dias_a_vencimiento) antes de sumarlo. Pensado para el
        Flip Global, donde se combinan varios vencimientos y el OI de
        los lejanos podría distorsionar el cálculo sin esta ponderación
        (ver docstring de calcular_gex_en_spot para el detalle).

    Implementación: vectorizada con NumPy (ver
    _calcular_curva_gex_vectorizada), no loops anidados de Python.
    """

    ahora = datetime.now(timezone.utc)

    precios_grilla, gex_por_precio, gex_spot = _calcular_curva_gex_vectorizada(
        instrumentos, spot_actual, ahora, rango_pct=rango_pct, pasos=pasos,
        ponderar_por_tiempo=ponderar_por_tiempo,
    )

    if precios_grilla is None:
        return None

    curva = list(zip(precios_grilla.tolist(), gex_por_precio.tolist()))

    # Buscar el cruce de signo más cercano al spot actual
    flip_point = None
    mejor_distancia = None

    for i in range(len(curva) - 1):
        precio_a, gex_a = curva[i]
        precio_b, gex_b = curva[i + 1]

        if gex_a == 0:
            candidato = precio_a
        elif gex_a * gex_b < 0:
            # Interpolación lineal simple entre los dos puntos para
            # estimar el precio exacto donde cruza cero
            proporcion = abs(gex_a) / (abs(gex_a) + abs(gex_b))
            candidato = precio_a + proporcion * (precio_b - precio_a)
        else:
            continue

        distancia = abs(candidato - spot_actual)
        if mejor_distancia is None or distancia < mejor_distancia:
            mejor_distancia = distancia
            flip_point = candidato

    return {
        "gex_spot": gex_spot,
        "flip_point": flip_point,
        "curva": curva,
        "total_contratos": len(instrumentos),
    }


# ----------------------------------
# WALLS, FLIP LOCAL/GLOBAL Y GAMMA ZONES
# ----------------------------------
#
# Construye los niveles que se dibujan sobre el candlestick:
# Call Wall, Put Wall, Flip Global (Semanal), Flip Local (Cercano),
# y las zonas de gamma local (picos de la curva de GEX).
#
# Definición de Call/Put Wall (especificación del usuario):
#   Nivel de precio donde se concentra la mayor exposición de OI de
#   un tipo de contrato. Compone: strike, OI en contratos, cambio de
#   OI (requiere historial propio, Deribit no lo da vía API pública),
#   gamma asociada al strike, distancia % al precio actual, y rol
#   (soporte si está debajo del precio, resistencia si está arriba).


def agrupar_oi_por_strike(instrumentos, tipo, vencimiento_max=None):
    """
    Agrega el OI de todos los instrumentos de un tipo (call/put) por
    strike. Si vencimiento_max se especifica (datetime), solo suma
    instrumentos cuyo vencimiento sea <= ese límite (para Flip Local).
    Devuelve dict {strike: oi_total}.
    """

    acumulado = {}

    for inst in instrumentos:
        if inst["tipo"] != tipo:
            continue
        if vencimiento_max is not None and inst["vencimiento"] > vencimiento_max:
            continue

        acumulado[inst["strike"]] = acumulado.get(inst["strike"], 0.0) + inst["oi"]

    return acumulado


def encontrar_wall(instrumentos, tipo, spot_actual, ahora):
    """
    Encuentra el strike con mayor OI agregado para un tipo de opción
    (call o put) y devuelve su info completa: strike, oi, gamma en ese
    strike (usando el vencimiento más cercano disponible en ese strike
    como referencia), distancia % al spot, y rol.
    """

    acumulado = agrupar_oi_por_strike(instrumentos, tipo)

    if not acumulado:
        return None

    strike_wall = max(acumulado, key=acumulado.get)
    oi_wall = acumulado[strike_wall]

    # Para la gamma asociada, tomamos el vencimiento más próximo entre
    # los instrumentos que tienen ese strike y ese tipo (la wall suele
    # estar dominada por el vencimiento más cercano/líquido).
    candidatos = [
        inst for inst in instrumentos
        if inst["tipo"] == tipo and inst["strike"] == strike_wall
    ]
    candidatos.sort(key=lambda i: i["vencimiento"])
    inst_referencia = candidatos[0]

    dias = (inst_referencia["vencimiento"] - ahora).total_seconds() / 86400.0
    gamma_strike = _gamma_black_scholes(
        spot=spot_actual,
        strike=strike_wall,
        vol_anual=inst_referencia["iv"],
        dias_a_vencimiento=max(dias, 0.01),
    )

    distancia_pct = ((strike_wall - spot_actual) / spot_actual) * 100

    rol = "Resistencia" if strike_wall > spot_actual else "Soporte"

    return {
        "tipo": tipo,
        "strike": strike_wall,
        "oi": oi_wall,
        "gamma": gamma_strike,
        "distancia_pct": distancia_pct,
        "rol": rol,
    }


def calcular_flip(instrumentos, spot_actual, vencimiento_max=None, rango_pct=0.15, pasos=61, ponderar_por_tiempo=False):
    """
    Calcula el flip point (cruce de signo del GEX) usando solo los
    instrumentos con vencimiento <= vencimiento_max (si se especifica).
    Si vencimiento_max es None, usa TODOS los instrumentos disponibles
    (esto es lo que diferencia Flip Global de Flip Local).

    ponderar_por_tiempo: ver docstring de calcular_gamma_exposure.
    Se usa True para el Flip Global (mezcla varios vencimientos) y
    False para el Flip Local (un solo vencimiento, no hace falta).
    """

    if vencimiento_max is not None:
        filtrados = [i for i in instrumentos if i["vencimiento"] <= vencimiento_max]
    else:
        filtrados = instrumentos

    if not filtrados:
        return None

    return calcular_gamma_exposure(
        filtrados, spot_actual, rango_pct=rango_pct, pasos=pasos,
        ponderar_por_tiempo=ponderar_por_tiempo,
    )


def detectar_zonas_gamma_local(curva, spot_actual):
    """
    A partir de la curva (precio, gex) ya calculada, encuentra el pico
    positivo más alto (zona de gamma local "hi") y el pico negativo
    más profundo (zona de gamma local "lo"). Estas NO son cruces de
    cero, son los puntos de mayor concentración de gamma en cada
    dirección dentro del rango explorado.
    """

    if not curva:
        return None, None

    pico_positivo = max(curva, key=lambda par: par[1])
    pico_negativo = min(curva, key=lambda par: par[1])

    zona_hi = None
    zona_lo = None

    if pico_positivo[1] > 0:
        zona_hi = {"precio": pico_positivo[0], "gex": pico_positivo[1]}

    if pico_negativo[1] < 0:
        zona_lo = {"precio": pico_negativo[0], "gex": pico_negativo[1]}

    return zona_hi, zona_lo


def vencimiento_mas_proximo(instrumentos):
    """Devuelve el datetime del vencimiento más próximo entre todos los instrumentos."""

    if not instrumentos:
        return None

    return min(inst["vencimiento"] for inst in instrumentos)


def vencimientos_disponibles_ordenados(instrumentos):
    """Lista de vencimientos únicos, ordenados de más próximo a más lejano."""

    return sorted(set(inst["vencimiento"] for inst in instrumentos))


def calcular_rr_sugerido(precio_actual, nivel_valor, rol, siguiente_nivel=None):
    """
    Calcula un Riesgo:Beneficio sugerido MUY simplificado para mostrar
    junto a cada nivel: distancia al nivel (riesgo, asumiendo stop
    justo detrás del nivel) vs distancia al siguiente nivel relevante
    en la misma dirección (beneficio, como objetivo de toma de
    ganancia). Si no hay siguiente nivel, no se puede estimar.

    Esto es orientativo, no una recomendación financiera: el usuario
    debe validar con su propia gestión de riesgo.
    """

    riesgo = abs(nivel_valor - precio_actual)

    if siguiente_nivel is None or riesgo == 0:
        return None

    beneficio = abs(siguiente_nivel - nivel_valor)

    if beneficio == 0:
        return None

    return round(beneficio / riesgo, 2)


def mostrar_senal(texto, boton):

    if "Alcista" in texto:
        circulo = "🟢"
    elif "Bajista" in texto:
        circulo = "🔴"
    else:
        circulo = "🟡"

    col_btn, col_estado = st.columns([1, 2])

    with col_btn:
        presionado = st.button(boton, key=boton)

    with col_estado:
        st.markdown(
            f"""
            <div style="
            font-size:22px;
            color:white;
            text-align:left;
            margin-top:5px;
            ">
            {circulo} {texto.replace("🟢 ", "").replace("🔴 ", "").replace("🟡 ", "")}
            </div>
            """,
            unsafe_allow_html=True
        )

    return presionado


# ----------------------------------
# ANÁLISIS DE LIQUIDEZ (swing highs / lows)
# ----------------------------------

def detectar_niveles_liquidez(df, ventana=5, max_niveles=4, distancia_minima_usd=0.0):
    """
    Detecta swing highs/lows locales: velas cuyo high (o low) es el
    máximo (o mínimo) dentro de una ventana de N velas a cada lado.
    Estos puntos son un proxy de "zonas donde el precio ya reaccionó
    antes" -> niveles de liquidez/absorción relevantes para Scalp.

    distancia_minima_usd: filtra niveles que estén MUY pegados al
    precio actual (ej: ruido de 2-3 velas en 1m). Si tu operativa
    busca movimientos de 250-1000 USD, no tiene sentido que te marque
    un soporte a 40 USD de distancia. FIX pedido por el usuario tras
    comparar contra Bookmap: en Scalp los niveles aparecían demasiado
    pegados al precio (ej: apenas 250 USD entre soporte y resistencia
    en BTC, cuando su stop mínimo ya es de 200-350 USD).

    Devuelve los niveles más cercanos al precio actual (arriba y abajo)
    que respeten la distancia mínima.
    """

    highs = df["high"].values
    lows = df["low"].values
    n = len(df)

    swing_highs = []
    swing_lows = []

    for i in range(ventana, n - ventana):

        ventana_high = highs[i - ventana: i + ventana + 1]
        ventana_low = lows[i - ventana: i + ventana + 1]

        if highs[i] == ventana_high.max():
            swing_highs.append(highs[i])

        if lows[i] == ventana_low.min():
            swing_lows.append(lows[i])

    precio_actual = df["close"].iloc[-1]

    # Resistencias: swing highs por ENCIMA del precio actual Y a una
    # distancia mínima razonable, más cercanos primero
    resistencias = sorted(
        [h for h in swing_highs if h > precio_actual + distancia_minima_usd]
    )[:max_niveles]

    # Soportes: swing lows por DEBAJO del precio actual Y a una
    # distancia mínima razonable, más cercanos primero
    soportes = sorted(
        [l for l in swing_lows if l < precio_actual - distancia_minima_usd],
        reverse=True
    )[:max_niveles]

    return soportes, resistencias


def nivel_mas_cercano(precio_actual, soportes, resistencias):
    """Devuelve el nivel de liquidez más cercano al precio actual y su distancia %."""

    candidatos = []

    if soportes:
        dist = abs(precio_actual - soportes[0]) / precio_actual * 100
        candidatos.append(("soporte", soportes[0], dist))

    if resistencias:
        dist = abs(precio_actual - resistencias[0]) / precio_actual * 100
        candidatos.append(("resistencia", resistencias[0], dist))

    if not candidatos:
        return None

    return min(candidatos, key=lambda x: x[2])


# ----------------------------------
# VELOCIDAD / ACELERACIÓN
# ----------------------------------

def calcular_velocidad_precio(df, velas_recientes=3, velas_previas=3):
    """
    Compara el cambio % de las últimas N velas contra el cambio % del
    tramo anterior. Si el cambio reciente es mayor en magnitud, el
    movimiento se está ACELERANDO. Si es menor, se está DESACELERANDO.
    Esto es lo que distingue "impulso continuando" de "impulso
    agotándose justo antes de una absorción".
    """

    if len(df) < (velas_recientes + velas_previas + 1):
        return 0.0, 0.0, "lateral"

    cierre = df["close"].values

    tramo_reciente = cierre[-velas_recientes:]
    tramo_previo = cierre[-(velas_recientes + velas_previas):-velas_recientes]

    cambio_reciente = (tramo_reciente[-1] - tramo_reciente[0]) / tramo_reciente[0] * 100
    cambio_previo = (tramo_previo[-1] - tramo_previo[0]) / tramo_previo[0] * 100

    aceleracion = abs(cambio_reciente) - abs(cambio_previo)

    if aceleracion > 0.05:
        estado_velocidad = "acelerando"
    elif aceleracion < -0.05:
        estado_velocidad = "desacelerando"
    else:
        estado_velocidad = "estable"

    return cambio_reciente, cambio_previo, estado_velocidad


# ----------------------------------
# DETECCIÓN DE ABSORCIÓN (proxy de iceberg)
# ----------------------------------

def detectar_absorcion(df, lookback=20, umbral_volumen=1.3, umbral_rango=0.75):
    """
    Proxy de absorción/iceberg SIN datos de book real: busca la vela
    más reciente dentro de la ventana con volumen muy por encima del
    promedio, pero rango de precio (high-low) chico en relación a su
    propio volumen. Mucho volumen + poco movimiento de precio sugiere
    que alguien está absorbiendo el flujo sin dejar que el precio se
    mueva (comportamiento típico de absorción institucional/iceberg).

    Esto NO confirma un iceberg real (eso requiere Level 2 / book data),
    es una señal honesta basada en velas, no una certeza.

    Devuelve además un "score" 0-100 de qué tan cerca está la señal de
    dispararse, para poder mostrar un gauge visual además del texto.
    """

    if len(df) < lookback + 1:
        return False, None

    ventana = df.tail(lookback + 1).iloc[:-1]  # excluye la vela actual del promedio
    vela_actual = df.iloc[-1]

    vol_promedio = ventana["volume"].mean()

    if vol_promedio <= 0:
        return False, None

    rango_actual = vela_actual["high"] - vela_actual["low"]
    rango_promedio = (ventana["high"] - ventana["low"]).mean()

    if rango_promedio <= 0:
        return False, None

    ratio_volumen = vela_actual["volume"] / vol_promedio
    ratio_rango = rango_actual / rango_promedio

    hay_absorcion = (ratio_volumen >= umbral_volumen) and (ratio_rango <= umbral_rango)

    # Score de "qué tan cerca" de dispararse, combinando ambas condiciones.
    # Cada condición aporta hasta 50 puntos, proporcional a qué tan cerca
    # está de cruzar su propio umbral.
    score_volumen = min(ratio_volumen / umbral_volumen, 1.0) * 50

    if ratio_rango <= umbral_rango:
        score_rango = 50.0
    else:
        # Cuanto más lejos por ENCIMA del umbral (rango grande = mala señal),
        # menos puntos. Se cae a 0 cuando ratio_rango dobla el umbral.
        exceso = (ratio_rango - umbral_rango) / umbral_rango
        score_rango = max(0.0, 50 * (1 - min(exceso, 1.0)))

    score_absorcion = round(score_volumen + score_rango)

    detalle = {
        "ratio_volumen": round(ratio_volumen, 2),
        "ratio_rango": round(ratio_rango, 2),
        "score": score_absorcion,
        "tiempo": vela_actual["open_time"],
        "precio": float(vela_actual["close"]),
    }

    return hay_absorcion, detalle


# ----------------------------------
# DATOS BTC (ticker)
# ----------------------------------

try:
    ticker = obtener_ticker()

    if "error" in ticker:
        raise ConnectionError(ticker["error"])

    precio = float(ticker["lastPrice"])
    cambio = float(ticker["priceChangePercent"])
    volumen = float(ticker["volume"])

    c1, c2, c3 = st.columns(3)

    with c1:
        st.metric("Precio BTC", f"${precio:,.2f}")
    with c2:
        st.metric("Cambio 24h", f"{cambio:.2f}%")
    with c3:
        st.metric("Volumen BTC", f"{volumen:,.0f}")

except Exception as e:
    st.error(
        f"⚠️ No se pudo obtener el precio de BTC desde Binance (vía proxy): {e}\n\n"
        f"Puede ser un problema temporal de la API. "
        f"Se reintenta automáticamente en 15 segundos."
    )

# ----------------------------------
# FETCH ÚNICO DE VELAS POR TIMEFRAME
# (FIX: antes se pedían las mismas velas hasta 3 veces por refresh)
# ----------------------------------

# Velas de los 3 timeframes "fijos" del panel Multi-Timeframe.
# Se piden UNA sola vez y se reutilizan para tendencia + Market Intelligence.
df_5m = obtener_velas("5m", 50)
df_15m = obtener_velas("15m", 50)
df_1h = obtener_velas("1h", 50)

# Punto de control: si alguno de los 3 vino vacío (Binance no
# respondió), detenemos acá. Más abajo el Dealer Score usa
# df_1h["close"].iloc[-1] directamente, que explotaría igual que el
# error original si dejáramos pasar un df vacío sin chequear.
if df_5m.empty or df_15m.empty or df_1h.empty:
    error_detalle = st.session_state.get(
        "error_binance_velas", "Sin detalle del error disponible."
    )
    st.error(
        f"⚠️ No se pudo obtener datos de velas de Binance (timeframes 5m/15m/1h). "
        f"El dashboard no puede continuar este refresh.\n\n"
        f"Detalle: {error_detalle}\n\n"
        f"Puede ser un problema temporal de la API. Se va a reintentar "
        f"automáticamente en 15 segundos."
    )
    st.stop()

tendencia_5m = obtener_tendencia_desde_df(df_5m)
tendencia_15m = obtener_tendencia_desde_df(df_15m)
tendencia_1h = obtener_tendencia_desde_df(df_1h)

# FIX: el df de los gráficos/presión SIEMPRE se pide aparte, con su
# propio límite fijo (100 velas), sin reutilizar df_5m/df_15m/df_1h.
# Antes, si data_timeframe coincidía con uno de esos tres (por ejemplo
# en Scalp "1H → 5M", donde data_timeframe = "5m"), se reusaba el df_5m
# de 50 velas pensado para tendencia -> el gráfico mostraba menos
# velas/rango que en los otros sub-modos de Scalp (1m, 3m), que sí
# pedían 100 velas frescas. Ahora todos los sub-modos son consistentes.
df = obtener_velas(data_timeframe, 100)

# Punto de control central: si Binance no respondió (vía proxy), df viene vacío.
# En vez de dejar que explote en cualquier otro .iloc[-1] más adelante
# (con un traceback ilegible), avisamos claro y detenemos la ejecución
# de esta vuelta del script. st_autorefresh va a reintentar solo en 15s.
if df.empty:
    error_detalle = st.session_state.get(
        "error_binance_velas", "Sin detalle del error disponible."
    )
    st.error(
        f"⚠️ No se pudo obtener datos de velas de Binance para el timeframe "
        f"{data_timeframe}. El dashboard no puede continuar este refresh.\n\n"
        f"Detalle: {error_detalle}\n\n"
        f"Puede ser un problema temporal de la API de Binance o del proxy CORS. "
        f"Se va a reintentar automáticamente en 15 segundos."
    )
    st.stop()

# ----------------------------------
# ANÁLISIS SCALP: liquidez, velocidad, absorción
# (calculados sobre el df del timeframe operativo activo)
# ----------------------------------

ventanas_swing_por_timeframe = {
    "1m": 12,   # ventana más ancha: en 1m, 5 velas son ruido de minutos
    "3m": 8,
    "5m": 6,
    "15m": 5,
    "1h": 5,
}

ventana_swing_activa = ventanas_swing_por_timeframe.get(data_timeframe, 5)

cambio_reciente_precio, cambio_previo_precio, estado_velocidad = calcular_velocidad_precio(df)

hay_absorcion, detalle_absorcion = detectar_absorcion(df)

# Tendencia sobre el timeframe OPERATIVO activo (no los fijos 5m/15m/1h
# del panel Multi-Timeframe). Se usa para decidir hacia qué lado
# proyectar el candidato de absorción futura: si el mercado va alcista,
# el candidato más probable está arriba (resistencia); si va bajista,
# está abajo (soporte).
tendencia_activa = obtener_tendencia_desde_df(df)


# ----------------------------------
# MULTI TIMEFRAME + SELECTOR
# ----------------------------------

st.subheader("📊 Multi-Timeframe")

if modo == "Scalp":
    etiqueta_5m = "⚡ 5M → 1M"
    etiqueta_15m = "⚡ 15M → 3M"
    etiqueta_1h = "⚡ 1H → 5M"
else:
    etiqueta_5m = "🧠 5M"
    etiqueta_15m = "🧠 15M"
    etiqueta_1h = "🧠 1H"

m1, m2, m3 = st.columns(3)

with m1:
    if mostrar_senal(tendencia_5m, etiqueta_5m):
        st.session_state.timeframe = "5m"
        st.rerun()

with m2:
    if mostrar_senal(tendencia_15m, etiqueta_15m):
        st.session_state.timeframe = "15m"
        st.rerun()

with m3:
    if mostrar_senal(tendencia_1h, etiqueta_1h):
        st.session_state.timeframe = "1h"
        st.rerun()

st.divider()

# ----------------------------------
# CÁLCULO DE NIVELES PARA EL OVERLAY
# (Walls, Flip Global/Local, Gamma Zones, Imán/MAG, Absorción)
# Se calcula ANTES del gráfico porque el candlestick necesita estos
# valores para dibujar las líneas encima de las velas.
# ----------------------------------

precio_actual = df["close"].iloc[-1]

instrumentos_deribit = obtener_instrumentos_deribit()
deribit_disponible = instrumentos_deribit is not None and len(instrumentos_deribit) > 0

resultado_flip_global = None
resultado_flip_local = None
call_wall = None
put_wall = None
zona_gamma_hi = None
zona_gamma_lo = None
vencimiento_local_dt = None

if deribit_disponible:

    ahora = datetime.now(timezone.utc)

    vencimientos_ordenados = vencimientos_disponibles_ordenados(instrumentos_deribit)

    # Global: agrupa los próximos 3 a 5 vencimientos semanales disponibles
    # (simplificación pedida: "cúmulo de call y put wall más fuerte
    # durante tres a cinco vencimientos, simplificado semanal").
    vencimientos_global = vencimientos_ordenados[:5] if len(vencimientos_ordenados) >= 3 else vencimientos_ordenados
    vencimiento_global_max = vencimientos_global[-1] if vencimientos_global else None

    # Local: solo el vencimiento más próximo disponible.
    vencimiento_local_dt = vencimientos_ordenados[0] if vencimientos_ordenados else None

    resultado_flip_global = calcular_flip(
        instrumentos_deribit, precio_actual, vencimiento_max=vencimiento_global_max,
        ponderar_por_tiempo=True,  # evita que el OI de vencimientos lejanos distorsione el flip
    )
    resultado_flip_local = calcular_flip(
        instrumentos_deribit, precio_actual, vencimiento_max=vencimiento_local_dt
        # sin ponderar: un solo vencimiento, todos los contratos comparten el mismo t
    )

    call_wall = encontrar_wall(instrumentos_deribit, "call", precio_actual, ahora)
    put_wall = encontrar_wall(instrumentos_deribit, "put", precio_actual, ahora)

    if resultado_flip_global:
        zona_gamma_hi, zona_gamma_lo = detectar_zonas_gamma_local(
            resultado_flip_global["curva"], precio_actual
        )

# Historial de OI de las walls, para poder mostrar su variación con
# el correr de los refreshes (Deribit no da histórico vía API pública,
# así que lo construimos nosotros guardando snapshots en la sesión,
# igual que ya hacemos con el Open Interest de futuros).

if "call_wall_oi_historial" not in st.session_state:
    st.session_state.call_wall_oi_historial = []
if "put_wall_oi_historial" not in st.session_state:
    st.session_state.put_wall_oi_historial = []


def _actualizar_y_calcular_cambio_oi(historial_lista, oi_actual, ventana=10, tope=20):
    """Guarda el OI actual en el historial de sesión y devuelve el
    cambio % vs ~ventana refreshes atrás (o el más viejo disponible
    si todavía no hay tantos puntos). None si no hay historial previo."""

    cambio_pct = None

    if len(historial_lista) > 0:
        base_idx = -ventana if len(historial_lista) >= ventana else 0
        oi_base = historial_lista[base_idx]
        if oi_base > 0:
            cambio_pct = round(((oi_actual - oi_base) / oi_base) * 100, 2)

    historial_lista.append(oi_actual)
    if len(historial_lista) > tope:
        historial_lista.pop(0)

    return cambio_pct


call_wall_oi_cambio = None
put_wall_oi_cambio = None

if call_wall:
    call_wall_oi_cambio = _actualizar_y_calcular_cambio_oi(
        st.session_state.call_wall_oi_historial, call_wall["oi"]
    )

if put_wall:
    put_wall_oi_cambio = _actualizar_y_calcular_cambio_oi(
        st.session_state.put_wall_oi_historial, put_wall["oi"]
    )

# Filtro de ruido de liquidez (switch reubicado y mejorado, dentro del
# bloque de niveles porque alimenta directamente el overlay de MAG/Imán).

st.markdown("**🔇 Filtro de ruido de liquidez**")

filtro_liquidez_activo = st.toggle(
    "Ignorar niveles Imán (MAG) muy pegados al precio",
    value=True,
    help=(
        "⚠️ Esta variable afecta DOS lugares a la vez: la capa 🧲 IMÁN "
        "dibujada sobre el gráfico principal, Y el resumen de 'Niveles Imán "
        "(liquidez)' más abajo en la página. No son cálculos separados — "
        "es el mismo filtro aplicado en ambos.\n\n"
        "Cuando está activo, solo se muestran soportes/resistencias Imán a "
        "más de $130 USD del precio actual. Pensado para operativa con "
        "objetivos de 250–1000 USD y stops de 200–350 USD: un nivel a "
        "40–80 USD de distancia es ruido, no un nivel operable."
    ),
)

DISTANCIA_MINIMA_USD = 130.0 if filtro_liquidez_activo else 0.0
st.caption(
    "Filtro activo: ignorando niveles Imán a menos de $130 del precio (afecta al gráfico y al resumen de abajo)."
    if filtro_liquidez_activo else
    "Filtro inactivo: mostrando todos los niveles Imán, incluidos los más cercanos (afecta al gráfico y al resumen de abajo)."
)

soportes, resistencias = detectar_niveles_liquidez(
    df,
    ventana=ventana_swing_activa,
    max_niveles=4,
    distancia_minima_usd=DISTANCIA_MINIMA_USD
)

# ----------------------------------
# BOTONERA DE CAPAS (toggles individuales, estilo overlay de trading)
# ----------------------------------

if "capas_activas" not in st.session_state:
    st.session_state.capas_activas = {
        "IMAN": True,
        "MINI_FLIP": True,
        "FLIP_FULL": True,
        "GAMMA_ZONE": False,
        "WALLS": False,
        "ABSORB": True,
    }
# Migración: si quedó guardada una sesión vieja con la clave "FLIP"
# unificada, la separamos para no romper el estado de quien ya tenía
# el dashboard abierto antes de este cambio.
if "FLIP" in st.session_state.capas_activas:
    valor_previo = st.session_state.capas_activas.pop("FLIP")
    st.session_state.capas_activas.setdefault("MINI_FLIP", valor_previo)
    st.session_state.capas_activas.setdefault("FLIP_FULL", valor_previo)

st.markdown("**Capas sobre el gráfico**")

b1, b2, b3, b4, b5, b6 = st.columns(6)

with b1:
    st.session_state.capas_activas["IMAN"] = st.toggle(
        "🧲 IMÁN", value=st.session_state.capas_activas["IMAN"], key="cap_iman"
    )
with b2:
    st.session_state.capas_activas["MINI_FLIP"] = st.toggle(
        "🔁 MINI FLIP", value=st.session_state.capas_activas["MINI_FLIP"], key="cap_mini_flip",
        help="Flip Cercano (Local): solo el vencimiento más próximo de Deribit. Pensado para Scalp."
    )
with b3:
    st.session_state.capas_activas["FLIP_FULL"] = st.toggle(
        "🔁 FLIP FULL", value=st.session_state.capas_activas["FLIP_FULL"], key="cap_flip_full",
        help="Flip Semanal (Global): agrega los próximos 3 a 5 vencimientos de Deribit. Pensado para Normal/intradiario."
    )
with b4:
    st.session_state.capas_activas["GAMMA_ZONE"] = st.toggle(
        "🌀 GAMMA ZONE", value=st.session_state.capas_activas["GAMMA_ZONE"], key="cap_gamma"
    )
with b5:
    st.session_state.capas_activas["WALLS"] = st.toggle(
        "🧱 WALLS", value=st.session_state.capas_activas["WALLS"], key="cap_walls"
    )
with b6:
    st.session_state.capas_activas["ABSORB"] = st.toggle(
        "🧊 ABSORB", value=st.session_state.capas_activas["ABSORB"], key="cap_absorb"
    )

capas = st.session_state.capas_activas

# ----------------------------------
# GRÁFICO PRINCIPAL: CANDLESTICK + OVERLAY DE NIVELES
# ----------------------------------

st.divider()

x_min = df["open_time"].iloc[0]
x_max = df["open_time"].iloc[-1]


def _etiqueta_overlay(fig, y_valor, texto_corto, color_linea, color_fondo_rgba, dash="dot"):
    """
    Agrega una línea horizontal punteada que cruza TODO el candlestick
    (referencia visual sobre el precio) + una etiqueta CORTA anclada a
    una columna fija a la derecha del área de velas (usando xref='paper'
    para que no se mueva con el zoom/pan horizontal, siempre visible).

    El fondo de la etiqueta usa un color RGBA con canal alfa bajo
    (transparencia real, no afecta la nitidez del texto) para no tapar
    tanto las velas. El detalle completo de cada nivel sigue disponible
    en el panel de texto debajo del gráfico.
    """

    fig.add_shape(
        type="line",
        x0=x_min, x1=x_max,
        y0=y_valor, y1=y_valor,
        line=dict(color=color_linea, width=1, dash=dash),
    )
    fig.add_annotation(
        xref="paper", x=1.0,
        y=y_valor,
        text=texto_corto,
        showarrow=False,
        xanchor="left",
        yanchor="middle",
        font=dict(color=color_linea, size=11, family="Arial Black"),
        bgcolor=color_fondo_rgba,
        bordercolor=color_linea,
        borderwidth=1,
        borderpad=2,
    )


fig_overlay = go.Figure(
    data=[
        go.Candlestick(
            x=df["open_time"],
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="BTC/USDT",
        )
    ]
)

# --- Capa IMÁN (ex "MAG", niveles de liquidez / swing highs-lows) ---
if capas["IMAN"]:
    for s in soportes:
        _etiqueta_overlay(
            fig_overlay, s,
            f"🧲 ${s:,.0f}",
            "#3b82f6", "rgba(59,130,246,0.28)"
        )
    for r in resistencias:
        _etiqueta_overlay(
            fig_overlay, r,
            f"🧲 ${r:,.0f}",
            "#3b82f6", "rgba(59,130,246,0.28)"
        )

# --- Capa MINI FLIP (Local = vencimiento más próximo, pensado para Scalp) ---
if capas["MINI_FLIP"] and deribit_disponible:

    if resultado_flip_local and resultado_flip_local["flip_point"]:
        fp_local = resultado_flip_local["flip_point"]
        dist_local = ((fp_local - precio_actual) / precio_actual) * 100
        lado_local = "🟢" if fp_local < precio_actual else "🔴"
        _etiqueta_overlay(
            fig_overlay, fp_local,
            f"🔁 MINI ${fp_local:,.0f} {lado_local} ({dist_local:+.1f}%)",
            "#22c55e", "rgba(34,197,94,0.28)",
        )

# --- Capa FLIP FULL (Global = 3 a 5 vencimientos semanales, pensado para Normal/intradiario) ---
if capas["FLIP_FULL"] and deribit_disponible:

    if resultado_flip_global and resultado_flip_global["flip_point"]:
        fp_global = resultado_flip_global["flip_point"]
        dist_global = ((fp_global - precio_actual) / precio_actual) * 100
        _etiqueta_overlay(
            fig_overlay, fp_global,
            f"🔁 FULL ${fp_global:,.0f} ({dist_global:+.1f}%)",
            "#a855f7", "rgba(168,85,247,0.28)",
            dash="dash"
        )

# --- Capa GAMMA ZONE (picos locales de la curva de GEX) ---
if capas["GAMMA_ZONE"] and deribit_disponible:

    if zona_gamma_hi:
        _etiqueta_overlay(
            fig_overlay, zona_gamma_hi["precio"],
            f"🌀 hi ${zona_gamma_hi['precio']:,.0f}",
            "#10b981", "rgba(16,185,129,0.28)",
            dash="dashdot"
        )
    if zona_gamma_lo:
        _etiqueta_overlay(
            fig_overlay, zona_gamma_lo["precio"],
            f"🌀 lo ${zona_gamma_lo['precio']:,.0f}",
            "#10b981", "rgba(16,185,129,0.28)",
            dash="dashdot"
        )

# --- Capa WALLS (Call Wall / Put Wall) ---
if capas["WALLS"] and deribit_disponible:

    if call_wall:
        cambio_txt = f" Δ{call_wall_oi_cambio:+.1f}%" if call_wall_oi_cambio is not None else ""
        _etiqueta_overlay(
            fig_overlay, call_wall["strike"],
            f"🧱C ${call_wall['strike']:,.0f}{cambio_txt}",
            "#ef4444", "rgba(239,68,68,0.28)",
            dash="solid"
        )

    if put_wall:
        cambio_txt = f" Δ{put_wall_oi_cambio:+.1f}%" if put_wall_oi_cambio is not None else ""
        _etiqueta_overlay(
            fig_overlay, put_wall["strike"],
            f"🧱P ${put_wall['strike']:,.0f}{cambio_txt}",
            "#f97316", "rgba(249,115,22,0.28)",
            dash="solid"
        )

# --- Capa ABSORB (candidato de absorción PROYECTADA hacia adelante) ---
# Antes: marcaba el candle PASADO donde ya había ocurrido una absorción
# confirmada (mirando atrás). Ahora: proyecta el punto donde es MÁS
# PROBABLE que ocurra la PRÓXIMA absorción, según la dirección del
# mercado — el nivel Imán (liquidez) más cercano en esa dirección, que
# es donde coincide liquidez concentrada con mayor probabilidad de
# reacción. Si la tendencia activa es alcista, el candidato está
# ARRIBA (resistencia Imán); si es bajista, está ABAJO (soporte Imán).
if capas["ABSORB"]:

    nivel_candidato_absorcion = None
    direccion_absorcion = None

    if "Alcista" in tendencia_activa and resistencias:
        nivel_candidato_absorcion = resistencias[0]
        direccion_absorcion = "resistencia (mercado alcista)"
    elif "Bajista" in tendencia_activa and soportes:
        nivel_candidato_absorcion = soportes[0]
        direccion_absorcion = "soporte (mercado bajista)"

    if nivel_candidato_absorcion is not None:
        dist_absorcion = ((nivel_candidato_absorcion - precio_actual) / precio_actual) * 100
        # Score de absorción actual como referencia de "qué tan activo
        # está el patrón ahora", aunque el nivel proyectado sea futuro.
        score_ref = detalle_absorcion["score"] if detalle_absorcion else 0
        _etiqueta_overlay(
            fig_overlay, nivel_candidato_absorcion,
            f"🧊 ABS ${nivel_candidato_absorcion:,.0f} ({dist_absorcion:+.1f}%)",
            "#eab308", "rgba(234,179,8,0.28)",
            dash="longdash",
        )

fig_overlay.add_hline(
    y=precio_actual, line_dash="dot", line_color="yellow", line_width=1,
    annotation_text=f"Precio actual ${precio_actual:,.0f}",
    annotation_position="right",
)

# Zoom/pan libres en ambos ejes: por defecto el rango inicial es el
# normal de las velas (sin autoescalar para que entren niveles lejanos),
# pero el usuario puede arrastrar o usar la rueda del mouse para
# alejarse y ver niveles que quedaron fuera del recuadro (ej. un Call
# Wall o un Flip Global muy lejos del precio actual).
#
# El eje de precios pasa a la derecha (barra de valores), con más
# densidad de marcas (nticks) para que no salte tanto entre niveles.
# El eje de tiempo (X) queda visible abajo, como en el gráfico original.
fig_overlay.update_xaxes(fixedrange=False, visible=True, showticklabels=True)
fig_overlay.update_yaxes(fixedrange=False, side="right", nticks=25, visible=True, showticklabels=True)

fig_overlay.update_layout(
    template="plotly_dark",          # fuerza el tema oscuro explícitamente
    paper_bgcolor="#0e1117",          # fondo exterior, igual al fondo de Streamlit
    plot_bgcolor="#0e1117",           # fondo del área de trazado (donde van las velas)
    xaxis_rangeslider_visible=False,
    height=650,
    margin=dict(l=10, r=90, t=10, b=40),  # más margen abajo para que el eje de tiempo no quede cortado
    dragmode="pan",  # arrastrar mueve el gráfico; zoom queda a cargo de la rueda
)

st.caption(
    "🖱️ Posicionate sobre el gráfico y usá la rueda del mouse para hacer zoom "
    "(acercar/alejar). Arrastrá con el clic para desplazarte si algún nivel "
    "(Wall, Flip, Gamma Zone) quedó fuera del recuadro visible."
)

st.plotly_chart(
    fig_overlay,
    use_container_width=True,
    config={
        "scrollZoom": True,       # la rueda del mouse hace zoom en vez de scrollear la página
        "displayModeBar": True,
        "modeBarButtonsToAdd": ["pan2d", "zoomIn2d", "zoomOut2d", "autoScale2d", "resetScale2d"],
    },
)

# ----------------------------------
# NOTA SOBRE PERSISTENCIA DE ZOOM (estado actual):
#
# Se probó streamlit-plotly-events para mantener el zoom entre
# refreshes de 15s, pero ese componente: (1) no respeta el tema oscuro
# de la figura salvo configuración adicional, y (2) está diseñado para
# capturar clicks/hover/selección — para que dispare el evento de
# relayout (zoom/pan) de forma confiable hace falta tener activo al
# menos uno de esos otros eventos, lo cual generaba re-ejecuciones no
# deseadas. Se revirtió a st.plotly_chart nativo, que renderiza
# correctamente pero NO expone el evento de zoom, por lo que el rango
# se reinicia en cada refresh automático (cada 15s), no solo al
# cambiar de temporalidad.
#
# Plan para resolver esto de verdad: un componente HTML personalizado
# con JavaScript (vía st.components.v1.html) que dibuje el candlestick
# con Plotly.js directamente y capture el evento 'plotly_relayout' del
# lado del cliente, comunicando el rango a Streamlit con un mecanismo
# de mensajería propio. Es más trabajo pero aísla el evento que
# necesitamos sin arrastrar comportamiento no deseado. Si querés que
# lo arme, avisame y seguimos por ese camino en el próximo paso.
# ----------------------------------

if not deribit_disponible:
    st.warning(
        "⚠️ No se pudo conectar a la API de Deribit en este refresh. "
        "Las capas MINI FLIP, FLIP FULL, GAMMA ZONE y WALLS no están disponibles "
        "momentáneamente (la capa IMÁN y ABSORB siguen funcionando, son cálculos "
        "sobre velas de Binance)."
    )

# ----------------------------------
# TENDENCIA (gráfico de línea, sin overlay — referencia rápida)
# ----------------------------------

with st.expander("📈 Ver gráfico de tendencia simple (línea)"):
    fig_line = go.Figure()
    fig_line.add_trace(
        go.Scatter(x=df["open_time"], y=df["close"], mode="lines")
    )
    fig_line.update_layout(height=350)
    st.plotly_chart(fig_line, use_container_width=True)

# ----------------------------------
# VARIABLES GLOBALES
# ----------------------------------

funding = obtener_funding()
oi = obtener_open_interest()

# FIX: si Binance Futures falla, no rompemos el dashboard.
# Usamos 0.0 como neutro y marcamos que el dato no está disponible.
funding_disponible = funding is not None
oi_disponible = oi is not None

funding_valor = funding if funding_disponible else 0.0
oi_valor = oi if oi_disponible else 0.0

# ----------------------------------
# PRESION REAL DEL MERCADO
# ----------------------------------

buy_volume = df["taker_buy_base"].sum()
total_volume = df["volume"].sum()

if total_volume > 0:
    buy_pressure = round((buy_volume / total_volume) * 100, 1)
    sell_pressure = round(100 - buy_pressure, 1)
else:
    buy_pressure = 50
    sell_pressure = 50

# ----------------------------------
# PANEL PROFESIONAL
# ----------------------------------

st.divider()

panel1, panel2, panel3, panel4 = st.columns(4)

# -----------------------------
# ESTADO GENERAL
# -----------------------------

with panel1:

    st.subheader(
        "🧠 Estado",
        help=(
            "Resume si las condiciones generales del mercado son favorables para "
            "operar a favor de la tendencia, o si conviene ser cauteloso. Combina "
            "funding, Open Interest, presión de compra/venta y distancia a la "
            "media de 1H en un solo puntaje (Dealer Score). No es una señal de "
            "compra/venta, es una foto del contexto."
        ),
    )

    # ----------------------------------
    # DEALER SCORE V2 — más granular
    # FIX: antes eran 4 condiciones binarias de 25 pts cada una (todo
    # o nada). Ahora cada componente aporta una porción proporcional
    # a qué tan fuerte es la señal, no solo si está presente o no.
    # ----------------------------------

    dealer_score = 0.0

    # Funding: aporta hasta 25, proporcional a la magnitud (capado en 0.05%)
    if funding_disponible:
        magnitud_funding = min(abs(funding_valor) / 0.05, 1.0)
        if funding_valor > 0:
            dealer_score += 25 * magnitud_funding
        # funding negativo no resta, pero tampoco suma (es info aparte, ver Institucional)

    # OI: aporta hasta 25, proporcional a cuánto supera el umbral de referencia
    if oi_disponible:
        exceso_oi = max(0.0, (oi_valor - 90000) / 90000)
        dealer_score += 25 * min(exceso_oi * 5, 1.0)

    # Presión compradora: aporta hasta 25, proporcional al desbalance
    desbalance_presion = (buy_pressure - 50) / 50  # -1 a 1
    dealer_score += 25 * max(0.0, desbalance_presion)

    # Tendencia 1H: aporta hasta 25, proporcional a la distancia a la SMA20
    dist_sma_1h = (df_1h["close"].iloc[-1] - df_1h["close"].tail(20).mean()) / df_1h["close"].iloc[-1] * 100
    dealer_score += 25 * min(max(dist_sma_1h, 0) / 0.5, 1.0)

    dealer_score = round(min(dealer_score, 100))

    st.metric(
        "Dealer Score", f"{dealer_score}/100",
        help=(
            "0 a 100. Más alto = más señales alineadas a favor de continuidad "
            "alcista (funding pagando largos, OI creciendo, presión compradora, "
            "precio por encima de su media de 1H). No mide si el mercado va a "
            "subir o bajar en sí, mide cuántas señales coinciden entre ellas. "
            "Por debajo de 50: las señales están mezcladas o en contra."
        ),
    )

    # ----------------------------------
    # MARKET MAKER vs RETAIL (proxy por convergencia de señales)
    # Aclaración honesta: esto NO es un dato confirmado de quién opera,
    # es una inferencia por convergencia de proxies típicos:
    # - funding extremo + volumen alto -> apalancamiento retail
    # - absorción (mucho volumen, poco rango) -> comportamiento de MM/dealer
    # - movimiento "limpio" sin mucho volumen -> participación institucional
    # ----------------------------------

    mm_score = 0
    retail_proxy_score = 0

    if hay_absorcion:
        mm_score += 2  # absorber flujo sin mover precio es comportamiento típico de MM/dealer

    if funding_disponible and abs(funding_valor) > 0.02:
        retail_proxy_score += 2  # funding muy estirado = apalancamiento retail unidireccional

    if estado_velocidad == "desacelerando" and hay_absorcion:
        mm_score += 1  # impulso frenando justo donde hay absorción = MM defendiendo nivel

    if estado_velocidad == "acelerando" and not hay_absorcion:
        retail_proxy_score += 1  # impulso acelerando sin absorción = flujo retail persiguiendo precio

    if mm_score > retail_proxy_score:
        actor_dominante = "🏦 Posible Market Maker"
    elif retail_proxy_score > mm_score:
        actor_dominante = "👤 Posible Retail"
    else:
        actor_dominante = "⚖️ Sin señal clara"

    st.caption(f"Actor dominante (estimado): {actor_dominante}")

    # -----------------------------
    # LECTURA ESTADO INTELIGENTE
    # (Movida adentro de la columna Estado: antes vivía suelta debajo
    # de las 4 columnas, atravesando todo el ancho, lo que la hacía
    # parecer un resumen general del dashboard cuando en realidad es
    # parte específica del análisis de Estado/Dealer Score.)
    # -----------------------------

    if dealer_score >= 75:
        estado_general = "🟢 Condiciones favorables"
    elif dealer_score >= 50:
        estado_general = "🟡 Mercado mixto"
    else:
        estado_general = "🔴 Baja convicción"

    if modo == "Scalp":
        contexto_estado = (
            "⚡ Modo Scalp activo\n"
            "Evaluando microflujo, presión inmediata y reacción del precio."
        )
    else:
        contexto_estado = (
            "🧠 Modo Normal activo\n"
            "Evaluando estructura, tendencia y participación."
        )

    aviso_datos = ""
    if not funding_disponible or not oi_disponible:
        aviso_datos = "\n\n⚠️ Datos de derivados no disponibles en este refresh (Binance Futures)."

    st.info(
        f"""
{estado_general}

{contexto_estado}{aviso_datos}
"""
    )

# -----------------------------
# INSTITUCIONAL
# -----------------------------

with panel2:

    st.subheader(
        "🌎 Institucional",
        help=(
            "Mira el costo de mantener posiciones apalancadas (funding) y "
            "cuántos contratos abiertos hay en futuros (Open Interest). Funding "
            "positivo = los largos le pagan a los cortos (mercado inclinado "
            "hacia arriba, riesgo de sobrecompra). Si el OI crece junto con el "
            "precio, hay dinero nuevo entrando, no solo gente cerrando posiciones."
        ),
    )

    if funding_disponible:
        estado_funding = "🟢" if funding_valor > 0 else "🔴"
        st.metric("Funding", f"{funding_valor:.4f}% {estado_funding}")
    else:
        st.metric("Funding", "N/D")

    if oi_disponible:
        st.metric("Open Interest", f"{oi_valor:,.0f}")
    else:
        st.metric("Open Interest", "N/D")

    # CAMBIO OPEN INTEREST
    # FIX (calibración): el OI de Binance Futures en BTCUSDT se mueve
    # muy poco en 15s (típicamente 0.01%-0.05%), muy por debajo del
    # umbral de 0.5% que se usaba antes -> la barra quedaba siempre en
    # "estable" porque el umbral estaba pensado para otra cadencia.
    #
    # Ahora se calculan DOS cambios:
    # - cambio_oi_inmediato: vs el refresh anterior (~15s) — sigue
    #   existiendo porque lo usa Flow para la lectura de microestructura.
    # - cambio_oi_ventana: vs el valor de ~10 refreshes atrás (~2.5 min)
    #   — esto SÍ acumula suficiente movimiento como para que la barra
    #   tenga rango dinámico real, en vez de oscilar entre 0 y 0.02%.
    #
    # El umbral de clasificación también se ajustó a un valor realista
    # para el dato acumulado (0.15% en vez de 0.5%).

    cambio_oi_inmediato = 0.0
    cambio_oi_ventana = 0.0

    if oi_disponible:

        if len(st.session_state.oi_historial) > 0:
            oi_anterior = st.session_state.oi_historial[-1]
            if oi_anterior > 0:
                cambio_oi_inmediato = round(((oi_valor - oi_anterior) / oi_anterior) * 100, 4)

        if len(st.session_state.oi_historial) >= 10:
            oi_base_ventana = st.session_state.oi_historial[-10]
            if oi_base_ventana > 0:
                cambio_oi_ventana = round(((oi_valor - oi_base_ventana) / oi_base_ventana) * 100, 2)
        elif len(st.session_state.oi_historial) > 0:
            # todavía no hay 10 puntos: usamos el más viejo disponible
            oi_base_ventana = st.session_state.oi_historial[0]
            if oi_base_ventana > 0:
                cambio_oi_ventana = round(((oi_valor - oi_base_ventana) / oi_base_ventana) * 100, 2)

        st.session_state.oi_historial.append(oi_valor)

        if len(st.session_state.oi_historial) > 20:
            st.session_state.oi_historial.pop(0)

    # cambio_oi se mantiene como nombre usado por Flow/lectura más abajo,
    # pero ahora apunta al cambio de ventana (~2.5 min), más representativo
    cambio_oi = cambio_oi_ventana

    st.caption(f"Cambio OI (≈2.5 min): {cambio_oi_ventana}% · último refresh: {cambio_oi_inmediato}%")

    UMBRAL_OI = 0.15  # antes 0.5, demasiado alto para el movimiento real acumulado

    if cambio_oi_ventana > UMBRAL_OI:
        st.success("📈 Participación entrando")
    elif cambio_oi_ventana < -UMBRAL_OI:
        st.warning("📉 Participación saliendo")
    else:
        st.info("⚖️ OI estable")

    # Barra escalada a un rango más realista (antes dividía por 1 = 100%,
    # necesitabas un cambio de 1% para llenar la barra; ahora por 0.5%)
    st.progress(min(abs(cambio_oi_ventana) / 0.5, 1.0))

# -----------------------------
# PRESIÓN
# -----------------------------

with panel3:

    st.subheader(
        "⚔️ Presión",
        help=(
            "Compara el volumen que entró como compra de mercado (taker buy) "
            "contra el de venta de mercado, dentro de las velas visibles. Por "
            "encima de 60% de un lado se considera dominio claro de ese lado. "
            "No mide si el precio sube o baja, mide quién está siendo más "
            "agresivo ejecutando órdenes a mercado."
        ),
    )

    st.metric("Compra", f"{buy_pressure}% 🟢")
    st.metric("Venta", f"{sell_pressure}% 🔴")

    if buy_pressure > 60:
        st.success("Dominio comprador")
    elif sell_pressure > 60:
        st.error("Dominio vendedor")
    else:
        st.warning("Equilibrio")

# -----------------------------
# FLOW V2 INTELIGENTE
# -----------------------------

with panel4:

    st.subheader(
        "🌊 Flow",
        help=(
            "Combina hacia dónde se mueve el precio con si está entrando o "
            "saliendo participación (cambio de Open Interest), y le suma si el "
            "movimiento está acelerando o perdiendo fuerza. Por ejemplo: precio "
            "subiendo + OI cayendo sugiere un short squeeze (cierre de cortos), "
            "no necesariamente compradores nuevos entrando."
        ),
    )

    # precio_actual ya fue calculado arriba (se usa también en el overlay)
    precio_inicio = df["close"].iloc[0]

    cambio_precio = ((precio_actual - precio_inicio) / precio_inicio) * 100

    cambio_oi_flow = cambio_oi

    st.metric("Cambio Precio", f"{cambio_precio:.2f}%")
    st.metric("Cambio OI", f"{cambio_oi_flow:.2f}%")

    # FIX: antes el flow solo miraba si precio/OI subían o bajaban
    # (estático, comparando inicio vs fin de la ventana completa).
    # Ahora se le suma la VELOCIDAD: si el movimiento reciente es más
    # fuerte o más débil que el tramo anterior. Esto separa "impulso
    # que sigue ganando fuerza" de "impulso que se está agotando" -
    # clave para decidir si conviene buscar continuación o esperar
    # una absorción/reversión.

    iconos_velocidad = {
        "acelerando": "🚀 Acelerando",
        "desacelerando": "🐢 Desacelerando",
        "estable": "➡️ Estable",
        "lateral": "➡️ Estable",
    }

    st.caption(f"Velocidad reciente: {iconos_velocidad[estado_velocidad]}")

    # 🧠 FLOW INTELLIGENCE V3 (dirección + velocidad)

    if cambio_precio > 0 and cambio_oi_flow > 0:
        base_flow = "🟢 Compradores entrando\nPrecio ↑ + Open Interest ↑"
        if estado_velocidad == "acelerando":
            lectura_flow = base_flow + "\nParticipación aumentando, impulso ganando fuerza."
        elif estado_velocidad == "desacelerando":
            lectura_flow = base_flow + "\nParticipación aumenta, pero el impulso pierde velocidad: posible absorción cerca."
        else:
            lectura_flow = base_flow + "\nParticipación aumentando."

    elif cambio_precio > 0 and cambio_oi_flow < 0:
        base_flow = "🟡 Short squeeze posible\nPrecio ↑ + Open Interest ↓"
        if estado_velocidad == "acelerando":
            lectura_flow = base_flow + "\nCierre de cortos acelerando el movimiento."
        else:
            lectura_flow = base_flow + "\nCierre de posiciones vendedoras."

    elif cambio_precio < 0 and cambio_oi_flow > 0:
        base_flow = "🔴 Vendedores entrando\nPrecio ↓ + Open Interest ↑"
        if estado_velocidad == "acelerando":
            lectura_flow = base_flow + "\nNueva presión bajista ganando fuerza."
        elif estado_velocidad == "desacelerando":
            lectura_flow = base_flow + "\nPresión bajista perdiendo velocidad: posible absorción cerca."
        else:
            lectura_flow = base_flow + "\nNueva presión bajista."

    else:
        lectura_flow = (
            "⚪ Mercado descargando posiciones\n"
            "Precio ↓ + Open Interest ↓\n"
            "Menor participación"
        )

    st.info(lectura_flow)

# ----------------------------------
# RESUMEN TEXTUAL DE NIVELES (detalle técnico + R:R sugerido)
# Complementa el overlay visual: el gráfico muestra TODOS los niveles
# de un vistazo, esta sección da el detalle completo de cada uno
# (variables, distancia, rol) para quien quiera leerlo en texto.
# ----------------------------------

st.subheader(
    "📋 Detalle de niveles activos",
    help=(
        "Estos niveles vienen del mercado de OPCIONES de BTC (Deribit), no de "
        "las velas de precio. Reflejan dónde los grandes operadores de opciones "
        "tienen posiciones concentradas, lo que suele generar zonas de soporte, "
        "resistencia o 'imán' de precio. Que un nivel actúe como tal no garantiza "
        "rebote: una ruptura confirmada también es un escenario válido."
    ),
)

if not deribit_disponible:
    st.warning(
        "⚠️ No se pudo conectar a la API de Deribit en este refresh. "
        "El detalle de Flip/Walls/Gamma Zone no está disponible momentáneamente."
    )
else:

    col_w1, col_w2 = st.columns(2)

    with col_w1:

        st.subheader(
            "🧱 Call Wall (Resistencia / zona magnética)",
            help=(
                "El strike de CALLS con más Open Interest abierto. Suele actuar "
                "como techo: cuando el precio se acerca, los vendedores de esas "
                "opciones (dealers) suelen vender futuros/spot para cubrirse, "
                "lo que frena la suba. Si el precio lo rompe con fuerza, puede "
                "acelerar hacia arriba (los dealers tienen que comprar para cubrirse)."
            ),
        )

        if call_wall:
            cambio_txt = (
                f"{call_wall_oi_cambio:+.1f}% (≈2.5 min)"
                if call_wall_oi_cambio is not None
                else "sin historial suficiente todavía"
            )
            st.info(
                f"""
**Strike:** ${call_wall['strike']:,.0f}
**OI:** {call_wall['oi']:,.0f} contratos
**Cambio de OI:** {cambio_txt}
**Gamma asociada:** {call_wall['gamma']:.8f}
**Distancia al precio actual:** {call_wall['distancia_pct']:+.2f}%
**Rol:** {call_wall['rol']}
"""
            )
        else:
            st.caption("Sin datos suficientes de calls en Deribit para esta ventana.")

    with col_w2:

        st.subheader(
            "🧱 Put Wall (Soporte / zona defensiva)",
            help=(
                "El strike de PUTS con más Open Interest abierto. Suele actuar "
                "como piso: cuando el precio cae hacia ahí, los vendedores de "
                "esas opciones suelen comprar futuros/spot para cubrirse, lo que "
                "frena la caída. Si el precio lo rompe con fuerza hacia abajo, "
                "puede acelerar la baja (los dealers tienen que vender para cubrirse)."
            ),
        )

        if put_wall:
            cambio_txt = (
                f"{put_wall_oi_cambio:+.1f}% (≈2.5 min)"
                if put_wall_oi_cambio is not None
                else "sin historial suficiente todavía"
            )
            st.info(
                f"""
**Strike:** ${put_wall['strike']:,.0f}
**OI:** {put_wall['oi']:,.0f} contratos
**Cambio de OI:** {cambio_txt}
**Gamma asociada:** {put_wall['gamma']:.8f}
**Distancia al precio actual:** {put_wall['distancia_pct']:+.2f}%
**Rol:** {put_wall['rol']}
"""
            )
        else:
            st.caption("Sin datos suficientes de puts en Deribit para esta ventana.")

    col_f1, col_f2 = st.columns(2)

    with col_f1:

        st.subheader(
            "🔁 Flip Semanal (Largo Plazo)",
            help=(
                "El precio donde, sumando varios vencimientos de opciones, el "
                "mercado pasa de 'Short Gamma' a 'Long Gamma' (o viceversa). En "
                "Long Gamma, los dealers tienden a comprar caídas y vender subas, "
                "lo que comprime el rango de precio. En Short Gamma, hacen lo "
                "contrario, lo que puede amplificar los movimientos. Pensado "
                "para una visión más de varios días, no de las próximas horas."
            ),
        )

        if resultado_flip_global and resultado_flip_global["flip_point"]:
            fp_global = resultado_flip_global["flip_point"]
            dist_global = ((fp_global - precio_actual) / precio_actual) * 100
            gex_spot_global = resultado_flip_global["gex_spot"]
            contexto_global = "🟢 Long Gamma" if gex_spot_global > 0 else "🔴 Short Gamma"
            st.info(
                f"""
**Flip Point:** ${fp_global:,.0f} ({dist_global:+.2f}% desde el spot)
**Régimen actual:** {contexto_global}
**Vencimientos agregados:** {len(vencimientos_global) if deribit_disponible else 0}
"""
            )
        else:
            st.caption("No se detectó cruce de signo dentro de ±15% en los vencimientos agregados.")

    with col_f2:

        st.subheader(
            "🔁 Flip Cercano (Corto Plazo)",
            help=(
                "Lo mismo que el Flip Semanal, pero calculado solo con el "
                "vencimiento de opciones más próximo en el calendario. Reacciona "
                "más rápido a cambios de posicionamiento de corto plazo — más "
                "relevante para operativa intradiaria o de Scalp que el Flip Semanal."
            ),
        )

        if resultado_flip_local and resultado_flip_local["flip_point"]:
            fp_local = resultado_flip_local["flip_point"]
            dist_local = ((fp_local - precio_actual) / precio_actual) * 100
            gex_spot_local = resultado_flip_local["gex_spot"]
            contexto_local = "🟢 Long Gamma" if gex_spot_local > 0 else "🔴 Short Gamma"
            lado_local = "🟢 defendido como soporte (comprador)" if fp_local < precio_actual else "🔴 defendido como resistencia (vendedor)"
            venc_txt = vencimiento_local_dt.strftime("%d-%b-%Y") if vencimiento_local_dt else "N/D"
            st.info(
                f"""
**Flip Point:** ${fp_local:,.0f} ({dist_local:+.2f}% desde el spot)
**Régimen actual:** {contexto_local}
**Lado dominante:** {lado_local}
**Vencimiento usado:** {venc_txt}
"""
            )
        else:
            st.caption("No se detectó cruce de signo dentro de ±15% en el vencimiento más próximo.")

    st.caption(
        "⚠️ Nota sobre soporte/resistencia: que un nivel actúe como tal no garantiza "
        "rebote — una ruptura confirmada (aceptación de precio del otro lado) también "
        "es un escenario válido y suele habilitar continuación. Todo nivel debe leerse "
        "después de su aceptación, no antes."
    )

    with st.expander("Ver curva de GEX vs precio hipotético (Flip Global)"):

        st.markdown(
            "**🧭 Cómo leer este gráfico:** el eje horizontal es un precio "
            "hipotético de BTC (no el tiempo). El eje vertical es el GEX total "
            "que *habría* a ese precio. Donde la línea está **por encima de "
            "cero**: régimen Long Gamma (rango más comprimido, los dealers "
            "amortiguan el movimiento). **Por debajo de cero**: régimen Short "
            "Gamma (movimientos más amplificados). El punto exacto donde la "
            "línea cruza el cero es el **Flip Point** — no es un soporte ni "
            "una resistencia clásica, es un cambio de *comportamiento* del "
            "mercado de opciones, no del precio en sí."
        )

        if resultado_flip_global:
            curva = resultado_flip_global["curva"]
            precios_curva = [p for p, _ in curva]
            gex_curva = [g for _, g in curva]

            fig_gamma = go.Figure()
            fig_gamma.add_trace(
                go.Scatter(x=precios_curva, y=gex_curva, mode="lines", name="GEX")
            )
            fig_gamma.add_hline(y=0, line_dash="dash", line_color="gray")
            fig_gamma.add_vline(
                x=precio_actual, line_dash="dot", line_color="yellow",
                annotation_text="Precio actual"
            )
            if resultado_flip_global["flip_point"]:
                fig_gamma.add_vline(
                    x=resultado_flip_global["flip_point"], line_dash="dot", line_color="red",
                    annotation_text="Flip Global"
                )
            fig_gamma.update_layout(
                height=350,
                xaxis_title="Precio hipotético BTC (USD)",
                yaxis_title="GEX total"
            )
            st.plotly_chart(fig_gamma, use_container_width=True)

        st.caption(
            "⚠️ Convención estándar de mercado: asume dealers long calls / "
            "short puts en neto. No es un dato confirmado de posicionamiento real, "
            "sino la lectura habitual usada en este tipo de indicadores de GEX.\n\n"
            "⚠️ Modelo Sticky Strike: la grilla usa el IV actual de cada strike "
            "fijo en todos los precios hipotéticos simulados, sin ajustar por skew. "
            "En una caída fuerte real, las puts OTM suelen encarecerse (su IV sube), "
            "lo cual no está modelado acá — esto puede desplazar el Flip Point "
            "calculado levemente hacia abajo respecto al que realmente se observaría "
            "en momentos de alta volatilidad. Es una simplificación consciente, no un bug."
        )

st.divider()
st.subheader(
    "🧲 Niveles Imán (liquidez) y 🧊 Absorción — resumen rápido",
    help=(
        "Niveles Imán: zonas donde el precio ya reaccionó antes (máximos/mínimos "
        "locales en las velas), no vienen de opciones. El switch 'Filtrar ruido "
        "de Liquidez' más arriba afecta TANTO a estos niveles como a los que "
        "se dibujan en el gráfico.\n\n"
        "Absorción: ver el tooltip específico más abajo, en esa columna."
    ),
)

col_liq1, col_liq2 = st.columns(2)

with col_liq1:

    if soportes:
        st.caption(f"🟩 Imán soporte más cercano: ${soportes[0]:,.1f}")
    else:
        st.caption("🟩 Sin soporte Imán detectado en la ventana")

    if resistencias:
        st.caption(f"🟥 Imán resistencia más cercana: ${resistencias[0]:,.1f}")
    else:
        st.caption("🟥 Sin resistencia Imán detectada en la ventana")

    nivel_cercano = nivel_mas_cercano(precio_actual, soportes, resistencias)

    if nivel_cercano:
        tipo, valor, dist = nivel_cercano

        # R:R sugerido orientativo: riesgo = distancia al nivel cercano,
        # beneficio = distancia al SIGUIENTE nivel imán en la misma
        # dirección (si existe). Puramente orientativo, no es consejo
        # financiero — la gestión de riesgo final es del usuario.
        siguiente = None
        if tipo == "soporte" and len(soportes) > 1:
            siguiente = soportes[1]
        elif tipo == "resistencia" and len(resistencias) > 1:
            siguiente = resistencias[1]

        rr = calcular_rr_sugerido(precio_actual, valor, tipo, siguiente)
        rr_txt = f" · R:R orientativo ≈ {rr}:1" if rr else ""

        if dist < 0.15:
            st.warning(f"⚠️ Precio MUY cerca de {tipo} (${valor:,.1f}, {dist:.2f}%){rr_txt} — zona de reacción probable, pero la ruptura también es válida tras aceptación.")
        else:
            st.info(f"Nivel más relevante: {tipo} a ${valor:,.1f} ({dist:.2f}% de distancia){rr_txt}")

with col_liq2:

    st.markdown(
        "**🧊 Absorción**",
        help=(
            "Busca velas con volumen muy alto pero rango de precio chico: mucha "
            "actividad entrando sin que el precio avance, lo que sugiere que "
            "alguien grande está absorbiendo el flujo (comportamiento típico de "
            "un dealer defendiendo un nivel). El 'score' indica qué tan cerca "
            "está la señal de confirmarse, no qué tan fuerte es el movimiento. "
            "Es un proxy basado en velas — no reemplaza ver el book real (Level 2). "
            "En el gráfico principal, la capa 🧊 ABSORB marca un CANDIDATO de "
            "dónde podría ocurrir la próxima absorción (en la dirección de la "
            "tendencia actual, sobre el nivel Imán más cercano), no un hecho ya "
            "confirmado."
        ),
    )

    if detalle_absorcion:

        score = detalle_absorcion["score"]

        if hay_absorcion:
            st.success(
                f"🧊 Absorción detectada (score {score}/100)\n\n"
                f"Volumen {detalle_absorcion['ratio_volumen']}x el promedio, "
                f"con rango de precio {detalle_absorcion['ratio_rango']}x el promedio.\n\n"
                f"➡️ Mucho volumen entrando y el precio casi no se mueve: "
                f"alguien grande puede estar absorbiendo el flujo sin dejar avanzar el precio. "
                f"Pensado sobre todo para detectar oportunidades de scalp pequeño."
            )
        elif score >= 70:
            st.warning(
                f"🟡 Absorción débil / en formación (score {score}/100)\n\n"
                f"Volumen {detalle_absorcion['ratio_volumen']}x promedio "
                f"(dispara en 1.3x) · Rango {detalle_absorcion['ratio_rango']}x promedio "
                f"(dispara en ≤0.75x)."
            )
        else:
            st.caption(
                f"⚪ Sin absorción (score {score}/100)\n\n"
                f"Volumen {detalle_absorcion['ratio_volumen']}x promedio "
                f"(dispara en 1.3x) · Rango {detalle_absorcion['ratio_rango']}x promedio "
                f"(dispara en ≤0.75x)."
            )

        st.progress(score / 100)

    else:
        st.caption("Datos insuficientes para evaluar absorción en esta ventana.")

    st.caption(
        "⚠️ Proxy basado en velas (volumen alto + rango chico). "
        "No reemplaza confirmación con datos de book (Level 2)."
    )


# MARKET INTELLIGENCE V2
# ----------------------------------

st.divider()

st.subheader("🧠 Market Intelligence")

institucional_score = 0
retail_score = 0

# OI
if oi_disponible:
    if oi_valor > 90000:
        institucional_score += 30
    else:
        retail_score += 10

# FUNDING
if funding_disponible:
    if funding_valor > 0:
        retail_score += 15
    elif funding_valor < 0:
        institucional_score += 15

# FLOW
if buy_pressure > sell_pressure:
    institucional_score += 25
else:
    retail_score += 25

# TEMPORALIDADES
# FIX: reutiliza tendencia_5m/15m/1h ya calculadas arriba (antes se
# repetía el fetch con otros nombres de variable: tf_5, tf_15, tf_1h)
if "Alcista" in tendencia_1h:
    institucional_score += 30
elif "Bajista" in tendencia_1h:
    retail_score += 30

# RESULTADOS

col1, col2 = st.columns(2)

with col1:
    st.metric("🏦 Institucional", f"{institucional_score}%")

with col2:
    st.metric("👤 Retail", f"{retail_score}%")

st.write("")

# CONTROL

if institucional_score > retail_score:
    control = "🏦 Institucional"
    sesgo = "🟢 Comprador"

elif retail_score > institucional_score:
    control = "👤 Retail"
    sesgo = "🔴 Vendedor"

else:
    control = "⚖️ Equilibrio"
    sesgo = "🟡 Neutral"

st.info(
    f"""
Control actual:
{control}

Sesgo:
{sesgo}
"""
)

# -----------------------------
# CEREBRO GENERAL COPILOT
# -----------------------------

if modo == "Scalp":

    # FIX: la lectura Scalp anterior solo combinaba presión + cambio_oi
    # (las mismas señales del panel Presión/Flow). Ahora incorpora
    # absorción y cercanía a niveles de liquidez, que es la metodología
    # real del usuario: buscar ruptura de liquidez con continuación,
    # o absorción de liquidez con rebote.

    cerca_de_nivel = nivel_mas_cercano(precio_actual, soportes, resistencias)
    en_zona_relevante = cerca_de_nivel is not None and cerca_de_nivel[2] < 0.15

    if hay_absorcion and en_zona_relevante:
        tipo_nivel = cerca_de_nivel[0]
        lectura = (
            f"⚡ Scalp: absorción detectada justo en zona de {tipo_nivel} "
            f"(${cerca_de_nivel[1]:,.1f}). Escenario de posible rebote — "
            "vigilar reacción antes de operar a favor de la ruptura."
        )

    elif en_zona_relevante and estado_velocidad == "acelerando" and not hay_absorcion:
        tipo_nivel = cerca_de_nivel[0]
        lectura = (
            f"⚡ Scalp: precio acelerando contra {tipo_nivel} "
            f"(${cerca_de_nivel[1]:,.1f}) sin absorción visible. "
            "Escenario de posible ruptura con continuación."
        )

    elif buy_pressure > 60 and cambio_oi_flow > 0 and estado_velocidad != "desacelerando":
        lectura = (
            "⚡ Scalp: impulso comprador con entrada de participación, "
            "sin señales de agotamiento. Buscar confirmación en microestructura."
        )

    elif sell_pressure > 60 and cambio_oi_flow > 0 and estado_velocidad != "desacelerando":
        lectura = (
            "⚡ Scalp: presión vendedora con construcción de posiciones, "
            "sin señales de agotamiento. Atención a continuación bajista."
        )

    elif estado_velocidad == "desacelerando":
        lectura = (
            "⚡ Scalp: impulso perdiendo velocidad. "
            "Posible agotamiento — evaluar absorción antes de seguir la dirección actual."
        )

    elif cambio_oi_flow < 0:
        lectura = (
            "⚡ Scalp: descarga de posiciones. "
            "Movimiento perdiendo participación."
        )

    else:
        lectura = "⚡ Scalp: equilibrio. Esperando expansión de volatilidad."

else:

    if institucional_score > retail_score:
        lectura = (
            "🧠 Normal: control institucional dominante. "
            "Analizando continuidad o absorción."
        )

    elif retail_score > institucional_score:
        lectura = (
            "🧠 Normal: presión retail predominante. "
            "Evaluar posibles trampas."
        )

    else:
        lectura = "🧠 Normal: mercado equilibrado esperando confirmación."

st.caption(f"📌 Lectura: {lectura}")

st.divider()
st.caption(
    "⚠️ **Aviso importante:** este dashboard combina datos de mercado (Binance, "
    "Deribit) con cálculos e inferencias propias (Dealer Score, Flip Points, "
    "Walls, niveles Imán, candidatos de absorción, lecturas de Scalp/Normal). "
    "Ninguna lectura, métrica o 'candidato' mostrado en esta página constituye "
    "una recomendación de inversión ni una señal de entrada o salida. Toda "
    "sugerencia o interpretación que pueda desprenderse de estos datos queda "
    "sujeta a la validación y aprobación propia de cada usuario, considerando "
    "siempre la confirmación real del mercado antes de actuar — los niveles "
    "proyectados (Flip, Walls, Imán, Absorción) son zonas de mayor probabilidad "
    "estadística, no garantías de reacción del precio."
)
