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
import market_depth as md
import market_bias as mb

# ----------------------------------
# CONFIG
# ----------------------------------

st.set_page_config(
    page_title="BTC Copilot by JONFLOW-MDQ",
    page_icon="📈",
    layout="wide"
)

# Versión del build, mostrada en letra chica junto a la fecha de
# última actualización (ver más abajo, cerca del gráfico principal).
# IMPORTANTE: estas dos constantes NO se recalculan solas con cada
# refresh de 15s — son un changelog manual. Subí VERSION_APP y
# actualizá FECHA_ULTIMA_ACTUALIZACION a mano cada vez que el CÓDIGO
# cambie (nueva capa, fix, ajuste de UI), no cada vez que llega un
# dato nuevo de Binance/Deribit.
VERSION_APP = "V 0.0.9"
FECHA_ULTIMA_ACTUALIZACION = "09/07/2026"  # dd/mm/aaaa — actualizar a mano en cada deploy

# ----------------------------------
# REFRESH DINÁMICO AL ARRANQUE
# ----------------------------------
#
# Problema observado: al abrir la app por primera vez (sesión nueva),
# el proxy de Render puede estar "dormido" (free tier) y tarda varios
# segundos en responder -> el primer ciclo muestra un error técnico
# crudo (timeout) en vez de simplemente avisar "conectando".
#
# Fix: durante los primeros ciclos de la sesión, refrescamos más
# rápido (5s en vez de 15s) para no hacer esperar al usuario los 15s
# completos mientras el proxy todavía está despertando. Una vez que
# se logra un ciclo exitoso (o se agota el margen de "arranque"),
# volvemos al intervalo normal de 15s.
#
# CICLOS_ARRANQUE: cuántos refreshes rápidos toleramos antes de pasar
# a tratar un fallo como error real (no como "todavía conectando").
#
# FIX (recalibración tras bans -1003 repetidos): antes eran 4 ciclos a
# 5s -- si varias sesiones nuevas (varios testers, o varias pestañas)
# entraban en esta ventana al mismo tiempo, la ráfaga conjunta sobre
# la misma IP del proxy alcanzaba para gatillar el ban de peso incluso
# con el cache del proxy ya puesto. Bajado a 2 ciclos a 8s: sigue
# resolviendo el caso real que motivó esto (proxy de Render dormido
# tarda en responder al abrir la app por primera vez) sin necesitar
# refrescos tan agresivos.
CICLOS_ARRANQUE = 2

if "ciclos_transcurridos" not in st.session_state:
    st.session_state.ciclos_transcurridos = 0

en_periodo_arranque = st.session_state.ciclos_transcurridos < CICLOS_ARRANQUE

intervalo_refresh = 8000 if en_periodo_arranque else 15000

st_autorefresh(interval=intervalo_refresh, key="btc_refresh")

st.session_state.ciclos_transcurridos += 1

# ----------------------------------
# PROXY (Render) — Binance bloquea la IP de Streamlit Cloud, así que
# todas las consultas a Binance pasan por nuestro proxy propio en
# Render, que corre en una región no bloqueada.
# ----------------------------------

PROXY_URL = "https://btccopilot-beta1-0-1.onrender.com"

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


def mostrar_estado_no_disponible(detalle_tecnico, contexto=""):
    """
    Decide qué mostrarle al usuario cuando falla un fetch (ticker o
    velas): durante el período de arranque de la sesión (los primeros
    CICLOS_ARRANQUE refreshes), el proxy de Render puede estar
    despertando -> mostramos un mensaje neutro de "conectando", sin
    traceback. Pasado ese margen, si SIGUE fallando, ahí sí mostramos
    el detalle técnico real, porque a esa altura es información útil
    (no es solo arranque) y no queremos ocultar un problema genuino.
    """

    if en_periodo_arranque:
        st.info(
            "🔄 Conectando con el mercado...\n\n"
            "Esto puede tardar unos segundos la primera vez que se abre la página."
        )
    else:
        st.error(
            f"⚠️ {contexto}\n\n"
            f"Detalle: {detalle_tecnico}\n\n"
            f"Puede ser un problema temporal de la API o del proxy. "
            f"Se va a reintentar automáticamente en 15 segundos."
        )

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

st.title("📈 BTC Copilot by JonFlow-MDQ")

tab_dashboard, tab_opciones, tab_profundidad = st.tabs(
    ["📊 Dashboard", "📐 Opciones / Derivados", "🌊 Profundidad de Mercado"]
)

with tab_dashboard:

    # ----------------------------------
    # MODO OPERATIVO
    # ----------------------------------

    if "modo" not in st.session_state:
        st.session_state.modo = "Normal"

    if "timeframe" not in st.session_state:
        st.session_state.timeframe = "15m"

    if "oi_historial" not in st.session_state:
        st.session_state.oi_historial = []


    c_normal, c_scalp, c_microscalp = st.columns(3)

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
                "Pensado para operativa de corto plazo. Las mismas temporalidades "
                "(5M/15M/1H) se traducen a velas más chicas (1M/3M/5M) para reaccionar "
                "más rápido. Prioriza microflujo, presión inmediata y reacción del "
                "precio sobre niveles cercanos — usá el Flip Cercano (Local) y la "
                "capa ABSORB del gráfico como referencia principal en este modo."
            ),
        ):
            st.session_state.modo = "Scalp"
            st.rerun()

    with c_microscalp:
        if st.button(
            "🔬 Microscalp",
            help=(
                "Pensado para operativa de segundos-a-minutos: SOLO velas de 1M, "
                "con ventana de análisis más corta (menos velas, swings más chicos) "
                "y el Flip Cercano recalculado en un rango mucho más angosto "
                "(±0.8% en vez de ±1.8%). Ignora el selector 5M/15M/1H — siempre "
                "opera sobre 1M. Prioriza reacción inmediata: usalo solo si tu "
                "gestión de riesgo trabaja con stops chicos (~100-180 USD)."
            ),
        ):
            st.session_state.modo = "Microscalp"
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

    elif modo == "Microscalp":
        # Microscalp ignora el selector 5M/15M/1H a propósito: siempre
        # trabaja sobre 1M puro, con ventana ultra corta (ver más abajo,
        # límite de velas y ventana de swing recalibrados para este modo).
        temporalidad_analizada = "🔬1M ULTRA"
        data_timeframe = "1m"

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

def obtener_ticker():
    """
    Pide el ticker 24hs a través de nuestro proxy en Render (en vez de
    pegarle directo a Binance), porque Binance bloquea la IP de
    Streamlit Cloud. El proxy corre en una región no bloqueada y
    reenvía la consulta, probando varios dominios de Binance.
    Devuelve el dict de Binance, o un dict con 'error' si falla.
    """

    url = f"{PROXY_URL}/ticker24hr?symbol=BTCUSDT"

    try:
        respuesta = requests.get(url, timeout=10)
        cuerpo = respuesta.json()
        if isinstance(cuerpo, dict) and "lastPrice" in cuerpo:
            return cuerpo
        msg = cuerpo.get("error", str(cuerpo)) if isinstance(cuerpo, dict) else "Respuesta inesperada del proxy"
        return {"error": msg}
    except Exception as e:
        return {"error": str(e)}


def obtener_velas(intervalo, limite=100):
    """
    Descarga velas a través de nuestro proxy en Render (ver
    obtener_ticker). Devuelve un DataFrame con la estructura esperada,
    o un DataFrame VACÍO (mismas columnas, 0 filas) si el pedido
    falla — nunca lanza una excepción hacia afuera, para que el resto
    del dashboard pueda mostrar un aviso claro en vez de un traceback
    ilegible.
    """

    columnas = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_asset_volume", "number_of_trades",
        "taker_buy_base", "taker_buy_quote", "ignore"
    ]

    url = (
        f"{PROXY_URL}/klines"
        f"?symbol=BTCUSDT&interval={intervalo}&limit={limite}"
    )

    datos = None
    ultimo_error = None

    try:
        respuesta = requests.get(url, timeout=10)
        cuerpo = respuesta.json()

        # El proxy/Binance devuelve un dict con "error"/"msg" cuando
        # hay un problema, en vez de la lista de velas esperada.
        if isinstance(cuerpo, dict):
            ultimo_error = cuerpo.get("error", cuerpo.get("msg", str(cuerpo)))
        elif not cuerpo:  # lista vacía
            ultimo_error = "Respuesta vacía del servidor"
        else:
            datos = cuerpo

    except Exception as e:
        ultimo_error = str(e)

    if datos is None:
        # El pedido falló: devolvemos DataFrame vacío con la
        # estructura correcta, y guardamos el error en session_state
        # para que el dashboard pueda avisar sin romper la ejecución.
        st.session_state["error_binance_velas"] = (
            f"No se pudo obtener velas vía proxy ({intervalo}). "
            f"Último error: {ultimo_error}"
        )
        return pd.DataFrame(columns=columnas)

    df = pd.DataFrame(datos, columns=columnas)

    df["open_time"] = pd.to_datetime(
        df["open_time"],
        unit="ms"
    )

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
    """Pide el funding rate de Binance Futures vía nuestro proxy en Render."""
    try:
        url = f"{PROXY_URL}/premiumIndex?symbol=BTCUSDT"
        respuesta = requests.get(url, timeout=10)
        data = respuesta.json()
        if "lastFundingRate" not in data:
            st.session_state["error_funding"] = data.get("error", str(data))
            return None
        return float(data["lastFundingRate"]) * 100
    except Exception as e:
        st.session_state["error_funding"] = str(e)
        return None
    

def obtener_open_interest():
    """Pide el Open Interest de Binance Futures vía nuestro proxy en Render."""

    try:
        url = f"{PROXY_URL}/openInterest?symbol=BTCUSDT"
        data = requests.get(url, timeout=10).json()
        return float(data["openInterest"])

    except Exception:
        return None
    
def obtener_open_interest_bybit():
    """
    Obtiene Open Interest de Bybit (suele ser más reactivo) a través
    de nuestro proxy en Render (ver obtener_ticker) — Bybit bloquea
    geográficamente vía CloudFront el acceso directo desde Streamlit
    Cloud (mismo problema que ya tenemos con Binance), así que esto
    NO se puede pedir directo, tiene que pasar por el proxy.
    """
    try:
        url = f"{PROXY_URL}/bybit/openInterest?symbol=BTCUSDT&intervalTime=5min"
        respuesta = requests.get(url, timeout=10)

        if respuesta.status_code != 200:
            st.session_state["bybit_error"] = (
                f"HTTP {respuesta.status_code} desde el proxy. "
                f"Respuesta cruda: {respuesta.text[:200]}"
            )
            return None

        try:
            data = respuesta.json()
        except ValueError:
            st.session_state["bybit_error"] = (
                f"El proxy no devolvió JSON. Respuesta cruda: {respuesta.text[:200]}"
            )
            return None

        if data.get("retCode") == 0 and data.get("result", {}).get("list"):
            oi = float(data["result"]["list"][0]["openInterest"])
            return oi
        else:
            st.session_state["bybit_error"] = f"Bybit retCode: {data.get('retCode')} - Msg: {data.get('retMsg')}"
            return None

    except Exception as e:
        st.session_state["bybit_error"] = str(e)
        return None
        
with tab_dashboard:


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


def _d1_d2_black_scholes(spot, strike, vol_anual, dias_a_vencimiento, tasa=0.0):
    """
    Calcula d1 y d2 de Black-Scholes una sola vez, para que Delta/Vega/
    Theta (que todos dependen de d1 y/o d2) no repitan el mismo cálculo
    de log/sqrt cada uno por separado. Devuelve (None, None) si los
    parámetros no son válidos (vencido, vol/spot/strike <= 0).
    """

    if dias_a_vencimiento <= 0 or vol_anual <= 0 or spot <= 0 or strike <= 0:
        return None, None

    t = dias_a_vencimiento / 365.0

    try:
        d1 = (
            math.log(spot / strike) + (tasa + 0.5 * vol_anual ** 2) * t
        ) / (vol_anual * math.sqrt(t))
        d2 = d1 - vol_anual * math.sqrt(t)
    except (ValueError, ZeroDivisionError):
        return None, None

    return d1, d2


def _norm_cdf(x):
    """Función de distribución acumulada normal estándar, vía erf (sin scipy)."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2)))


def _delta_black_scholes(spot, strike, vol_anual, dias_a_vencimiento, tipo, tasa=0.0):
    """
    Delta de Black-Scholes: sensibilidad del precio de la opción ante
    un cambio de 1 USD en el spot. Para CALL va de 0 a 1, para PUT de
    -1 a 0. Lectura de mercado estándar: |delta| se usa como proxy de
    "probabilidad implícita" de terminar in-the-money al vencimiento
    (no es una probabilidad real bajo medida física, es la que implica
    el modelo bajo medida neutral al riesgo — la lectura habitual en
    mesas de opciones, no una garantía estadística).
    """

    d1, _ = _d1_d2_black_scholes(spot, strike, vol_anual, dias_a_vencimiento, tasa)
    if d1 is None:
        return 0.0

    if tipo == "call":
        return _norm_cdf(d1)
    else:
        return _norm_cdf(d1) - 1.0


def _vega_black_scholes(spot, strike, vol_anual, dias_a_vencimiento, tasa=0.0):
    """
    Vega de Black-Scholes (igual para call y put al mismo strike):
    cambio en el precio de la opción por cada 1 punto porcentual
    (0.01) de cambio en la volatilidad implícita anualizada. Se
    devuelve ya escalado a "por 1 punto de IV" (estándar de mesas:
    vega/100), no a "por 100% de IV".
    """

    d1, _ = _d1_d2_black_scholes(spot, strike, vol_anual, dias_a_vencimiento, tasa)
    if d1 is None:
        return 0.0

    t = dias_a_vencimiento / 365.0
    vega_completo = spot * _norm_pdf(d1) * math.sqrt(t)
    return vega_completo / 100.0


def _theta_black_scholes(spot, strike, vol_anual, dias_a_vencimiento, tipo, tasa=0.0):
    """
    Theta de Black-Scholes: cuánto valor pierde la opción por el paso
    de 1 día calendario, manteniendo todo lo demás constante (spot,
    IV) — el "alquiler" que paga el comprador de la opción cada día.
    Se devuelve ya escalado a "por día" (theta_anual / 365), no a
    "por año", que es como sale crudo de la fórmula clásica.

    Asume tasa libre de riesgo ≈ 0 (mismo criterio que el resto del
    módulo de GEX), lo cual simplifica el término de costo de
    financiamiento de la fórmula completa sin alterar la lectura
    cualitativa del decaimiento.
    """

    d1, d2 = _d1_d2_black_scholes(spot, strike, vol_anual, dias_a_vencimiento, tasa)
    if d1 is None:
        return 0.0

    t = dias_a_vencimiento / 365.0

    termino_comun = -(spot * _norm_pdf(d1) * vol_anual) / (2 * math.sqrt(t))

    if tipo == "call":
        theta_anual = termino_comun - tasa * strike * _norm_cdf(d2)
    else:
        theta_anual = termino_comun + tasa * strike * _norm_cdf(-d2)

    return theta_anual / 365.0


def _lectura_decaimiento(dias_a_vencimiento, oi, magnetismo_relativo):
    """
    Clasificación cualitativa de cómo se espera que decaiga la
    relevancia de un strike hacia su vencimiento. Esto NO es una
    predicción de movimiento de precio ni un timing de ruptura — es
    una descripción de cómo decae el propio contrato de opción con el
    paso del tiempo (hecho matemático del modelo) combinada con qué
    tan cargado está ESE strike en relación a los demás del mismo lado
    (magnetismo relativo) — no una inferencia sobre hacia dónde va a
    moverse el precio de BTC.

    FIX (detectado por el usuario): la versión anterior solo miraba
    días a vencimiento; el OI/gamma entraban en la fórmula pero se
    cancelaban algebraicamente (abs(theta)/oi*oi == abs(theta)), por
    eso dos strikes con los mismos días daban SIEMPRE la misma lectura
    sin importar cuánto OI/score tuvieran. Ahora el magnetismo relativo
    (score del strike / score máximo del lado, 0 a 1) sí cambia el
    resultado: un strike muy cargado (cerca del máximo de su lado)
    sostiene su lectura un escalón más "estable" que uno casi vacío con
    los mismos días restantes, porque hace falta más flujo para mover
    una posición grande que una chica.

    magnetismo_relativo: score_strike / score_máximo_del_lado (0 a 1).
    Se calcula afuera (ver _calcular_score_strikes), porque requiere
    conocer el score de TODOS los strikes del lado, no solo el propio.

    Categorías (umbrales orientativos, pensados para opciones de BTC
    con strikes cercanos al dinero en vencimientos semanales/cortos):
      - "estable": muchos días restantes, o pocos días pero con
        magnetismo alto (posición grande, tarda más en perder peso).
      - "moderado": ventana de tiempo intermedia con magnetismo medio,
        o pocos días con magnetismo bajo-medio.
      - "acelerado": pocos días Y magnetismo bajo -> posición chica
        cerca de vencer, pierde relevancia rápido y es la primera que
        los dealers dejan de defender.
    """

    if dias_a_vencimiento <= 0 or oi <= 0:
        return "sin datos suficientes"

    # Score combinado: días restantes (normalizado a una ventana de 21
    # días, el máximo que usa el Flip Global) + magnetismo relativo.
    # Ambos pesan por igual -> un strike puede compensar pocos días con
    # mucho magnetismo, o viceversa.
    factor_tiempo = min(dias_a_vencimiento / 21.0, 1.0)  # 0 (vence ya) a 1 (21+ días)
    indice_combinado = (factor_tiempo + magnetismo_relativo) / 2.0  # 0 a 1

    if indice_combinado >= 0.55:
        return (
            f"estable: magnetismo relativo {magnetismo_relativo*100:.0f}% del máximo de su lado "
            f"con {dias_a_vencimiento:.1f} días restantes — posición lo bastante cargada o con "
            f"tiempo suficiente para sostener su peso."
        )
    elif indice_combinado >= 0.25:
        return (
            f"moderado: magnetismo relativo {magnetismo_relativo*100:.0f}% del máximo de su lado "
            f"con {dias_a_vencimiento:.1f} días restantes — decaimiento gradual, ni de los más "
            f"sostenidos ni de los primeros en perder peso."
        )
    else:
        return (
            f"acelerado: magnetismo relativo {magnetismo_relativo*100:.0f}% del máximo de su lado "
            f"con {dias_a_vencimiento:.1f} días restantes — posición chica y/o cerca de vencer, "
            f"de las primeras que los dealers dejarían de defender."
        )


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


def _calcular_score_strikes(instrumentos, tipo, spot_actual, ahora):
    """
    Calcula, para cada strike de un tipo (call/put), un SCORE combinado
    en vez de usar solo OI bruto. Esto es lo que evita que un strike
    lejano (ej. vencimiento trimestral con mucho OI acumulado) gane la
    wall por puro volumen histórico, aunque no tenga relevancia real
    para el comportamiento de corto plazo de los dealers.

    score(strike) = OI * gamma * peso_tiempo * peso_distancia

    - gamma: gamma de Black-Scholes en ESE strike, al spot actual. Un
      strike muy lejano en precio o tiempo tiene gamma casi nula, así
      que aporta poco al score aunque tenga mucho OI.
    - peso_tiempo = 1/sqrt(días a vencimiento): un contrato por vencer
      esta semana pesa mucho más que uno que vence en 2-3 meses.
    - peso_distancia: castiga progresivamente los strikes lejos del
      spot. Usamos un decaimiento exponencial simple sobre la distancia
      %, para que la wall relevante quede dentro de un rango operable
      (intradiario/scalp), no a decenas de miles de dólares.

    Para la gamma/distancia usamos, por strike, el instrumento de
    vencimiento más próximo disponible en ese strike (igual criterio
    que antes: la wall suele estar dominada por el vencimiento más
    cercano/líquido).

    AMPLIACIÓN (Delta/Vega/Theta + lectura de decaimiento): además del
    score y la gamma del tipo pedido (`tipo`), cada strike devuelve
    también los Greeks del LADO OPUESTO, calculados sobre el mismo
    instrumento de referencia (vencimiento más próximo en ese strike,
    misma IV). Esto es lo que permite que la tabla muestre, para un
    mismo strike, tanto la lectura call como la lectura put sin tener
    que recorrer la lista de instrumentos dos veces. El campo
    "lectura_decaimiento" es texto descriptivo (ver _lectura_decaimiento),
    no una predicción de movimiento de precio.

    Devuelve dict {strike: {...}} con, por strike:
      score, oi, gamma, distancia_pct (del tipo pedido),
      delta, vega, theta (del tipo pedido),
      delta_otro_lado, vega_otro_lado, theta_otro_lado, gamma_otro_lado,
      dias_a_vencimiento, lectura_decaimiento
    """

    por_strike = {}

    for inst in instrumentos:
        if inst["tipo"] != tipo:
            continue

        dias = (inst["vencimiento"] - ahora).total_seconds() / 86400.0
        if dias <= 0:
            continue

        strike = inst["strike"]
        por_strike.setdefault(strike, []).append(inst)

    # Índice auxiliar del lado opuesto, por (strike, vencimiento), para
    # poder calcular sus Greeks sobre el MISMO vencimiento de referencia
    # que el tipo pedido (mismo criterio de "instrumento más próximo").
    tipo_opuesto = "put" if tipo == "call" else "call"
    iv_lado_opuesto = {}
    for inst in instrumentos:
        if inst["tipo"] != tipo_opuesto:
            continue
        clave = (inst["strike"], inst["vencimiento"])
        iv_lado_opuesto[clave] = inst["iv"]

    resultados = {}

    for strike, candidatos in por_strike.items():

        oi_total = sum(c["oi"] for c in candidatos)

        # Vencimiento más próximo disponible en este strike, como
        # referencia para la gamma (mismo criterio que la versión anterior).
        candidatos.sort(key=lambda c: c["vencimiento"])
        inst_referencia = candidatos[0]

        dias_ref = (inst_referencia["vencimiento"] - ahora).total_seconds() / 86400.0
        dias_ref_seguro = max(dias_ref, 0.01)
        iv_ref = inst_referencia["iv"]

        gamma_strike = _gamma_black_scholes(
            spot=spot_actual,
            strike=strike,
            vol_anual=iv_ref,
            dias_a_vencimiento=dias_ref_seguro,
        )

        delta_strike = _delta_black_scholes(
            spot=spot_actual, strike=strike, vol_anual=iv_ref,
            dias_a_vencimiento=dias_ref_seguro, tipo=tipo,
        )
        vega_strike = _vega_black_scholes(
            spot=spot_actual, strike=strike, vol_anual=iv_ref,
            dias_a_vencimiento=dias_ref_seguro,
        )
        theta_strike = _theta_black_scholes(
            spot=spot_actual, strike=strike, vol_anual=iv_ref,
            dias_a_vencimiento=dias_ref_seguro, tipo=tipo,
        )

        # Lado opuesto: mismo strike, mismo vencimiento de referencia.
        # Si no existe ese instrumento en Deribit (puede pasar, no
        # todos los strikes tienen ambos lados listados), usamos la
        # misma IV de referencia como aproximación razonable -- el
        # objetivo es mostrar la lectura comparativa, no un dato exacto
        # de un contrato que no existe.
        clave_opuesta = (strike, inst_referencia["vencimiento"])
        iv_opuesta = iv_lado_opuesto.get(clave_opuesta, iv_ref)

        gamma_otro_lado = _gamma_black_scholes(
            spot=spot_actual, strike=strike, vol_anual=iv_opuesta,
            dias_a_vencimiento=dias_ref_seguro,
        )
        delta_otro_lado = _delta_black_scholes(
            spot=spot_actual, strike=strike, vol_anual=iv_opuesta,
            dias_a_vencimiento=dias_ref_seguro, tipo=tipo_opuesto,
        )
        vega_otro_lado = _vega_black_scholes(
            spot=spot_actual, strike=strike, vol_anual=iv_opuesta,
            dias_a_vencimiento=dias_ref_seguro,
        )
        theta_otro_lado = _theta_black_scholes(
            spot=spot_actual, strike=strike, vol_anual=iv_opuesta,
            dias_a_vencimiento=dias_ref_seguro, tipo=tipo_opuesto,
        )

        peso_tiempo = 1.0 / math.sqrt(max(dias_ref, 0.5))

        distancia_pct = abs((strike - spot_actual) / spot_actual) * 100
        # Decaimiento exponencial: a 0% de distancia, peso 1.0; a
        # ~7% de distancia, peso ya cayó a ~0.05 (prácticamente
        # descartado). Ajustado para que la wall relevante quede en un
        # rango operable de intradiario/scalp, no a decenas de miles
        # de USD de distancia.
        peso_distancia = math.exp(-distancia_pct / 2.5)

        score = oi_total * gamma_strike * peso_tiempo * peso_distancia

        resultados[strike] = {
            "score": score,
            "oi": oi_total,
            "gamma": gamma_strike,
            "distancia_pct": (strike - spot_actual) / spot_actual * 100,
            "delta": delta_strike,
            "vega": vega_strike,
            "theta": theta_strike,
            "gamma_otro_lado": gamma_otro_lado,
            "delta_otro_lado": delta_otro_lado,
            "vega_otro_lado": vega_otro_lado,
            "theta_otro_lado": theta_otro_lado,
            "dias_a_vencimiento": dias_ref,
        }

    # Segundo paso: recién acá conocemos el score máximo del lado
    # completo, necesario para el magnetismo relativo de la lectura de
    # decaimiento (ver _lectura_decaimiento).
    score_max_lado = max((r["score"] for r in resultados.values()), default=0.0) or 1.0
    for strike, info in resultados.items():
        magnetismo_relativo = info["score"] / score_max_lado
        info["lectura_decaimiento"] = _lectura_decaimiento(
            info["dias_a_vencimiento"], info["oi"], magnetismo_relativo
        )

    return resultados


def encontrar_wall(instrumentos, tipo, spot_actual, ahora, vencimientos_permitidos=None):
    """
    Encuentra el strike más relevante para un tipo de opción (call o
    put), usando un SCORE combinado (ver _calcular_score_strikes) en
    vez de solo OI bruto — así un strike lejano con mucho OI viejo no
    le gana al strike que realmente está influyendo el precio actual.

    vencimientos_permitidos: si se especifica (lista de datetimes),
    solo se consideran instrumentos con esos vencimientos exactos
    (pensado para limitar a vencimientos semanales reales, ver
    vencimientos_semanales_ordenados). Si es None, usa todos los
    instrumentos recibidos.
    """

    if vencimientos_permitidos is not None:
        instrumentos = filtrar_instrumentos_por_vencimientos(instrumentos, vencimientos_permitidos)

    scores = _calcular_score_strikes(instrumentos, tipo, spot_actual, ahora)

    if not scores:
        return None

    strike_wall = max(scores, key=lambda s: scores[s]["score"])
    info = scores[strike_wall]

    rol = "Resistencia" if strike_wall > spot_actual else "Soporte"

    return {
        "tipo": tipo,
        "strike": strike_wall,
        "oi": info["oi"],
        "gamma": info["gamma"],
        "distancia_pct": info["distancia_pct"],
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


def calcular_tabla_strikes(instrumentos, spot_actual, ahora, vencimientos_permitidos=None, max_strikes=12):
    """
    Calcula, para CADA strike (separando calls y puts) dentro de los
    vencimientos permitidos, los datos necesarios para el heatmap +
    tabla de la tab de Opciones/Derivados: OI agregado, gamma, score
    combinado (mismo criterio que encontrar_wall — ver
    _calcular_score_strikes), distancia % al precio actual, y los
    Greeks ampliados (delta, vega, theta, y los mismos del lado
    opuesto) más la lectura cualitativa de decaimiento.

    Pedido del usuario: un indicador tipo Deribit que muestre rápido
    dónde está el OI más cargado (magnetismo) por strike, con su
    variación, y además el detalle de Greeks para poder leer cómo se
    comporta cada contrato hacia su vencimiento (sin que esto sea una
    predicción de timing de movimiento de precio — ver docstring de
    _lectura_decaimiento). La variación de OI entre refreshes se
    calcula afuera de esta función (necesita guardarse en
    session_state, que vive en el script principal, no en esta
    función pura).

    Devuelve una lista de dicts ordenada por score descendente,
    limitada a max_strikes (los más relevantes, para no saturar la
    tabla con docenas de strikes irrelevantes).
    """

    if vencimientos_permitidos is not None:
        instrumentos = filtrar_instrumentos_por_vencimientos(instrumentos, vencimientos_permitidos)

    filas = []

    for tipo in ("call", "put"):
        scores = _calcular_score_strikes(instrumentos, tipo, spot_actual, ahora)
        for strike, info in scores.items():
            filas.append({
                "tipo": tipo,
                "strike": strike,
                "oi": info["oi"],
                "gamma": info["gamma"],
                "score": info["score"],
                "distancia_pct": info["distancia_pct"],
                "delta": info["delta"],
                "vega": info["vega"],
                "theta": info["theta"],
                "gamma_otro_lado": info["gamma_otro_lado"],
                "delta_otro_lado": info["delta_otro_lado"],
                "vega_otro_lado": info["vega_otro_lado"],
                "theta_otro_lado": info["theta_otro_lado"],
                "dias_a_vencimiento": info["dias_a_vencimiento"],
                "lectura_decaimiento": info["lectura_decaimiento"],
            })

    filas.sort(key=lambda f: f["score"], reverse=True)

    return filas[:max_strikes]



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


def _detectar_iman_dorado(niveles_tagged, precio_actual, tolerancia_pct=0.35):
    """
    IMÁN DORADO: detecta zonas donde coinciden (dentro de una tolerancia
    % chica) niveles calculados por métodos INDEPENDIENTES entre sí —
    no un solo indicador dando la misma lectura dos veces, sino distintas
    fuentes de cálculo llegando al mismo precio por caminos distintos.

    LÍMITE HONESTO (importante): esto NO es "spot + futuro + derivado"
    en el sentido literal de 3 mercados con order book propio — hoy el
    dashboard no tiene profundidad de book de futuros a nivel de precio
    (eso está en el roadmap, ligado al WebSocket de order book pendiente).
    Las 3 fuentes reales que SÍ tenemos, calculadas de forma
    independiente, son:
      - "Spot-liquidez": swing highs/lows de velas Binance (dónde el
        precio ya reaccionó antes).
      - "Opciones-Wall": strike con más OI ponderado en Deribit (dónde
        hay más contratos abiertos, posición estática).
      - "Opciones-Régimen": Flip Point Local o pico de Gamma Zone (dónde
        cambia el comportamiento dinámico de los dealers, no la posición
        en sí).
    Cuando 2 o más de estas fuentes coinciden en el mismo precio (dentro
    de tolerancia_pct), es una confluencia real entre métodos distintos
    -> más peso estadístico que cualquiera de las 3 por separado. Si el
    día de mañana se suma profundidad real de futuros, se agrega como
    una 4ta fuente ("Futuro-book") a esta misma función sin cambiar la
    lógica de clustering.

    niveles_tagged: lista de tuplas (fuente: str, precio: float).
    Devuelve lista de dicts {precio, fuentes, fuerza, distancia_pct},
    ordenada por cercanía absoluta al precio actual. fuerza = cantidad
    de fuentes DISTINTAS que coincidieron (2 o 3).
    """

    if not niveles_tagged or precio_actual <= 0:
        return []

    niveles_ordenados = sorted(niveles_tagged, key=lambda par: par[1])
    usados = [False] * len(niveles_ordenados)
    grupos = []

    for i, (fuente_i, precio_i) in enumerate(niveles_ordenados):
        if usados[i]:
            continue

        grupo = [(fuente_i, precio_i)]
        usados[i] = True

        for j in range(i + 1, len(niveles_ordenados)):
            if usados[j]:
                continue
            fuente_j, precio_j = niveles_ordenados[j]
            distancia_pct = abs(precio_j - precio_i) / precio_actual * 100
            if distancia_pct <= tolerancia_pct:
                grupo.append((fuente_j, precio_j))
                usados[j] = True

        fuentes_distintas = sorted(set(f for f, _ in grupo))

        if len(fuentes_distintas) >= 2:
            precio_promedio = sum(p for _, p in grupo) / len(grupo)
            grupos.append({
                "precio": precio_promedio,
                "fuentes": fuentes_distintas,
                "fuerza": len(fuentes_distintas),
                "distancia_pct": (precio_promedio - precio_actual) / precio_actual * 100,
            })

    grupos.sort(key=lambda g: abs(g["distancia_pct"]))

    return grupos


    """Devuelve el datetime del vencimiento más próximo entre todos los instrumentos."""

    if not instrumentos:
        return None

    return min(inst["vencimiento"] for inst in instrumentos)


def vencimientos_disponibles_ordenados(instrumentos):
    """Lista de vencimientos únicos, ordenados de más próximo a más lejano."""

    return sorted(set(inst["vencimiento"] for inst in instrumentos))


def vencimientos_semanales_ordenados(instrumentos, ahora, dias_max=None):
    """
    Filtra los vencimientos a SOLO los que caen en viernes (los
    semanales reales de Deribit).

    Por qué hace falta este filtro: Deribit mezcla en la misma lista
    de vencimientos los semanales (viernes), mensuales (último viernes
    del mes, que numéricamente también cae viernes) y trimestrales.
    Sin filtrar por día de semana, un trimestral lejano (que no cae
    viernes) podría colarse. weekday()==4 es viernes en Python
    (lunes=0).

    dias_max: si se especifica (no None), además acota a vencimientos
    dentro de esa ventana de días desde "ahora". Si es None (default),
    NO hay tope de días — se devuelven TODOS los viernes disponibles,
    ordenados de más próximo a más lejano, y quien llama decide cuántos
    usar (ver vencimientos_globales_5_reales para el criterio de
    selección del Flip Global).

    Nota honesta: esto NO distingue un semanal de un mensual que
    coincide en viernes (Deribit no expone esa distinción en el nombre
    del instrumento) — ambos quedan incluidos por igual si caen viernes.
    """

    todos = vencimientos_disponibles_ordenados(instrumentos)

    return [
        v for v in todos
        if v.weekday() == 4
        and (dias_max is None or (v - ahora).total_seconds() / 86400.0 <= dias_max)
    ]


def vencimientos_globales_5_reales(instrumentos, ahora):
    """
    Selecciona los vencimientos para el Flip Semanal (Global —
    mediano/largo plazo): los próximos 5 vencimientos semanales reales
    (viernes), SIN tope artificial de días — si el 5to viernes real
    cae a 35 o 40 días, se incluye igual, porque lo que importa es
    "5 vencimientos reales", no una ventana de calendario fija.

    Además, se garantiza que TODOS los viernes del mes calendario en
    curso (el mes de "ahora") que todavía no vencieron queden incluidos,
    aunque eso empuje la lista a más de 5 elementos — pedido explícito:
    el Global tiene que ver el mes completo en el que estamos parados,
    no cortarlo a mitad de mes solo porque ya se juntaron 5 viernes de
    otros meses.

    Devuelve la lista ordenada de más próximo a más lejano.
    """

    semanales = vencimientos_semanales_ordenados(instrumentos, ahora, dias_max=None)

    if not semanales:
        return []

    viernes_del_mes_actual = [
        v for v in semanales
        if v.year == ahora.year and v.month == ahora.month
    ]

    candidatos = semanales[:5]

    # Si algún viernes del mes en curso quedó afuera de los primeros 5
    # (mes con muchos viernes, o ya van varios vencidos y el resto cae
    # tarde en la lista), lo agregamos igual y reordenamos.
    faltantes_del_mes = [v for v in viernes_del_mes_actual if v not in candidatos]

    if faltantes_del_mes:
        candidatos = sorted(set(candidatos) | set(faltantes_del_mes))

    return candidatos


def filtrar_instrumentos_por_vencimientos(instrumentos, vencimientos_permitidos):
    """Devuelve solo los instrumentos cuyo vencimiento está en la lista permitida."""

    permitidos = set(vencimientos_permitidos)
    return [inst for inst in instrumentos if inst["vencimiento"] in permitidos]


def _score_carga_vencimiento(instrumentos, vencimiento, spot_actual, ahora):
    """
    Calcula la carga total (suma de OI x gamma, sin distinguir call/put)
    de TODOS los instrumentos que vencen en una fecha puntual. Es el
    mismo espíritu del score de Walls (OI x gamma, ponderado por qué tan
    cerca está del spot en gamma), pero agregado a nivel "vencimiento
    completo" en vez de por strike — sirve para decidir si ESE
    vencimiento concentra suficiente peso como para entrar en el Flip
    Local, no para encontrar un strike puntual.
    """

    carga = 0.0

    for inst in instrumentos:
        if inst["vencimiento"] != vencimiento:
            continue

        dias = (inst["vencimiento"] - ahora).total_seconds() / 86400.0
        dias_seguro = max(dias, 0.01)

        gamma = _gamma_black_scholes(
            spot=spot_actual, strike=inst["strike"], vol_anual=inst["iv"],
            dias_a_vencimiento=dias_seguro,
        )

        carga += inst["oi"] * gamma

    return carga


def vencimientos_locales_por_carga(instrumentos, vencimientos_candidatos, spot_actual, ahora, max_vencimientos=3, umbral_relativo=0.20):
    """
    Selecciona los vencimientos para el Flip Cercano (Local — corto
    plazo) en base a dónde está realmente concentrada la carga (OI x
    gamma), no por posición fija en la lista.

    Criterio (pedido explícito): "si están cargados los próximos tres,
    bien; si los más cargados son los próximos 2, bien" — es decir, no
    hay un número fijo de vencimientos a usar, sino que se va sumando
    mientras cada vencimiento adicional siga aportando una carga
    relevante, con un techo de max_vencimientos (3) para que el Local
    no termine pareciéndose al Global.

    Algoritmo:
      1. Se calcula el score de carga (OI x gamma) de cada uno de los
         vencimientos_candidatos (ordenados de más próximo a más lejano,
         normalmente vencimientos_global ya filtrado a semanales reales).
      2. Se toma el de MAYOR carga como referencia (score_max).
      3. Se van incluyendo vencimientos en orden de proximidad mientras
         su carga sea al menos umbral_relativo (20%) de score_max — un
         vencimiento casi vacío en comparación con el más cargado no
         suma, sería ruido para el cálculo de corto plazo.
      4. Tope duro de max_vencimientos (3), siempre se incluye al menos
         el más próximo de todos (si hay candidatos).

    Devuelve la lista de vencimientos seleccionados, ordenada de más
    próximo a más lejano.
    """

    if not vencimientos_candidatos:
        return []

    candidatos_ordenados = sorted(vencimientos_candidatos)[:max(max_vencimientos, 1) + 2]
    # +2 de margen: por si el vencimiento más cargado no es el primero
    # de la lista, igual queremos poder evaluarlo dentro del universo
    # candidato sin tener que mirar el listado entero de Deribit.

    scores = {
        v: _score_carga_vencimiento(instrumentos, v, spot_actual, ahora)
        for v in candidatos_ordenados
    }

    score_max = max(scores.values(), default=0.0)

    if score_max <= 0:
        # Sin datos de carga útiles: al menos devolvemos el más próximo,
        # para no dejar el Local sin ningún vencimiento.
        return candidatos_ordenados[:1]

    seleccionados = []

    for v in sorted(candidatos_ordenados):  # de más próximo a más lejano
        if len(seleccionados) >= max_vencimientos:
            break
        if not seleccionados:
            seleccionados.append(v)  # el más próximo siempre entra
            continue
        if scores[v] >= score_max * umbral_relativo:
            seleccionados.append(v)
        else:
            # Una vez que un vencimiento (en orden de proximidad) no
            # llega al umbral, los siguientes son aún más lejanos y
            # típicamente más chicos en carga reciente -> cortamos.
            break

    return seleccionados


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
with tab_dashboard:

    # ----------------------------------
# DATOS BTC (ticker)
# ----------------------------------

    try:
        ticker = obtener_ticker()
        if "error" in ticker:
            raise ConnectionError(ticker["error"])

        precio = float(ticker["lastPrice"])
        cambio_24h = float(ticker["priceChangePercent"])
        volumen = float(ticker["volume"])

        c1, c2, c3 = st.columns(3)

        with c1:
            st.metric("Precio BTC", f"${precio:,.2f}")

        with c2:
            if modo in ("Scalp", "Microscalp"):
                st.metric("Cambio 24h", f"{cambio_24h:+.2f}%", 
                         help="En Scalp/Microscalp se muestra 24h por simplicidad (1H se calcula más abajo)")
            else:
                st.metric("Cambio 24h", f"{cambio_24h:+.2f}%")

        with c3:
            st.metric("Volumen BTC", f"{volumen:,.0f}")

        st.caption("🟢 Conectado al mercado")

    except Exception as e:
        mostrar_estado_no_disponible(
            detalle_tecnico=str(e),
            contexto="No se pudo obtener el precio de BTC (vía proxy).",
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

    # Punto de control: si alguno de los 3 vino vacío (el proxy no
    # respondió), detenemos acá. Más abajo el Dealer Score usa
    # df_1h["close"].iloc[-1] directamente, que explotaría igual que el
    # error original si dejáramos pasar un df vacío sin chequear.
    if df_5m.empty or df_15m.empty or df_1h.empty:
        error_detalle = st.session_state.get(
            "error_binance_velas", "Sin detalle del error disponible."
        )
        mostrar_estado_no_disponible(
            detalle_tecnico=error_detalle,
            contexto="No se pudo obtener datos de velas (timeframes 5m/15m/1h). El dashboard no puede continuar este refresh.",
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
    #
    # Microscalp pide una ventana ULTRA CORTA (40 velas de 1M ≈ 40 minutos)
    # en vez de las 100 estándar: a esa escala, velas de hace más de
    # 40 minutos son ruido de otro régimen de mercado, no contexto útil
    # para una entrada de segundos-a-minutos.
    LIMITE_VELAS_MICROSCALP = 40
    limite_velas_grafico = LIMITE_VELAS_MICROSCALP if modo == "Microscalp" else 100
    df = obtener_velas(data_timeframe, limite_velas_grafico)

    # Punto de control central: si el proxy no respondió, df viene vacío.
    # En vez de dejar que explote en cualquier otro .iloc[-1] más adelante
    # (con un traceback ilegible), avisamos claro y detenemos la ejecución
    # de esta vuelta del script. st_autorefresh va a reintentar solo en 15s.
    if df.empty:
        error_detalle = st.session_state.get(
            "error_binance_velas", "Sin detalle del error disponible."
        )
        mostrar_estado_no_disponible(
            detalle_tecnico=error_detalle,
            contexto=f"No se pudo obtener datos de velas para el timeframe {data_timeframe}. El dashboard no puede continuar este refresh.",
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

    # Microscalp usa su propia ventana (más angosta que el "1m" de Scalp):
    # a 1m puro y ventana ultra corta, un swing de 12 velas tarda demasiado
    # en confirmar — bajamos a 4 para que reaccione a los micro-swings reales.
    if modo == "Microscalp":
        ventana_swing_activa = 4
    else:
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

        # FIX (calibración): Deribit mezcla en la misma lista de vencimientos
        # los semanales, mensuales y trimestrales. Tomar "los próximos 3-5
        # vencimientos disponibles" sin filtrar podía colar un mensual/
        # trimestral lejano que, por tener mucho OI acumulado de largo
        # plazo, arrastraba el Flip Global y las Walls a distancias de
        # precio gigantes (decenas de miles de USD) — inútil para operativa
        # intradiaria o de scalp. Ahora se filtra a SOLO vencimientos
        # semanales reales (viernes) dentro de una ventana corta de
        # DIAS_MAX_GLOBAL días. Ver vencimientos_semanales_ordenados.
        # FIX (calibración): Deribit mezcla en la misma lista de vencimientos
        # los semanales, mensuales y trimestrales. Tomar "los próximos 3-5
        # vencimientos disponibles" sin filtrar podía colar un mensual/
        # trimestral lejano que, por tener mucho OI acumulado de largo
        # plazo, arrastraba el Flip Global y las Walls a distancias de
        # precio gigantes (decenas de miles de USD) — inútil para operativa
        # intradiaria o de scalp. Ahora se filtra a SOLO vencimientos
        # semanales reales (viernes).
        #
        # GLOBAL (mediano/largo plazo): los próximos 5 vencimientos
        # semanales reales, SIN tope artificial de días, garantizando
        # además que se incluyan todos los viernes del MES EN CURSO
        # aunque eso sume algún vencimiento extra por encima de 5. Ver
        # vencimientos_globales_5_reales.
        vencimientos_semanales = vencimientos_semanales_ordenados(
            instrumentos_deribit, ahora, dias_max=None
        )

        if vencimientos_semanales:
            vencimientos_global = vencimientos_globales_5_reales(instrumentos_deribit, ahora)
        else:
            # Caso raro: Deribit no tiene NADA listado que caiga viernes
            # (no debería pasar en condiciones normales). Caemos a los
            # próximos vencimientos disponibles, sean viernes o no, para
            # no dejar el dashboard sin Flip/Walls.
            todos_ordenados = vencimientos_disponibles_ordenados(instrumentos_deribit)
            vencimientos_global = todos_ordenados[:5] or todos_ordenados[:3]

        vencimiento_global_max = vencimientos_global[-1] if vencimientos_global else None

        # LOCAL (corto plazo): ya no son "los primeros 2 de la lista" a
        # ciegas. Se calcula la carga real (OI x gamma) de cada uno de
        # los vencimientos candidatos del Global y se seleccionan los
        # que de verdad concentran peso de corto plazo — como mínimo el
        # más próximo, como máximo 3 (ver vencimientos_locales_por_carga).
        # Esto es lo que corrige que antes Local terminara apuntando al
        # mismo recorte que antes tenía Global invertido.
        vencimientos_local = vencimientos_locales_por_carga(
            instrumentos_deribit, vencimientos_global, precio_actual, ahora,
            max_vencimientos=3,
        )
        vencimiento_local_dt = vencimientos_local[-1] if vencimientos_local else None

        # RANGO DEL FLIP: Global y Local ya NO comparten el mismo rango de
        # búsqueda — usar la misma ventana para ambos era, en la práctica,
        # la razón por la que Local casi nunca mostraba algo distinto de
        # Global. Ahora cada uno tiene su propia escala, y Microscalp usa
        # una tercera, todavía más angosta, acorde a su horizonte de
        # segundos-a-minutos:
        #   - Global (mediano/largo plazo): ±6% — zona amplia, varios vencimientos.
        #   - Local (Scalp/Normal, corto plazo): ±1.8% — reacciona rápido
        #     sin perderse en ruido de precio lejano.
        #   - Local en Microscalp: ±0.8% — el flip tiene que estar MUY
        #     cerca del spot para ser información operable a esta escala.
        RANGO_FLIP_GLOBAL_PCT = 0.06
        RANGO_FLIP_LOCAL_PCT = 0.008 if modo == "Microscalp" else 0.018

        resultado_flip_global = calcular_flip(
            instrumentos_deribit, precio_actual, vencimiento_max=vencimiento_global_max,
            rango_pct=RANGO_FLIP_GLOBAL_PCT,
            ponderar_por_tiempo=True,  # evita que el OI de vencimientos lejanos distorsione el flip
        )
        resultado_flip_local = calcular_flip(
            instrumentos_deribit, precio_actual, vencimiento_max=vencimiento_local_dt,
            rango_pct=RANGO_FLIP_LOCAL_PCT,
            ponderar_por_tiempo=True,  # dos vencimientos mezclados: pesar el diario sobre el siguiente
        )

        # WALLS: ahora limitadas a los mismos vencimientos semanales/corto
        # plazo ya filtrados arriba (vencimientos_global), y usando el
        # score combinado (OI x gamma x peso tiempo x peso distancia) en
        # vez de OI bruto — ver encontrar_wall y _calcular_score_strikes.
        # Esto evita que un strike lejano con mucho OI viejo (vencimiento
        # mensual/trimestral) le gane al strike que realmente importa para
        # el precio actual.
        call_wall = encontrar_wall(
            instrumentos_deribit, "call", precio_actual, ahora,
            vencimientos_permitidos=vencimientos_global,
        )
        put_wall = encontrar_wall(
            instrumentos_deribit, "put", precio_actual, ahora,
            vencimientos_permitidos=vencimientos_global,
        )

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


def procesar_oi_fuente(clave_session, oi_nuevo, ventana=10, tope=20):
    """
    Procesa el Open Interest de UNA fuente puntual (Binance, Bybit,
    etc.) de forma autocontenida: cachea el último valor válido (para
    no parpadear a N/D en un refresh fallido), mantiene su PROPIO
    historial en session_state (clave_session, distinto al de otras
    fuentes -> evita que dos fuentes se pisen en el mismo historial,
    que era la causa de la "doble barra" repetida con un solo cálculo
    de cambio), y devuelve el cambio % vs ~ventana refreshes atrás.

    clave_session: prefijo único para esta fuente, ej. "binance" o
    "bybit". Se usan dos claves de session_state por fuente:
    f"{clave_session}_ultimo_oi_valido" y f"{clave_session}_oi_historial".

    Devuelve un dict con: disponible, valor, es_cache, cambio_pct,
    historial_lista (la lista misma, por si se necesita inspeccionar).
    """

    clave_ultimo = f"{clave_session}_ultimo_oi_valido"
    clave_historial = f"{clave_session}_oi_historial"

    if clave_ultimo not in st.session_state:
        st.session_state[clave_ultimo] = None
    if clave_historial not in st.session_state:
        st.session_state[clave_historial] = []

    if oi_nuevo is not None:
        st.session_state[clave_ultimo] = oi_nuevo
        disponible = True
        valor = oi_nuevo
        es_cache = False
    else:
        disponible = st.session_state[clave_ultimo] is not None
        valor = st.session_state[clave_ultimo] if disponible else 0.0
        es_cache = True

    cambio_pct = None

    if disponible:
        cambio_pct = _actualizar_y_calcular_cambio_oi(
            st.session_state[clave_historial], valor, ventana=ventana, tope=tope
        )

    return {
        "disponible": disponible,
        "valor": valor,
        "es_cache": es_cache,
        "cambio_pct": cambio_pct,
    }

def render_metrica_oi(titulo, resultado_fuente, color_caption="#9aa0a6"):
    """
    Dibuja UNA métrica de Open Interest con su barra de cambio %,
    reusando el resultado de procesar_oi_fuente. Esto es lo que evita
    repetir el bloque de st.metric + st.caption + st.progress a mano
    por cada fuente nueva que se agregue (Binance, Bybit, o la que
    venga después) -- una sola función, una sola barra por fuente,
    nunca una fuente pisando la barra de la otra.
    """
    if not resultado_fuente["disponible"]:
        st.metric(titulo, "N/D")
        
        # === DEBUG BYBIT ===
        if titulo == "OI Bybit" and "bybit_error" in st.session_state:
            st.error(f"🔍 Bybit Debug: {st.session_state.bybit_error}")
            if st.button("Limpiar debug Bybit", key="clear_bybit"):
                del st.session_state.bybit_error
        return

    etiqueta_cache = " ⏳" if resultado_fuente["es_cache"] else ""
    st.metric(titulo, f"{resultado_fuente['valor']:,.0f}{etiqueta_cache}")

    cambio = resultado_fuente["cambio_pct"]

    if cambio is None:
        st.caption("Sin historial suficiente todavía (≈2.5 min)")
        st.progress(0.0)
    else:
        st.caption(f"Cambio (≈2.5 min): **{cambio:+.2f}%**")
        st.progress(min(abs(cambio) / 0.5, 1.0))

        if abs(cambio) > 0.35:
            st.caption("📈 Creciendo fuerte" if cambio > 0 else "📉 Cayendo fuerte")
        elif abs(cambio) > 0.12:
            st.caption("📈 Subiendo" if cambio > 0 else "📉 Bajando")

with tab_dashboard:


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

    # Umbral de ruido dependiente del modo: Microscalp opera con objetivos
    # y stops mucho más chicos (ver lectura de gestión más abajo), así que
    # un nivel a $130 de distancia sigue siendo relevante ahí, mientras
    # que en Normal/Scalp seguiría siendo ruido.
    UMBRAL_RUIDO_LIQUIDEZ_USD = 45.0 if modo == "Microscalp" else 130.0

    filtro_liquidez_activo = st.toggle(
        "Ignorar niveles Imán (MAG) muy pegados al precio",
        value=True,
        help=(
            "⚠️ Esta variable afecta DOS lugares a la vez: la capa 🧲 IMÁN "
            "dibujada sobre el gráfico principal, Y el resumen de 'Niveles Imán "
            "(liquidez)' más abajo en la página. No son cálculos separados — "
            "es el mismo filtro aplicado en ambos.\n\n"
            f"Cuando está activo, solo se muestran soportes/resistencias Imán a "
            f"más de ${UMBRAL_RUIDO_LIQUIDEZ_USD:.0f} USD del precio actual "
            f"({'calibrado a Microscalp: objetivos 120-300 USD' if modo == 'Microscalp' else 'objetivos 250–1000 USD y stops 200–350 USD'})."
        ),
    )

    DISTANCIA_MINIMA_USD = UMBRAL_RUIDO_LIQUIDEZ_USD if filtro_liquidez_activo else 0.0
    st.caption(
        f"Filtro activo: ignorando niveles Imán a menos de ${UMBRAL_RUIDO_LIQUIDEZ_USD:.0f} del precio (afecta al gráfico y al resumen de abajo)."
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
    # 🧲✨ IMÁN DORADO — confluencia entre fuentes independientes
    # (ver docstring de _detectar_iman_dorado para el límite honesto de
    # qué significa "3 fuentes" hoy, y qué falta para que sea 4 reales).
    # ----------------------------------

    TOLERANCIA_IMAN_DORADO_PCT = 0.15 if modo == "Microscalp" else 0.35

    niveles_para_iman_dorado = []
    for s in soportes:
        niveles_para_iman_dorado.append(("Spot-liquidez", s))
    for r in resistencias:
        niveles_para_iman_dorado.append(("Spot-liquidez", r))
    if call_wall:
        niveles_para_iman_dorado.append(("Opciones-Wall", call_wall["strike"]))
    if put_wall:
        niveles_para_iman_dorado.append(("Opciones-Wall", put_wall["strike"]))
    if resultado_flip_local and resultado_flip_local.get("flip_point"):
        niveles_para_iman_dorado.append(("Opciones-Régimen", resultado_flip_local["flip_point"]))
    if zona_gamma_hi:
        niveles_para_iman_dorado.append(("Opciones-Régimen", zona_gamma_hi["precio"]))
    if zona_gamma_lo:
        niveles_para_iman_dorado.append(("Opciones-Régimen", zona_gamma_lo["precio"]))

    iman_dorado_grupos = _detectar_iman_dorado(
        niveles_para_iman_dorado, precio_actual, tolerancia_pct=TOLERANCIA_IMAN_DORADO_PCT
    )
    iman_dorado_activo = iman_dorado_grupos[0] if iman_dorado_grupos else None

    # ----------------------------------
    # BOTONERA DE CAPAS (toggles individuales, estilo overlay de trading)
    # ----------------------------------

    if "capas_activas" not in st.session_state:
        st.session_state.capas_activas = {
            "IMAN": True,
            "IMAN_DORADO": True,
            "MINI_FLIP": True,
            "FLIP_FULL": True,
            "GAMMA_ZONE": False,
            "WALLS": False,
            "ABSORB": True,
        }
    st.session_state.capas_activas.setdefault("IMAN_DORADO", True)
    # Migración: si quedó guardada una sesión vieja con la clave "FLIP"
    # unificada, la separamos para no romper el estado de quien ya tenía
    # el dashboard abierto antes de este cambio.
    if "FLIP" in st.session_state.capas_activas:
        valor_previo = st.session_state.capas_activas.pop("FLIP")
        st.session_state.capas_activas.setdefault("MINI_FLIP", valor_previo)
        st.session_state.capas_activas.setdefault("FLIP_FULL", valor_previo)

    st.markdown("**Capas sobre el gráfico**")

    b1, b2, b3, b4, b5, b6, b7 = st.columns(7)

    with b1:
        st.session_state.capas_activas["IMAN"] = st.toggle(
            "🧲 IMÁN", value=st.session_state.capas_activas["IMAN"], key="cap_iman"
        )
    with b2:
        st.session_state.capas_activas["IMAN_DORADO"] = st.toggle(
            "🧲✨ DORADO", value=st.session_state.capas_activas["IMAN_DORADO"], key="cap_iman_dorado",
            help=(
                "Se activa cuando coinciden (dentro de ±0.35%, o ±0.15% en Microscalp) "
                "2 o más fuentes calculadas de forma independiente: liquidez Spot, "
                "Wall de Opciones y Flip/Gamma de Opciones. Ver detalle completo más "
                "abajo, en 'Detalle de niveles activos'."
            )
        )
    with b3:
        st.session_state.capas_activas["MINI_FLIP"] = st.toggle(
            "🔁 MINI FLIP", value=st.session_state.capas_activas["MINI_FLIP"], key="cap_mini_flip",
            help="Flip Cercano (Local): vencimientos de corto plazo seleccionados por carga real de OI/gamma (1 a 3, el más próximo siempre incluido). Rango de búsqueda ±1.8% (Normal/Scalp) o ±0.8% (Microscalp)."
        )
    with b4:
        st.session_state.capas_activas["FLIP_FULL"] = st.toggle(
            "🔁 FLIP FULL", value=st.session_state.capas_activas["FLIP_FULL"], key="cap_flip_full",
            help="Flip Semanal (Global): agrega los próximos 5 vencimientos semanales reales de Deribit (viernes), incluyendo todos los del mes en curso. Pensado para Normal/intradiario."
        )
    with b5:
        st.session_state.capas_activas["GAMMA_ZONE"] = st.toggle(
            "🌀 GAMMA ZONE", value=st.session_state.capas_activas["GAMMA_ZONE"], key="cap_gamma"
        )
    with b6:
        st.session_state.capas_activas["WALLS"] = st.toggle(
            "🧱 WALLS", value=st.session_state.capas_activas["WALLS"], key="cap_walls"
        )
    with b7:
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

    # --- MARGEN VISIBLE A LA DERECHA (espacio de ~3 velas vacías) ---
    # Pedido del usuario: que entre la última vela y los valores del eje
    # de precio (derecha) quede un espacio fijo, tipo 3 velas en blanco,
    # en vez de que la última vela quede pegada al borde del gráfico.
    # Esto es SOLO el rango visible inicial del eje X (x_min/x_max de
    # arriba no se tocan, porque esas siguen marcando el ancho real de
    # las líneas de nivel — Imán, Flip, Walls, etc. — que deben cruzar
    # todo el candlestick, no el margen extra).
    VELAS_DE_MARGEN_DERECHO = 3

    duracion_vela = df["open_time"].iloc[-1] - df["open_time"].iloc[-2]
    x_max_visible = x_max + duracion_vela * VELAS_DE_MARGEN_DERECHO


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
with tab_dashboard:


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

    # --- PRECIO ACTUAL: valor completo (sin abreviar) + recuadro naranja ---
    # Pedido del usuario: que el último precio se vea completo (ej. 62,810
    # y no "$63,1" recortado por superponerse con otros indicadores), y
    # que además resalte sobre el resto de las capas con un color propio
    # (naranja) que no se confunda con Imán/Flip/Walls/Absorb. Reusa
    # _etiqueta_overlay para mantener el mismo estilo visual (línea +
    # etiqueta con fondo semi-transparente) que ya tienen los demás niveles.
    _etiqueta_overlay(
        fig_overlay, precio_actual,
        f"💲 ${precio_actual:,.0f}",
        "#ff8c00", "rgba(255,140,0,0.30)",
        dash="dot",
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

    # --- CROSSHAIR (cruz que sigue al mouse en todo el gráfico) ---
    # Plotly no tiene un "crosshair" nativo como lightweight-charts, pero
    # "spikelines" cumple la misma función: al pasar el mouse sobre el
    # área de velas, dibuja una línea vertical (hasta el eje de tiempo) y
    # una horizontal (hasta el eje de precio), mostrando los valores en
    # ambos ejes. hovermode="x" asegura que la spike vertical siga al
    # cursor sin necesidad de estar exactamente sobre una vela.
    fig_overlay.update_xaxes(
        fixedrange=False, visible=True, showticklabels=True,
        range=[x_min, x_max_visible],  # rango inicial: deja el margen de 3 velas a la derecha
        showspikes=True, spikemode="across", spikesnap="cursor",
        spikecolor="rgba(255,255,255,0.5)", spikethickness=1, spikedash="solid",
    )
    fig_overlay.update_yaxes(
        fixedrange=False, side="right", nticks=25, visible=True, showticklabels=True,
        showspikes=True, spikemode="across", spikesnap="cursor",
        spikecolor="rgba(255,255,255,0.5)", spikethickness=1, spikedash="solid",
    )

    fig_overlay.update_layout(
        template="plotly_dark",          # fuerza el tema oscuro explícitamente
        paper_bgcolor="#0e1117",          # fondo exterior, igual al fondo de Streamlit
        plot_bgcolor="#0e1117",           # fondo del área de trazado (donde van las velas)
        xaxis_rangeslider_visible=False,
        height=650,
        margin=dict(l=10, r=90, t=10, b=40),  # más margen abajo para que el eje de tiempo no quede cortado
        dragmode="pan",  # arrastrar mueve el gráfico; zoom queda a cargo de la rueda
        hovermode="x",  # activa el crosshair: spike vertical sigue al cursor en toda la franja de tiempo
    )

    st.caption(
        "🖱️ Posicionate sobre el gráfico y usá la rueda del mouse para hacer zoom "
        "(acercar/alejar). Arrastrá con el clic para desplazarte si algún nivel "
        "(Wall, Flip, Gamma Zone) quedó fuera del recuadro visible."
    )

    # Aviso de refresco automático: info operativa para el usuario (no
    # mantenimiento de código), por eso va pegado al gráfico — ayuda a
    # entender por qué el precio/velas cambian solos cada 15s sin que el
    # usuario haga nada. La fecha de actualización del CÓDIGO (changelog)
    # se movió al pie de página general del dashboard, ver el final del
    # archivo — son dos cosas distintas: una es mantenimiento, esta es
    # comportamiento en vivo de la página.
    st.markdown(
        """
        <div style="text-align:right; font-size:11px; color:#5c6370; margin-top:-8px;">
            🔄 Actualización automática cada 15 seg.
        </div>
        """,
        unsafe_allow_html=True,
    )

    st.plotly_chart(
        fig_overlay,
        use_container_width=True,
        config={
            "scrollZoom": True,       # la rueda del mouse hace zoom en vez de scrollear la página
            "displayModeBar": True,
            "modeBarButtonsToAdd": ["pan2d", "zoomIn2d", "zoomOut2d", "autoScale2d", "resetScale2d"],
        },
        key="fig_overlay_tab_dashboard",
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

    if "ultimo_funding_valido" not in st.session_state:
        st.session_state.ultimo_funding_valido = None
    if "ultimo_oi_valido" not in st.session_state:
        st.session_state.ultimo_oi_valido = None

    funding = obtener_funding()

    # Open Interest: se pide a las dos fuentes (Binance y Bybit). Cada
    # una se procesa por separado con procesar_oi_fuente (su propio
    # historial, su propio cambio %) para poder mostrarlas como dos
    # métricas distinguidas en el panel Institucional, sin que una
    # pise el historial de la otra. Además se mantiene "oi"/"fuente_oi"
    # como el MEJOR disponible (Bybit primero, fallback a Binance) para
    # no romper Dealer Score, Flow y Market Intelligence, que ya usaban
    # esta variable combinada.
    oi_binance = obtener_open_interest()
    oi_bybit = obtener_open_interest_bybit()

    resultado_oi_binance = procesar_oi_fuente("binance", oi_binance)
    resultado_oi_bybit = procesar_oi_fuente("bybit", oi_bybit)

    if oi_bybit is not None and oi_bybit > 0:
        oi = oi_bybit
        fuente_oi = "Bybit"
    elif oi_binance is not None:
        oi = oi_binance
        fuente_oi = "Binance"
    else:
        oi = None
        fuente_oi = "N/D"

    # FIX: en vez de mostrar N/D cada vez que un refresh de 15s falla
    # (timeout del proxy, Render dormido, etc.), usamos el último valor
    # bueno conocido y marcamos que es un dato "viejo" sin refrescar.
    # Esto elimina el parpadeo entre dato y N/D en refreshes consecutivos.

    if funding is not None:
        st.session_state.ultimo_funding_valido = funding
        funding_disponible = True
        funding_valor = funding
        funding_es_cache = False
    else:
        funding_disponible = st.session_state.ultimo_funding_valido is not None
        funding_valor = st.session_state.ultimo_funding_valido if funding_disponible else 0.0
        funding_es_cache = True

    if oi is not None:
        st.session_state.ultimo_oi_valido = oi
        oi_disponible = True
        oi_valor = oi
        oi_es_cache = False
    else:
        oi_disponible = st.session_state.ultimo_oi_valido is not None
        oi_valor = st.session_state.ultimo_oi_valido if oi_disponible else 0.0
        oi_es_cache = True
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

                # =============================================
        # SCALP EDGE SCORE + SCORE DE CONTEXTO (recalibrados)
        # =============================================
        # Rediseño (pedido del usuario: los scores no eran claros y casi
        # siempre terminaban en "esperar"). Dos cambios de fondo:
        #   1. Cada score ahora es la suma de componentes con peso FIJO
        #      y documentado que suman exactamente 100 — se puede ver el
        #      desglose completo en el expander de abajo, no es una caja
        #      negra.
        #   2. Los umbrales de lectura se bajaron: antes hacía falta
        #      75-78/100 para la lectura "alta", un nivel que rara vez se
        #      alcanzaba con todos los componentes activos a la vez. Ahora
        #      65/100 alcanza para "alta confluencia", en línea con cómo
        #      se distribuyen realmente los puntos entre sí.
        # Aplica igual en Scalp y Microscalp; en Microscalp los umbrales
        # de distancia (Flip, Wall) ya vienen recalibrados más arriba
        # (rango ±0.8% en vez de ±1.8%), así que el score reacciona a
        # movimientos más chicos sin cambiar su fórmula.

        cerca_de_nivel = nivel_mas_cercano(precio_actual, soportes, resistencias)
        en_zona_relevante = cerca_de_nivel is not None and cerca_de_nivel[2] < 0.25

        componentes_scalp_edge = []  # [(nombre, puntos_obtenidos, puntos_max)]

        # 1) Absorción (peso máximo: es la señal más directa de defensa
        #    de un dealer sobre un nivel real) — máx 30
        if hay_absorcion and en_zona_relevante:
            p = 30
        elif hay_absorcion:
            p = 20
        elif detalle_absorcion and detalle_absorcion["score"] >= 65:
            p = 15
        else:
            p = 0
        componentes_scalp_edge.append(("Absorción", p, 30))

        # 2) Proximidad al Flip Local — máx 25
        dist_flip_umbral_alto = 0.5 if modo == "Microscalp" else 0.8
        dist_flip_umbral_medio = 1.0 if modo == "Microscalp" else 1.5
        if resultado_flip_local and resultado_flip_local.get("flip_point"):
            dist_flip_local = abs(resultado_flip_local["flip_point"] - precio_actual) / precio_actual * 100
            if dist_flip_local < dist_flip_umbral_alto:
                p = 25
            elif dist_flip_local < dist_flip_umbral_medio:
                p = 15
            else:
                p = 0
        else:
            p = 0
        componentes_scalp_edge.append(("Proximidad Flip Local", p, 25))

        # 3) Velocidad + presión combinadas — máx 20
        if estado_velocidad == "acelerando" and buy_pressure > 58:
            p = 20
        elif estado_velocidad == "acelerando":
            p = 10
        elif estado_velocidad == "desacelerando" and hay_absorcion:
            p = 20
        else:
            p = 0
        componentes_scalp_edge.append(("Velocidad + presión", p, 20))

        # 4) Presión taker dominante — máx 15
        p = 15 if (buy_pressure > 65 or sell_pressure > 65) else (8 if (buy_pressure > 58 or sell_pressure > 58) else 0)
        componentes_scalp_edge.append(("Presión taker", p, 15))

        # 5) Proximidad a Wall (call/put) — máx 10
        dist_wall = 999.0
        if call_wall or put_wall:
            dist_wall = min(
                abs((call_wall["strike"] - precio_actual) / precio_actual * 100) if call_wall else 999,
                abs((put_wall["strike"] - precio_actual) / precio_actual * 100) if put_wall else 999
            )
        p = 10 if dist_wall < 0.6 else (5 if dist_wall < 1.2 else 0)
        componentes_scalp_edge.append(("Proximidad Wall", p, 10))

        scalp_edge = round(min(sum(c[1] for c in componentes_scalp_edge), 100))

        # --- Score de Contexto (ex "Dealer Score"): favorabilidad general
        # del mercado para operar a favor de tendencia, no una señal de
        # entrada. Componentes con peso fijo, máx 100. ---
        componentes_contexto = []

        p = round(15 * min(abs(funding_valor) / 0.05, 1.0)) if (funding_disponible and funding_valor > 0) else 0
        componentes_contexto.append(("Funding (sobrecompra)", p, 15))

        p = round(10 * min(max((oi_valor - 90000) / 90000, 0), 1.0)) if oi_disponible else 0
        componentes_contexto.append(("Open Interest elevado", p, 10))

        desbalance_presion = max(0.0, (buy_pressure - 50) / 50)
        p = round(40 * desbalance_presion)
        componentes_contexto.append(("Desbalance de presión", p, 40))

        if "Alcista" in tendencia_activa:
            p = 35
        elif "Bajista" in tendencia_activa:
            p = 32
        else:
            p = 0
        componentes_contexto.append(("Tendencia activa", p, 35))

        dealer_score = round(min(sum(c[1] for c in componentes_contexto), 100))

        # Mostramos ambos scores
        col_score1, col_score2 = st.columns(2)
        with col_score1:
            st.metric("**Scalp Edge Score**", f"{scalp_edge}/100", 
                     help="Suma de 5 componentes con peso fijo (30+25+20+15+10=100): Absorción, Proximidad Flip Local, Velocidad+presión, Presión taker, Proximidad Wall. Ver desglose abajo.")
        with col_score2:
            st.metric("Score de Contexto", f"{dealer_score}/100",
                     help="Suma de 4 componentes con peso fijo (15+10+40+35=100): Funding, OI, Desbalance de presión, Tendencia activa. Favorabilidad general, no es señal de entrada.")

        with st.expander("🔍 Ver desglose de puntos (Scalp Edge / Contexto)"):
            col_desglose1, col_desglose2 = st.columns(2)
            with col_desglose1:
                st.caption("**Scalp Edge Score**")
                for nombre, puntos, maximo in componentes_scalp_edge:
                    st.caption(f"• {nombre}: {puntos}/{maximo}")
            with col_desglose2:
                st.caption("**Score de Contexto**")
                for nombre, puntos, maximo in componentes_contexto:
                    st.caption(f"• {nombre}: {puntos}/{maximo}")

        # --- Recomendación clara (umbrales recalibrados: antes 75/55) ---
        if scalp_edge >= 65:
            recomendacion = "🟢 **ALTA CONFLUENCIA SCALP** — Buscar entrada en dirección de tendencia con confluencia de absorción + Flip Local."
        elif scalp_edge >= 40:
            recomendacion = "🟡 **Oportunidad moderada** — Vigilar reacción en próximo nivel Imán."
        else:
            recomendacion = "🔴 **Esperar mejor setup** — Baja confluencia para scalp."

        st.info(recomendacion)
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
            etiqueta_cache_f = " ⏳" if funding_es_cache else ""
            st.metric("Funding", f"{funding_valor:.4f}% {estado_funding}{etiqueta_cache_f}")
            if funding_es_cache:
                st.caption("⏳ Último dato conocido, este refresh no pudo actualizar.")
        else:
            st.metric("Funding", "N/D")
        # =============================================
        # OPEN INTEREST — BINANCE Y BYBIT POR SEPARADO
        # =============================================
        # FIX (pedido del usuario: "me sale doble barra"): antes había
        # UN solo historial compartido (oi_historial) al que se le hacía
        # append() DOS VECES por refresh (una vez en el bloque de
        # display, otra en el bloque de "cambio"), desincronizando las
        # ventanas de comparación. Ahora cada fuente tiene su propio
        # historial dedicado (vía procesar_oi_fuente/render_metrica_oi,
        # con un único append por fuente por refresh) y se muestran
        # como DOS métricas distinguidas, sin pisarse entre sí ni
        # duplicar el cálculo.

        col_oi1, col_oi2 = st.columns(2)

        with col_oi1:
            render_metrica_oi("OI Binance", resultado_oi_binance)

        with col_oi2:
            render_metrica_oi("OI Bybit", resultado_oi_bybit)

        # cambio_oi / oi_disponible / oi_valor: se mantienen como el
        # MEJOR dato disponible (mismo criterio que "oi"/"fuente_oi" —
        # Bybit primero, fallback a Binance), porque Dealer Score, Flow
        # y Market Intelligence más abajo en el dashboard ya dependen de
        # estos nombres. Usa su propio historial dedicado
        # (st.session_state.oi_historial), con un único append por
        # refresh — ya no comparte lista ni se duplica con las métricas
        # individuales de Binance/Bybit de arriba.

        cambio_oi = None

        if oi_disponible:
            cambio_oi = _actualizar_y_calcular_cambio_oi(
                st.session_state.oi_historial, oi_valor
            )

        cambio_oi = cambio_oi if cambio_oi is not None else 0.0

        st.caption(f"OI combinado (mejor fuente: {fuente_oi}) — cambio ≈2.5 min: **{cambio_oi:+.2f}%**")

        UMBRAL_OI = 0.15  # calibrado al movimiento real acumulado de BTC en ~2.5 min

        if cambio_oi > UMBRAL_OI:
            st.success("📈 Participación entrando")
        elif cambio_oi < -UMBRAL_OI:
            st.warning("📉 Participación saliendo")
        else:
            st.info("⚖️ OI estable")

        st.progress(min(abs(cambio_oi) / 0.5, 1.0))

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

with tab_dashboard:

    # ----------------------------------
    # PARTICIPANTES DE MERCADO (reemplaza al "Actor dominante" anterior)
    # ----------------------------------
    # Antes esto era una sola línea de caption con un umbral binario poco
    # transparente (mm_score>=3 / retail_proxy_score>=2, sin institucional).
    # Ahora es un panel propio: reparte 3 categorías (Market Maker, Retail,
    # Institucional) con puntaje propio por categoría, normalizado a 100%
    # entre las tres, y agrega un sesgo direccional probable según quién
    # domina. Es una lectura ESTADÍSTICA basada en proxies (absorción,
    # velocidad, presión, funding, OI, tendencia estructural) — no
    # identifica contrapartes reales ni usa datos de book (Level 2).

    st.divider()
    st.subheader(
        "🧑‍🤝‍🧑 Participantes de mercado",
        help=(
            "Estima qué tipo de participante domina el flujo ahora mismo, combinando "
            "las señales ya calculadas arriba: Market Maker (absorción, defensa de "
            "Wall/Flip Local), Retail (impulso direccional sin absorción, presión "
            "taker fuerte) e Institucional (funding activo, OI creciendo, tendencia "
            "estructural en 1H). Los 3 puntajes se normalizan para sumar 100% entre "
            "sí, y de ahí se deriva un sesgo direccional probable — una lectura "
            "estadística de probabilidades relativas, no una certeza ni una señal "
            "de entrada."
        ),
    )

    puntos_mm = 0
    puntos_retail = 0
    puntos_inst = 0

    # --- Market Maker: defiende niveles, absorbe flujo sin dejar mover precio ---
    if hay_absorcion:
        puntos_mm += 40
    elif detalle_absorcion and detalle_absorcion["score"] >= 60:
        puntos_mm += 20
    if dist_wall < 0.6:
        puntos_mm += 20
    if resultado_flip_local and resultado_flip_local.get("flip_point"):
        dist_flip_local_mm = abs(resultado_flip_local["flip_point"] - precio_actual) / precio_actual * 100
        if dist_flip_local_mm < dist_flip_umbral_alto:
            puntos_mm += 15
    if estado_velocidad == "desacelerando":
        puntos_mm += 10

    # --- Retail: impulso direccional agresivo, sin defensa visible de dealers ---
    if estado_velocidad == "acelerando" and not hay_absorcion:
        puntos_retail += 35
    if buy_pressure > 65 or sell_pressure > 65:
        puntos_retail += 30
    if dist_wall > 1.2:
        puntos_retail += 15

    # --- Institucional: funding activo, OI creciendo, tendencia estructural 1H ---
    if funding_disponible and abs(funding_valor) > 0.01:
        puntos_inst += 25
    if oi_disponible and cambio_oi > 0.15:
        puntos_inst += 30
    if "Alcista" in tendencia_1h or "Bajista" in tendencia_1h:
        puntos_inst += 25

    total_participantes = puntos_mm + puntos_retail + puntos_inst

    if total_participantes > 0:
        pct_mm = round(puntos_mm / total_participantes * 100)
        pct_retail = round(puntos_retail / total_participantes * 100)
        pct_inst = 100 - pct_mm - pct_retail
    else:
        pct_mm, pct_retail, pct_inst = 34, 33, 33

    col_p1, col_p2, col_p3 = st.columns(3)
    with col_p1:
        st.metric("🏦 Market Maker", f"{pct_mm}%")
        st.progress(pct_mm / 100)
    with col_p2:
        st.metric("👤 Retail", f"{pct_retail}%")
        st.progress(pct_retail / 100)
    with col_p3:
        st.metric("🏛️ Institucional", f"{pct_inst}%")
        st.progress(pct_inst / 100)

    dominante_pct = max(pct_mm, pct_retail, pct_inst)

    if dominante_pct == pct_mm:
        if resultado_flip_local and resultado_flip_local.get("flip_point"):
            sesgo_probable = (
                "defensa de soporte (compra hacia el nivel)"
                if resultado_flip_local["flip_point"] < precio_actual
                else "defensa de resistencia (venta hacia el nivel)"
            )
        else:
            sesgo_probable = "rango comprimido, sin dirección clara"
        lectura_participantes = (
            f"**Dominante: 🏦 Market Maker ({dominante_pct}%)**\n\n"
            f"Comportamiento esperado: defensa de nivel, rango más comprimido.\n"
            f"Sesgo probable: {sesgo_probable}."
        )
    elif dominante_pct == pct_retail:
        sesgo_probable = "continuación alcista" if buy_pressure > sell_pressure else "continuación bajista"
        lectura_participantes = (
            f"**Dominante: 👤 Retail ({dominante_pct}%)**\n\n"
            f"Comportamiento esperado: impulso direccional sin defensa visible de "
            f"dealers — mayor riesgo de reversión brusca si aparece absorción.\n"
            f"Sesgo probable: {sesgo_probable}."
        )
    else:
        if "Alcista" in tendencia_1h:
            sesgo_probable = "alcista estructural"
        elif "Bajista" in tendencia_1h:
            sesgo_probable = "bajista estructural"
        else:
            sesgo_probable = "neutral, a la espera de definición en 1H"
        lectura_participantes = (
            f"**Dominante: 🏛️ Institucional ({dominante_pct}%)**\n\n"
            f"Comportamiento esperado: flujo más lento, ligado a funding/OI y "
            f"tendencia de 1H.\n"
            f"Sesgo probable: {sesgo_probable}."
        )

    st.info(lectura_participantes)

    st.caption(
        "⚠️ Lectura probabilística basada en proxies de velas/OI/funding, NO en "
        "datos de book real (Level 2) ni en identificación real de contrapartes. "
        "Los % son pesos relativos entre las 3 categorías, no una medición directa "
        "de participación de mercado — es intuición estadística, no un hecho."
    )

with tab_opciones:

    # Mini-candlestick reutilizando la misma figura del dashboard
    # principal (fig_overlay ya construida más arriba, con todas las
    # capas activas que el usuario haya elegido). Pedido del usuario:
    # tener referencia visual del precio también en esta tab, sin
    # duplicar el cálculo del gráfico ni la lógica de las capas.
    st.plotly_chart(
        fig_overlay,
        use_container_width=True,
        config={"scrollZoom": True, "displayModeBar": True},
        key="fig_overlay_tab_opciones",
    )

    st.divider()

    resultado_bias = mb.calcular_market_bias(
    precio_actual=precio_actual,
    hay_absorcion=hay_absorcion,
    detalle_absorcion=detalle_absorcion,
    resultado_flip_local=resultado_flip_local,
    buy_pressure=buy_pressure, sell_pressure=sell_pressure,
    estado_velocidad=estado_velocidad,
    funding_disponible=funding_disponible, funding_valor=funding_valor,
    oi_disponible=oi_disponible, cambio_oi=cambio_oi,
    tendencia_1h=tendencia_1h,
    iman_dorado_activo=iman_dorado_activo,
    )

    st.metric("🧭 Market Bias", f"{resultado_bias['bias']:+d}", f"Confianza {resultado_bias['confianza']}%")
    st.info(resultado_bias["lectura"])
    with st.expander("Desglose del bias"):
    for nombre, puntos, activo, detalle in resultado_bias["componentes"]:
        estado = "✅" if activo else "⚠️ inactivo"
        st.caption(f"{estado} **{nombre}**: {puntos:+.1f} pts — {detalle}")

   
        
    # ----------------------------------
    # HEATMAP DE STRIKES — FORMATO BOOK DE PROFUNDIDAD (DOM)
    # ----------------------------------
    #
    # FIX (pedido del usuario, calibración de lectura): antes cada
    # strike aparecía en DOS filas separadas (una fila call, otra fila
    # put), cada una con su propia barra horizontal completa. Ahora es
    # UNA sola fila por STRIKE, con la columna de PRECIO al centro,
    # la barra de CALL creciendo hacia la IZQUIERDA y la barra de PUT
    # creciendo hacia la DERECHA — igual que un book de profundidad de
    # mercado (DOM), tal como lo esquematizó el usuario. Cada lado
    # escala de forma INDEPENDIENTE contra el máximo de su propio lado
    # (un call grande no aplasta visualmente a un put chico en la misma
    # fila, ni viceversa). El precio actual sigue cruzando el eje en su
    # posición real, como separador entre los strikes de arriba y abajo.
    #
    # Agrupamiento por strike: ver _agrupar_filas_strikes_por_precio.
    # Mismo criterio de score que ya usan las Walls (sin cambios en el
    # cálculo subyacente — esto es solo reordenamiento/recombinación
    # visual de filas que ya existían por separado).
    #
    # Historial de OI por strike en sesión (Deribit no da histórico
    # vía API pública), mismo patrón que ya se usa para Call/Put Wall.

    def _agrupar_filas_strikes_por_precio(tabla_strikes):
        """
        Reagrupa la lista plana de calcular_tabla_strikes (una fila por
        cada combinación strike+tipo) en un dict {strike: {"call":fila_o_None, "put":fila_o_None}},
        para poder dibujar UNA fila por strike con call a la izquierda y
        put a la derecha. Si un strike solo tiene datos de un lado (no
        todos los strikes de Deribit tienen call Y put con suficiente
        OI/IV), el lado faltante queda en None y simplemente no dibuja
        barra de ese lado.
        """

        agrupado = {}

        for fila in tabla_strikes:
            entrada = agrupado.setdefault(fila["strike"], {"call": None, "put": None})
            entrada[fila["tipo"]] = fila

        return agrupado

    st.subheader(
        "🌡️ Heatmap de strikes — book de profundidad (OI · magnetismo · variación)",
        help=(
            "Una fila por strike, cada $500 (intervalo nativo de Deribit para BTC). "
            "CALL crece hacia la izquierda, PUT crece hacia la derecha, cada lado "
            "escalado contra el máximo de SU propio lado (no se comparan entre sí "
            "en ancho). El precio actual cruza el eje en su posición real, separando "
            "los strikes de arriba (resistencias) de los de abajo (soportes). La "
            "barra más larga = mayor SCORE combinado (OI x gamma x peso tiempo x "
            "peso distancia, igual criterio que las Walls). Δ OI compara contra el "
            "valor de hace ~10 refreshes (~2.5 min)."
        ),
    )

    if "strikes_oi_historial" not in st.session_state:
        st.session_state.strikes_oi_historial = {}

    if not deribit_disponible:
        st.warning(
            "⚠️ No se pudo conectar a la API de Deribit en este refresh. "
            "El heatmap de strikes no está disponible momentáneamente."
        )
    else:

        tabla_strikes = calcular_tabla_strikes(
            instrumentos_deribit, precio_actual, ahora,
            vencimientos_permitidos=vencimientos_global,
            max_strikes=20,  # más alto que antes: ahora cada fila puede llevar call+put juntos, así que se necesitan más filas crudas para llenar una buena cantidad de strikes visibles
        )

        if not tabla_strikes:
            st.caption("Sin datos suficientes de strikes en los vencimientos filtrados.")
        else:

            # Actualiza el historial de OI de cada fila (call y put por
            # separado, cada uno tiene su propio Open Interest) ANTES de
            # agrupar/renderizar, para que el Δ esté disponible ya en
            # este ciclo (mismo patrón que el bloque anterior).
            for fila in tabla_strikes:
                clave_hist = f"{fila['tipo']}_{fila['strike']:.0f}"
                historial = st.session_state.strikes_oi_historial.setdefault(clave_hist, [])
                fila["cambio_oi"] = _actualizar_y_calcular_cambio_oi(historial, fila["oi"])

            score_max_call = max([f["score"] for f in tabla_strikes if f["tipo"] == "call"], default=1.0) or 1.0
            score_max_put = max([f["score"] for f in tabla_strikes if f["tipo"] == "put"], default=1.0) or 1.0

            agrupado_por_strike = _agrupar_filas_strikes_por_precio(tabla_strikes)

            strikes_arriba = sorted([s for s in agrupado_por_strike if s > precio_actual])
            strikes_abajo = sorted([s for s in agrupado_por_strike if s <= precio_actual], reverse=True)

            def _render_fila_book(strike, lado_call, lado_put):
                """
                Una fila del book: barra CALL (izquierda, crece hacia el
                centro desde afuera) | PRECIO (centro) | barra PUT
                (derecha, crece hacia afuera desde el centro). Si un lado
                no tiene datos para este strike, ese lado queda vacío.
                """

                col_call, col_precio, col_put = st.columns([2.4, 1.1, 2.4])

                with col_call:
                    if lado_call:
                        ancho_pct = max(round((lado_call["score"] / score_max_call) * 100), 2)
                        cambio = lado_call["cambio_oi"]
                        cambio_txt = f"Δ{cambio:+.1f}%" if cambio is not None else "Δ s/h"
                        st.markdown(
                            f"""
                            <div style="display:flex;flex-direction:column;align-items:flex-end;">
                                <div style="font-size:10px;color:#fca5a5;margin-bottom:2px;">
                                    OI {lado_call['oi']:,.0f} · {cambio_txt}
                                </div>
                                <div style="background:#1e2128;border-radius:4px;height:16px;width:100%;display:flex;justify-content:flex-end;overflow:hidden;">
                                    <div style="background:#ef4444;height:16px;border-radius:4px;width:{ancho_pct}%;"></div>
                                </div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            """<div style="height:34px;"></div>""",
                            unsafe_allow_html=True,
                        )

                with col_precio:
                    st.markdown(
                        f"""<div style="text-align:center;font-size:14px;font-weight:600;color:#e5e5e5;padding-top:6px;">
                        ${strike:,.0f}
                        </div>""",
                        unsafe_allow_html=True,
                    )

                with col_put:
                    if lado_put:
                        ancho_pct = max(round((lado_put["score"] / score_max_put) * 100), 2)
                        cambio = lado_put["cambio_oi"]
                        cambio_txt = f"Δ{cambio:+.1f}%" if cambio is not None else "Δ s/h"
                        st.markdown(
                            f"""
                            <div style="display:flex;flex-direction:column;align-items:flex-start;">
                                <div style="font-size:10px;color:#86efac;margin-bottom:2px;">
                                    OI {lado_put['oi']:,.0f} · {cambio_txt}
                                </div>
                                <div style="background:#1e2128;border-radius:4px;height:16px;width:100%;overflow:hidden;">
                                    <div style="background:#22c55e;height:16px;border-radius:4px;width:{ancho_pct}%;"></div>
                                </div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown(
                            """<div style="height:34px;"></div>""",
                            unsafe_allow_html=True,
                        )

            # --- Encabezado de columnas (CALL / PUT) ---
            col_h_call, col_h_precio, col_h_put = st.columns([2.4, 1.1, 2.4])
            with col_h_call:
                st.markdown(
                    """<div style="text-align:right;font-size:12px;font-weight:600;color:#fca5a5;">🔴 CALL</div>""",
                    unsafe_allow_html=True,
                )
            with col_h_precio:
                st.markdown(
                    """<div style="text-align:center;font-size:12px;font-weight:600;color:var(--text-secondary, #9aa0a6);">precio</div>""",
                    unsafe_allow_html=True,
                )
            with col_h_put:
                st.markdown(
                    """<div style="text-align:left;font-size:12px;font-weight:600;color:#86efac;">🟢 PUT</div>""",
                    unsafe_allow_html=True,
                )

            st.markdown(
                """<div style="border-top:0.5px solid #3a3a3a;margin:4px 0 6px 0;"></div>""",
                unsafe_allow_html=True,
            )

            # --- Strikes por arriba del precio (más alto primero, el más cercano queda pegado al separador) ---
            for strike in reversed(strikes_arriba):
                lado = agrupado_por_strike[strike]
                _render_fila_book(strike, lado["call"], lado["put"])

            # --- Separador: PRECIO ACTUAL cruzando el eje en su posición real ---
            st.markdown(
                f"""
                <div style="display:flex;align-items:center;justify-content:center;margin:6px 0;padding:6px 10px;
                background:rgba(255,140,0,0.15);border-top:1px solid #ff8c00;border-bottom:1px solid #ff8c00;">
                    <span style="font-size:14px;font-weight:700;color:#ff8c00;">💲 ${precio_actual:,.0f} precio actual</span>
                </div>
                """,
                unsafe_allow_html=True,
            )

            # --- Strikes por debajo del precio (más cercano primero, bajando) ---
            for strike in strikes_abajo:
                lado = agrupado_por_strike[strike]
                _render_fila_book(strike, lado["call"], lado["put"])

            st.divider()

            # ----------------------------------
            # GREEKS POR STRIKE — MISMO FORMATO BOOK (Delta · Vega · Theta)
            # ----------------------------------
            #
            # FIX (pedido del usuario): este bloque antes era un
            # st.dataframe plano (sin colores ni barras, perdía la
            # jerarquía visual del heatmap de arriba). Ahora usa el MISMO
            # formato de book de profundidad: una fila por strike, Delta
            # de CALL a la izquierda, Delta de PUT a la derecha (mismo
            # criterio de escala independiente por lado que el heatmap de
            # OI), con Vega/Theta/Gamma y la lectura de decaimiento como
            # texto debajo de cada lado.
            #
            # IMPORTANTE (límite honesto, ya conversado): esto describe el
            # COMPORTAMIENTO del contrato de opción (cuánto pierde valor
            # por día, cuán sensible es a la IV, qué probabilidad
            # implícita le asigna el mercado), no predice CUÁNDO se va a
            # mover el precio de BTC. No hay una "ecuación" válida que
            # combine estos Greeks en un timing de ruptura — eso sería
            # decoración matemática, no análisis. Por eso la lectura se
            # llama "decaimiento del contrato" y no "próximo movimiento".

            st.subheader(
                "📐 Greeks por strike — Delta · Vega · Theta · Gamma",
                help=(
                    "Mismo formato de book que el heatmap de OI: Delta de CALL a la "
                    "izquierda (barra = |delta|, de 0 a 1), Delta de PUT a la derecha "
                    "(barra = |delta|, de 0 a 1). Delta ≈ probabilidad implícita del "
                    "modelo de terminar in-the-money (no es una garantía estadística). "
                    "Vega/Theta/Gamma y la lectura de decaimiento describen el "
                    "comportamiento del contrato hacia su vencimiento, no predicen el "
                    "momento ni la dirección del próximo movimiento de precio."
                ),
            )

            def _render_fila_greeks(strike, lado_call, lado_put):
                """
                Fila de Greeks en formato horizontal (pedido del usuario):
                Delta · Vega · Theta · Gamma en una sola línea arriba de la barra.
                """

                col_call, col_precio, col_put = st.columns([2.6, 1.1, 2.6])

                with col_call:
                    if lado_call:
                        ancho_pct = max(round(abs(lado_call["delta"]) * 100), 2)
                        st.markdown(
                            f"""
                            <div style="display:flex;flex-direction:column;align-items:flex-end;">
                                <div style="font-size:10.5px;color:#fca5a5;line-height:1.35;margin-bottom:4px;">
                                    Δ <b>{lado_call['delta']:+.3f}</b> &nbsp;&nbsp;
                                    ν <b>{lado_call['vega']:.2f}</b> &nbsp;&nbsp;
                                    θ <b>{lado_call['theta']:.2f}</b> &nbsp;&nbsp;
                                    Γ <b>{lado_call['gamma']:.6f}</b>
                                </div>
                                <div style="background:#1e2128;border-radius:4px;height:16px;width:100%;display:flex;justify-content:flex-end;overflow:hidden;">
                                    <div style="background:#ef4444;height:16px;border-radius:4px;width:{ancho_pct}%;"></div>
                                </div>
                                <div style="font-size:9px;color:#9a9a9a;margin-top:3px;text-align:right;">
                                    {lado_call['lectura_decaimiento']}
                                </div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown("""<div style="height:85px;"></div>""", unsafe_allow_html=True)

                with col_precio:
                    st.markdown(
                        f"""<div style="text-align:center;font-size:14px;font-weight:600;color:#e5e5e5;padding-top:8px;">
                        ${strike:,.0f}
                        </div>""",
                        unsafe_allow_html=True,
                    )

                with col_put:
                    if lado_put:
                        ancho_pct = max(round(abs(lado_put["delta"]) * 100), 2)
                        st.markdown(
                            f"""
                            <div style="display:flex;flex-direction:column;align-items:flex-start;">
                                <div style="font-size:10.5px;color:#86efac;line-height:1.35;margin-bottom:4px;">
                                    Δ <b>{lado_put['delta']:+.3f}</b> &nbsp;&nbsp;
                                    ν <b>{lado_put['vega']:.2f}</b> &nbsp;&nbsp;
                                    θ <b>{lado_put['theta']:.2f}</b> &nbsp;&nbsp;
                                    Γ <b>{lado_put['gamma']:.6f}</b>
                                </div>
                                <div style="background:#1e2128;border-radius:4px;height:16px;width:100%;overflow:hidden;">
                                    <div style="background:#22c55e;height:16px;border-radius:4px;width:{ancho_pct}%;"></div>
                                </div>
                                <div style="font-size:9px;color:#9a9a9a;margin-top:3px;text-align:left;">
                                    {lado_put['lectura_decaimiento']}
                                </div>
                            </div>
                            """,
                            unsafe_allow_html=True,
                        )
                    else:
                        st.markdown("""<div style="height:85px;"></div>""", unsafe_allow_html=True)
                        
            # --- Encabezado ---
            col_hg_call, col_hg_precio, col_hg_put = st.columns([2.6, 1.1, 2.6])
            with col_hg_call:
                st.markdown(
                    """<div style="text-align:right;font-size:12px;font-weight:600;color:#fca5a5;">🔴 CALL — |Delta|</div>""",
                    unsafe_allow_html=True,
                )
            with col_hg_precio:
                st.markdown(
                    """<div style="text-align:center;font-size:12px;font-weight:600;color:var(--text-secondary, #9aa0a6);">precio</div>""",
                    unsafe_allow_html=True,
                )
            with col_hg_put:
                st.markdown(
                    """<div style="text-align:left;font-size:12px;font-weight:600;color:#86efac;">🟢 PUT — |Delta|</div>""",
                    unsafe_allow_html=True,
                )

            st.markdown(
                """<div style="border-top:0.5px solid #3a3a3a;margin:4px 0 6px 0;"></div>""",
                unsafe_allow_html=True,
            )

            for strike in reversed(strikes_arriba):
                lado = agrupado_por_strike[strike]
                _render_fila_greeks(strike, lado["call"], lado["put"])

            st.markdown(
                f"""
                <div style="display:flex;align-items:center;justify-content:center;margin:6px 0;padding:6px 10px;
                background:rgba(255,140,0,0.15);border-top:1px solid #ff8c00;border-bottom:1px solid #ff8c00;">
                    <span style="font-size:14px;font-weight:700;color:#ff8c00;">💲 ${precio_actual:,.0f} precio actual</span>
                </div>
                """,
                unsafe_allow_html=True,
            )

            for strike in strikes_abajo:
                lado = agrupado_por_strike[strike]
                _render_fila_greeks(strike, lado["call"], lado["put"])

            st.caption(
                "⚠️ Δ (delta) ≈ probabilidad implícita (bajo el modelo, no garantía "
                "estadística) de terminar in-the-money. ν (vega) = sensibilidad a 1 "
                "punto de IV. θ (theta) = pérdida de valor diaria del contrato, todo "
                "lo demás constante. Γ (gamma) ya determina el score de las Walls. La "
                "lectura de decaimiento describe el comportamiento del CONTRATO hacia "
                "su vencimiento — no predice cuándo ni hacia dónde se va a mover el "
                "precio de BTC. Ningún campo de este panel es una señal de entrada o salida."
            )

    st.divider()


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
                "🔁 Flip Semanal (Global — Mediano a Largo Plazo)",
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
                st.caption(f"No se detectó cruce de signo dentro de ±{RANGO_FLIP_GLOBAL_PCT*100:.0f}% en los vencimientos agregados.")

        with col_f2:

            st.subheader(
                "🔁 Flip Cercano (Local — Corto Plazo)",
                help=(
                    "Lo mismo que el Flip Semanal, pero calculado solo con los "
                    "vencimientos de opciones de corto plazo que concentran carga real "
                    "(OI x gamma) — entre 1 y 3, el más próximo siempre incluido. "
                    "Reacciona más rápido a cambios de posicionamiento de corto plazo — "
                    "más relevante para operativa intradiaria o de Scalp que el Flip Semanal."
                ),
            )

            if resultado_flip_local:
                gex_spot_local = resultado_flip_local["gex_spot"]
                contexto_local = "🟢 Long Gamma" if gex_spot_local > 0 else "🔴 Short Gamma"
                venc_lista_txt = (
                    ", ".join(v.strftime("%d-%b") for v in vencimientos_local)
                    if vencimientos_local else "N/D"
                )

                if resultado_flip_local["flip_point"]:
                    fp_local = resultado_flip_local["flip_point"]
                    dist_local = ((fp_local - precio_actual) / precio_actual) * 100
                    lado_local = "🟢 defendido como soporte (comprador)" if fp_local < precio_actual else "🔴 defendido como resistencia (vendedor)"
                    flip_txt = f"**Flip Point:** ${fp_local:,.0f} ({dist_local:+.2f}% desde el spot)\n**Lado dominante:** {lado_local}"
                else:
                    flip_txt = f"**Flip Point:** sin cruce dentro de ±{RANGO_FLIP_LOCAL_PCT*100:.1f}% (está fuera del rango explorado)"

                st.info(
                    f"""
    {flip_txt}
    **Régimen actual:** {contexto_local}
    **Vencimientos usados ({len(vencimientos_local)}):** {venc_lista_txt}
    """
                )
            else:
                st.caption("Sin datos suficientes en los vencimientos cercanos para calcular el régimen.") 

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
with tab_dashboard:

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

    st.subheader(
        "🧠 Market Intelligence",
        help=(
            "Distinto del panel '🧑‍🤝‍🧑 Participantes de mercado' de más arriba: "
            "ese es un reparto de 3 categorías (MM/Retail/Institucional) enfocado en "
            "el microflujo ACTUAL. Este es un puntaje estructural de 2 vías "
            "(Institucional vs Retail) sobre OI, funding, flow y tendencia de 1H — "
            "pensado como contexto de fondo, no de entrada inmediata."
        ),
    )

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
    # Normalización: institucional_score y retail_score son dos puntajes
    # acumulativos INDEPENDIENTES (cada uno puede llegar a 100 por su
    # cuenta), por eso antes podían mostrar, ej., 25% y 15% sin que el
    # resto (60%) quedara representado en ningún lado. Para mostrar dos
    # porcentajes que SIEMPRE suman 100%, los normalizamos acá -- pero
    # dejamos institucional_score/retail_score originales intactos para
    # la comparación de "Control actual" más abajo, que sigue usando los
    # valores crudos (no normalizados).

    total_score_mi = institucional_score + retail_score

    if total_score_mi > 0:
        institucional_pct = round((institucional_score / total_score_mi) * 100)
        retail_pct = 100 - institucional_pct
    else:
        institucional_pct = 50
        retail_pct = 50

    col1, col2 = st.columns(2)

    with col1:
        st.metric("🏦 Institucional", f"{institucional_pct}%")

    with col2:
        st.metric("👤 Retail", f"{retail_pct}%")

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
    # CEREBRO GENERAL COPILOT - LECTURA SCALP MEJORADA
    # -----------------------------

    if modo in ("Scalp", "Microscalp"):

        confluencia_scalp = scalp_edge
        etiqueta_modo = "MICROSCALP" if modo == "Microscalp" else "SCALP"

        # TP/Stop escalados por modo: Microscalp opera en una escala de
        # precio mucho más chica (1M puro, flip local ±0.8%) — mantener
        # los mismos 500-1200 USD de Scalp ahí sería pedirle al mercado
        # un movimiento que rara vez llega en ese horizonte de tiempo.
        if modo == "Microscalp":
            tabla_gestion = {
                "ALTA":     ("120-220 USD", "60-100 USD"),
                "BUENA":    ("100-180 USD", "70-110 USD"),
                "MODERADA": ("90-150 USD",  "60-100 USD"),
                "BAJA":     ("80-130 USD",  "80-140 USD"),
            }
        else:
            tabla_gestion = {
                "ALTA":     ("700-1200 USD", "140-220 USD"),
                "BUENA":    ("600-950 USD",  "180-280 USD"),
                "MODERADA": ("550-850 USD",  "140-230 USD"),
                "BAJA":     ("500-750 USD",  "200-350 USD"),
            }

        # Umbrales de confianza alineados con el Scalp Edge Score
        # recalibrado (antes 78/62, casi nunca alcanzables; ver desglose
        # de componentes más arriba para el detalle).
        if confluencia_scalp >= 65:
            confianza = "ALTA"
        elif confluencia_scalp >= 45:
            confianza = "BUENA"
        elif hay_absorcion and en_zona_relevante:
            confianza = "MODERADA"
        else:
            confianza = "BAJA"

        tp_sugerido, stop_sugerido = tabla_gestion[confianza]

        if confianza == "ALTA":
            lectura = (
                f"⚡ **{etiqueta_modo} {confianza}** — Excelente setup.\n\n"
                f"Absorción + Flip Local + presión alineados.\n"
                f"**Stop:** {stop_sugerido} | **TP inicial:** {tp_sugerido}\n"
                "Dejar correr parte si rompe con volumen y momentum."
            )
        elif confianza == "BUENA":
            lectura = (
                f"⚡ **{etiqueta_modo} {confianza}** — Setup válido.\n\n"
                f"Confluencia aceptable.\n"
                f"**Stop:** {stop_sugerido} | **TP primario:** {tp_sugerido}"
            )
        elif confianza == "MODERADA":
            lectura = (
                f"⚡ **Absorción en zona clave** — Posible rebote.\n\n"
                f"**Stop:** {stop_sugerido} | **TP:** {tp_sugerido}"
            )
        elif estado_velocidad == "acelerando" and (buy_pressure > 63 or sell_pressure > 63):
            lectura = (
                f"⚡ **Impulso fuerte** — Momentum presente.\n\n"
                f"**Stop:** {stop_sugerido} | **TP:** {tp_sugerido}"
            )
        else:
            lectura = (
                f"⚡ **Sin confluencia clara para {etiqueta_modo.lower()}** — Mejor esperar.\n\n"
                f"Scalp Edge {confluencia_scalp}/100 (umbral BUENA: 45). "
                "Esperar absorción o Flip Local cercano antes de operar — ver "
                "desglose de puntos más arriba para saber qué falta."
            )

        st.info(lectura)

        st.caption(
            f"**Gestión recomendada:** Stop {stop_sugerido} | "
            f"TP {tp_sugerido} | R:R mínimo 1:2.5"
        )

    else:
        # Lectura para Modo Normal
        if institucional_score > retail_score:
            lectura = "🧠 Normal: control institucional dominante. Analizando continuidad."
        elif retail_score > institucional_score:
            lectura = "🧠 Normal: presión retail predominante. Evaluar posibles trampas."
        else:
            lectura = "🧠 Normal: mercado equilibrado esperando confirmación."

        st.caption(f"📌 Lectura: {lectura}")

    st.divider()
    st.caption(
        "⚠️ **Aviso importante:** este dashboard combina datos de mercado (Binance, "
        "Bybit, Deribit) con cálculos e inferencias propias (Dealer Score, Flip Points, "
        "Walls, niveles Imán, candidatos de absorción, lecturas de Scalp/Normal). "
        "Ninguna lectura, métrica o 'candidato' mostrado en esta página constituye "
        "una recomendación de inversión ni una señal de entrada o salida. Toda "
        "sugerencia o interpretación que pueda desprenderse de estos datos queda "
        "sujeta a la validación y aprobación propia de cada usuario, considerando "
        "siempre la confirmación real del mercado antes de actuar — los niveles "
        "proyectados (Flip, Walls, Imán, Absorción) son zonas de mayor probabilidad "
        "estadística, no garantías de reacción del precio."
    )

with tab_profundidad:

    # ----------------------------------
    # 🌊 PROFUNDIDAD DE MERCADO (order book) — TEMPORALMENTE DESACTIVADA
    # ----------------------------------
    #
    # DESACTIVADO A PROPÓSITO (09/07/2026): esta tab agregaba 2 requests
    # extra por refresh (/depth spot + /futures/depth) sobre la misma IP
    # del proxy que ya venía sufriendo bans -1003 por exceso de peso.
    # No era la causa raíz (esa era la falta de cache en klines/ticker,
    # ya resuelta en app.py), pero se saca de circulación mientras se
    # confirma que el fix del proxy (cache TTL + circuit breaker)
    # estabiliza el resto del dashboard, y para retomarla desde una
    # base más simple.
    #
    # El código completo de la implementación (heatmap, imbalance,
    # comparación spot vs futuros) sigue intacto en market_depth.py y
    # en tab_profundidad_backup.txt (respaldo del bloque que iba acá) —
    # no se perdió nada, solo se dejó de EJECUTAR. Para reactivarla,
    # pegar el contenido de ese backup en este bloque.

    st.subheader("🌊 Profundidad de Mercado — Spot vs Futuros")
    st.info(
        "🔧 Esta sección está temporalmente desactivada mientras se estabiliza "
        "el proxy (evita requests extra a Binance mientras se confirma que el "
        "fix de cache/rate-limit está funcionando). Va a volver a activarse "
        "en un próximo update."
    )

# ----------------------------------
# FOOTER DE MANTENIMIENTO (changelog manual del CÓDIGO)
# ----------------------------------
#
# Esto NO tiene que ver con el refresco de datos en vivo (eso ya se
# avisa cerca del gráfico: "Actualización automática cada 15 seg.").
# Es información de mantenimiento de la propia web: cuándo fue la
# última vez que el código del dashboard fue editado/publicado, y qué
# versión de build es. Son constantes fijas (VERSION_APP,
# FECHA_ULTIMA_ACTUALIZACION) definidas al principio del archivo —
# actualizalas a mano cada vez que publiques un cambio nuevo.

st.markdown(
    f"""
    <div style="text-align:center; font-size:11px; color:#5c6370; margin-top:18px;">
        Última actualización del sistema: {FECHA_ULTIMA_ACTUALIZACION} &nbsp;·&nbsp; {VERSION_APP}
    </div>
    """,
    unsafe_allow_html=True,
)
