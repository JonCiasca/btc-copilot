"""
confluencia_log.py — Recopilación de resultados de "Confluencia by JonFlowMDQ"

Pedido explícito de Jon: no alcanza con mostrar la lectura en vivo del
panel -- hace falta guardar cada llamada (Setup 1: confluencia MTF;
Setup 2: continuación/rechazo del retest de vacío), chequear después
qué hizo el precio, y juntar estadísticas REALES en vez de umbrales
puestos a ojo. Esta es la base: registra eventos y evalúa resultados.
Las estadísticas se muestran en la solapa nueva "📈 Confluencia Stats"
de main.py.

ALMACENAMIENTO: archivo JSON local (confluencia_log.json), mismo
patrón que ya usa el repo para contador_sesiones.json.

LIMITACIÓN HONESTA (mismo espíritu que ya usa el repo para el contador
de sesiones): en el plan gratuito de Streamlit Community Cloud este
archivo puede resetearse en un redeploy, o si la app se duerme por
inactividad. Para que sobreviva eso hace falta un storage externo
(Notion, Google Sheets, Supabase). Jon ofreció acceso a Notion -- no
lo conecté todavía porque hace falta su token de integración + una
página compartida con esa integración para crear la base ahí. Con
esos dos datos se agrega un sync sin tocar esta base local (que sigue
funcionando igual, Notion sería un espejo).

Este módulo es puro Python (sin streamlit), para poder testearlo con
datos sintéticos sin levantar el dashboard.
"""

import json
import os
import uuid
from datetime import datetime, timedelta

RUTA_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "confluencia_log.json")

# No loguea la MISMA lectura en cada refresh de 15s -- solo registra
# un evento nuevo si cambió el escenario respecto del último evento de
# la misma fuente, o si ya pasó el cooldown desde el último.
COOLDOWN_MINUTOS = 10
VENTANA_EVALUACION_MINUTOS = 30  # cuánto esperar antes de chequear si acertó


def _cargar_log():
    """Lee el log desde disco. Nunca explota por esto: archivo
    ausente o corrupto -> lista vacía."""
    if not os.path.exists(RUTA_LOG):
        return []
    try:
        with open(RUTA_LOG, "r", encoding="utf-8") as f:
            datos = json.load(f)
        return datos if isinstance(datos, list) else []
    except Exception:
        return []


def _guardar_log(eventos):
    try:
        with open(RUTA_LOG, "w", encoding="utf-8") as f:
            json.dump(eventos, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def _ultimo_evento(eventos, fuente):
    candidatos = [e for e in eventos if e.get("fuente") == fuente]
    if not candidatos:
        return None
    return max(candidatos, key=lambda e: e.get("ts", ""))


def _debe_registrar(eventos, fuente, escenario_actual):
    ultimo = _ultimo_evento(eventos, fuente)
    if ultimo is None:
        return True
    if ultimo.get("escenario") != escenario_actual:
        return True
    try:
        ts_ultimo = datetime.fromisoformat(ultimo["ts"])
    except Exception:
        return True
    return datetime.now() - ts_ultimo >= timedelta(minutes=COOLDOWN_MINUTOS)


def registrar_evento_setup1(resultado_confluencia_mtf, precio_actual):
    """Registra un evento cuando Setup 1 muestra confluencia direccional
    (alcista o bajista) -- "sin confluencia" no se loguea, no hay nada
    que evaluar ahí."""
    escenario = resultado_confluencia_mtf.get("direccion")
    if not escenario or escenario == "sin confluencia":
        return

    eventos = _cargar_log()
    if not _debe_registrar(eventos, "setup1_mtf", escenario):
        return

    eventos.append({
        "id": str(uuid.uuid4()),
        "ts": datetime.now().isoformat(),
        "fuente": "setup1_mtf",
        "escenario": escenario,
        "precio_al_registrar": float(precio_actual),
        "factores": {
            "coincidentes": resultado_confluencia_mtf.get("coincidentes"),
            "fuerza": resultado_confluencia_mtf.get("fuerza"),
        },
        "resultado": "pendiente",
        "precio_resultado": None,
        "ts_resultado": None,
    })
    _guardar_log(eventos)


def registrar_evento_setup2(retest_vacio, precio_actual):
    """Registra un evento SOLO cuando Setup 2 confirma continuación o
    rechazo -- "en_definicion" y "sin retest" todavía no tienen nada
    confirmado para evaluar."""
    escenario = retest_vacio.get("escenario")
    if escenario not in ("continuacion", "rechazo"):
        return

    eventos = _cargar_log()
    if not _debe_registrar(eventos, "setup2_vacio", escenario):
        return

    eventos.append({
        "id": str(uuid.uuid4()),
        "ts": datetime.now().isoformat(),
        "fuente": "setup2_vacio",
        "escenario": escenario,
        "precio_al_registrar": float(precio_actual),
        "factores": {
            "direccion_quiebre": retest_vacio.get("direccion"),
            "votos_a_favor": retest_vacio.get("votos_a_favor"),
            "votos_en_contra": retest_vacio.get("votos_en_contra"),
            "hay_liquidez_para_barrer": retest_vacio.get("hay_liquidez_para_barrer"),
        },
        "resultado": "pendiente",
        "precio_resultado": None,
        "ts_resultado": None,
    })
    _guardar_log(eventos)


def _direccion_esperada(evento):
    """Traduce el escenario del evento a la dirección de precio que
    tendría que darse para contarlo como acierto."""
    fuente = evento.get("fuente")
    escenario = evento.get("escenario")

    if fuente == "setup1_mtf":
        return escenario  # "alcista" / "bajista" directo

    if fuente == "setup2_vacio":
        direccion_quiebre = evento.get("factores", {}).get("direccion_quiebre")
        if escenario == "continuacion":
            return direccion_quiebre
        if escenario == "rechazo":
            # rechazo = precio se da vuelta EN CONTRA del quiebre original
            if direccion_quiebre == "bajista":
                return "alcista"
            if direccion_quiebre == "alcista":
                return "bajista"

    return None


def evaluar_pendientes(precio_actual):
    """
    Recorre los eventos "pendiente" cuya ventana de evaluación ya
    pasó (VENTANA_EVALUACION_MINUTOS desde que se registraron) y los
    marca "acierto"/"fallo" comparando precio_actual contra el precio
    al momento de registrar, en la dirección esperada.

    precio_actual: último precio conocido (main.py ya lo tiene
    calculado, no se pide nada nuevo a la red acá).
    """
    eventos = _cargar_log()
    if not eventos:
        return 0

    ts_ahora = datetime.now()
    actualizados = 0

    for evento in eventos:
        if evento.get("resultado") != "pendiente":
            continue
        try:
            ts_evento = datetime.fromisoformat(evento["ts"])
        except Exception:
            continue

        minutos_transcurridos = (ts_ahora - ts_evento).total_seconds() / 60
        if minutos_transcurridos < VENTANA_EVALUACION_MINUTOS:
            continue

        direccion_esperada = _direccion_esperada(evento)
        precio_inicial = evento.get("precio_al_registrar")
        if direccion_esperada is None or precio_inicial is None:
            evento["resultado"] = "sin_evaluar"
            continue

        if direccion_esperada == "alcista":
            acierto = precio_actual > precio_inicial
        else:
            acierto = precio_actual < precio_inicial

        evento["resultado"] = "acierto" if acierto else "fallo"
        evento["precio_resultado"] = float(precio_actual)
        evento["ts_resultado"] = ts_ahora.isoformat()
        actualizados += 1

    if actualizados > 0:
        _guardar_log(eventos)

    return actualizados


def _resumen(lista):
    total = len(lista)
    aciertos = sum(1 for e in lista if e.get("resultado") == "acierto")
    win_rate = round((aciertos / total) * 100, 1) if total > 0 else None
    return {"total": total, "aciertos": aciertos, "fallos": total - aciertos, "win_rate": win_rate}


def calcular_estadisticas(eventos=None):
    """
    Agrega estadísticas sobre los eventos ya evaluados (ignora
    "pendiente"/"sin_evaluar"). Incluye desglose por factor -- pedido
    explícito de Jon: "recopilen los datos tipo y factores positivos
    y negativos" -- para poder ver qué condiciones correlacionan con
    acierto en vez de asumirlo.
    """
    if eventos is None:
        eventos = _cargar_log()

    evaluados = [e for e in eventos if e.get("resultado") in ("acierto", "fallo")]

    por_fuente_escenario = {}
    for fuente in ("setup1_mtf", "setup2_vacio"):
        for escenario in ("alcista", "bajista", "continuacion", "rechazo"):
            lista = [e for e in evaluados if e.get("fuente") == fuente and e.get("escenario") == escenario]
            if lista:
                por_fuente_escenario[f"{fuente}:{escenario}"] = _resumen(lista)

    s2 = [e for e in evaluados if e.get("fuente") == "setup2_vacio"]
    con_liquidez = [e for e in s2 if e.get("factores", {}).get("hay_liquidez_para_barrer")]
    sin_liquidez = [e for e in s2 if not e.get("factores", {}).get("hay_liquidez_para_barrer")]

    s1 = [e for e in evaluados if e.get("fuente") == "setup1_mtf"]
    fuerza_alta = [e for e in s1 if (e.get("factores", {}).get("fuerza") or 0) >= 8]
    fuerza_baja = [e for e in s1 if (e.get("factores", {}).get("fuerza") or 0) < 8]

    return {
        "total_eventos": len(eventos),
        "pendientes": sum(1 for e in eventos if e.get("resultado") == "pendiente"),
        "evaluados": len(evaluados),
        "por_fuente_escenario": por_fuente_escenario,
        "setup2_con_liquidez": _resumen(con_liquidez),
        "setup2_sin_liquidez": _resumen(sin_liquidez),
        "setup1_fuerza_alta": _resumen(fuerza_alta),
        "setup1_fuerza_baja": _resumen(fuerza_baja),
    }
