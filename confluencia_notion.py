"""
confluencia_notion.py — Espejo opcional del log de Confluencia en Notion

Pedido de Jon: que las estadísticas de confluencia_log.py sobrevivan a
un redeploy de Streamlit Cloud (el archivo JSON local no lo garantiza).
Este módulo sincroniza el mismo log a una base de datos de Notion,
como espejo -- confluencia_log.py sigue siendo la fuente de verdad
local, esto solo empuja una copia hacia afuera.

TOTALMENTE OPCIONAL Y DEFENSIVO: si no hay token configurado, si
Notion no responde, si se corta la red, o si pasa cualquier cosa
rara -- no rompe nada. Cada función atrapa sus propios errores y
devuelve un resultado neutro (None/False/0) en vez de propagar la
excepción. main.py llama a esto adentro del mismo try/except general
del panel, pero está pensado para no necesitarlo siquiera.

CONFIGURACIÓN (no se commitea nada de esto al repo):
  - NOTION_TOKEN: token de integración interna de Notion
    ("secret_..." o "ntn_..."), cargado desde st.secrets en
    Streamlit Cloud (Settings → Secrets de la app), NUNCA hardcodeado
    acá ni en git.
  - NOTION_PARENT_PAGE_ID: id de la página de Notion (compartida con
    la integración) donde se crea la base "BTC Copilot — Confluencia
    Log" la primera vez que corre.

No probado contra la API real todavía -- el sandbox donde se escribió
este código no tiene salida de red a api.notion.com (bloqueado por el
proxy del entorno). Corre recién en Streamlit Cloud, donde si hay
salida a internet. Primera corrida real: revisar la solapa
"📈 Confluencia Stats" para el estado de conexión.
"""

import json
import os
import requests

import confluencia_log as clog

NOTION_VERSION = "2022-06-28"
NOTION_API = "https://api.notion.com/v1"
TIMEOUT = 10

RUTA_CACHE_DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "confluencia_notion_db.json")

MAX_EVENTOS_POR_CICLO = 5  # limita cuántas llamadas a la API se hacen por refresh (rate limit de Notion)


def _headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Notion-Version": NOTION_VERSION,
        "Content-Type": "application/json",
    }


def verificar_conexion(token):
    """Ping simple para saber si el token es válido y hay red. Nunca
    explota -- devuelve (ok: bool, detalle: str)."""
    if not token:
        return False, "Sin NOTION_TOKEN configurado"
    try:
        r = requests.get(f"{NOTION_API}/users/me", headers=_headers(token), timeout=TIMEOUT)
        if r.status_code == 200:
            return True, "Conectado"
        return False, f"Notion respondió {r.status_code}: {r.text[:200]}"
    except Exception as e:
        return False, f"Sin conexión a Notion: {e}"


def _cargar_cache_db():
    if not os.path.exists(RUTA_CACHE_DB):
        return {}
    try:
        with open(RUTA_CACHE_DB, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _guardar_cache_db(datos):
    try:
        with open(RUTA_CACHE_DB, "w", encoding="utf-8") as f:
            json.dump(datos, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_ESQUEMA_BASE = {
    "Nombre": {"title": {}},
    "Fuente": {"select": {"options": [
        {"name": "setup1_mtf", "color": "blue"},
        {"name": "setup2_vacio", "color": "purple"},
    ]}},
    "Escenario": {"select": {"options": [
        {"name": "alcista", "color": "green"},
        {"name": "bajista", "color": "red"},
        {"name": "continuacion", "color": "green"},
        {"name": "rechazo", "color": "orange"},
    ]}},
    "Resultado": {"select": {"options": [
        {"name": "pendiente", "color": "yellow"},
        {"name": "acierto", "color": "green"},
        {"name": "fallo", "color": "red"},
        {"name": "sin_evaluar", "color": "gray"},
    ]}},
    "Precio registro": {"number": {"format": "dollar"}},
    "Precio resultado": {"number": {"format": "dollar"}},
    "Fecha registro": {"date": {}},
    "Fecha resultado": {"date": {}},
    "Factores": {"rich_text": {}},
}


def obtener_o_crear_base(token, parent_page_id):
    """
    Devuelve el database_id de "BTC Copilot — Confluencia Log",
    creándolo la primera vez (queda cacheado en disco para no
    recrearlo en cada refresh). None si algo falla -- el caller debe
    tratar eso como "sync no disponible este ciclo", no como error
    fatal.
    """
    if not token or not parent_page_id:
        return None

    cache = _cargar_cache_db()
    if cache.get("database_id") and cache.get("parent_page_id") == parent_page_id:
        return cache["database_id"]

    try:
        payload = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"type": "text", "text": {"content": "BTC Copilot — Confluencia Log"}}],
            "properties": _ESQUEMA_BASE,
        }
        r = requests.post(f"{NOTION_API}/databases", headers=_headers(token), json=payload, timeout=TIMEOUT)
        if r.status_code not in (200, 201):
            return None
        database_id = r.json().get("id")
        if not database_id:
            return None
        _guardar_cache_db({"database_id": database_id, "parent_page_id": parent_page_id})
        return database_id
    except Exception:
        return None


def _evento_a_propiedades(evento):
    fuente = evento.get("fuente", "")
    escenario = evento.get("escenario", "")
    resultado = evento.get("resultado", "pendiente")
    factores_txt = json.dumps(evento.get("factores", {}), ensure_ascii=False)[:1900]  # límite de rich_text

    props = {
        "Nombre": {"title": [{"text": {"content": f"{fuente} · {escenario} · {evento.get('ts', '')[:16]}"}}]},
        "Fuente": {"select": {"name": fuente}} if fuente else None,
        "Escenario": {"select": {"name": escenario}} if escenario else None,
        "Resultado": {"select": {"name": resultado}} if resultado else None,
        "Precio registro": {"number": evento.get("precio_al_registrar")},
        "Fecha registro": {"date": {"start": evento["ts"]}} if evento.get("ts") else None,
        "Factores": {"rich_text": [{"text": {"content": factores_txt}}]},
    }
    if evento.get("precio_resultado") is not None:
        props["Precio resultado"] = {"number": evento["precio_resultado"]}
    if evento.get("ts_resultado"):
        props["Fecha resultado"] = {"date": {"start": evento["ts_resultado"]}}

    return {k: v for k, v in props.items() if v is not None}


def sincronizar(token, parent_page_id):
    """
    Empuja al log de Notion los eventos nuevos (sin notion_page_id
    todavía) y actualiza los que cambiaron de resultado desde la
    última sincronización. Limitado a MAX_EVENTOS_POR_CICLO por
    llamada para no pasarse del rate limit de Notion ni frenar un
    refresh del dashboard.

    Devuelve (creados, actualizados) -- (0, 0) si no hay nada para
    hacer o si el sync no está disponible (sin token, sin red, etc).
    """
    database_id = obtener_o_crear_base(token, parent_page_id)
    if not database_id:
        return 0, 0

    eventos = clog.cargar_eventos()
    if not eventos:
        return 0, 0

    creados = 0
    actualizados = 0
    cambios = False

    for evento in eventos:
        if creados + actualizados >= MAX_EVENTOS_POR_CICLO:
            break

        page_id = evento.get("notion_page_id")

        try:
            if not page_id:
                payload = {"parent": {"database_id": database_id}, "properties": _evento_a_propiedades(evento)}
                r = requests.post(f"{NOTION_API}/pages", headers=_headers(token), json=payload, timeout=TIMEOUT)
                if r.status_code in (200, 201):
                    evento["notion_page_id"] = r.json().get("id")
                    evento["_notion_resultado_sincronizado"] = evento.get("resultado")
                    creados += 1
                    cambios = True
            elif evento.get("resultado") != evento.get("_notion_resultado_sincronizado"):
                payload = {"properties": _evento_a_propiedades(evento)}
                r = requests.patch(f"{NOTION_API}/pages/{page_id}", headers=_headers(token), json=payload, timeout=TIMEOUT)
                if r.status_code == 200:
                    evento["_notion_resultado_sincronizado"] = evento.get("resultado")
                    actualizados += 1
                    cambios = True
        except Exception:
            continue  # un evento con problema no frena el resto del lote

    if cambios:
        clog.guardar_eventos(eventos)

    return creados, actualizados
