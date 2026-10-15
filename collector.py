#!/usr/bin/env python3
"""
Recolector de noticias de Fortnite (costo cero, solo librería estándar).

Fuentes (fortnite-api.com, gratis, sin API key):
  - /v2/news/br           -> noticias in-game (motds)
  - /v2/cosmetics/new     -> cosméticos nuevos / filtrados
  - /v2/shop              -> tienda del día
  - /v2/aes               -> build del juego (detecta parches nuevos)

Cada ejecución:
  1. Consulta las fuentes.
  2. Compara contra state.json y detecta lo NUEVO.
  3. Redacta cada noticia en estilo rápido + meme (español).
  4. Guarda docs/news.json y docs/news.js (la web los lee; GitHub Pages sirve /docs).
  5. Opcional: publica en Telegram si hay TELEGRAM_BOT_TOKEN y TELEGRAM_CHAT_ID.

Uso:
  python collector.py            # ejecución normal
  python collector.py --demo     # usa datos de ejemplo (sin internet)
  python collector.py --loop 300 # repite cada 300 s (5 min)
  python collector.py --serve    # proceso permanente (Railway), cada fuente con su intervalo
  python collector.py --test-telegram  # mensaje de prueba al canal

Fuentes extra (solo en --serve, casi en tiempo real):
  - Bluesky de filtradores (LEAKER_HANDLES / FN_LEAKERS) y @fortnite-status
  - API de contenido de Epic (avisos de emergencia, lobby, fondos...) por huella
  - Epic Status (incidentes y mantenimientos que mencionan Fortnite)
"""
import argparse
import hashlib
import json
import base64
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://fortnite-api.com"
LANG = os.environ.get("FN_LANG", "es")
ROOT = Path(__file__).parent
SITE = ROOT / "docs"   # GitHub Pages solo publica desde / o /docs
STATE_FILE = ROOT / "state.json"
MAX_NEWS = 200
UA = "fortnite-news-bot/0.1"

# ---- fuentes en tiempo real (--serve)
BSKY_FEED = "https://api.bsky.app/xrpc/app.bsky.feed.getAuthorFeed"   # api.bsky.app: public.api.bsky.app da 403
DEFAULT_LEAKERS = ["lpleaksnews.bsky.social", "newsandleaksbr.bsky.social", "blackgokunews.bsky.social"]
LEAKER_HANDLES = [h.strip() for h in os.environ.get("FN_LEAKERS", "").split(",") if h.strip()] or DEFAULT_LEAKERS
STATUS_HANDLE = "fortnite-status.bsky.social"
MAX_POST_AGE = timedelta(hours=6)   # no volcar historial al ver una cuenta por primera vez
POST_MAX_CHARS = 600
EPIC_CONTENT_URL = "https://fortnitecontent-website-prod07.ol.epicgames.com/content/api/pages/fortnite-game"
EPIC_SECTIONS = {   # sección del JSON de Epic -> cómo se nombra en el post
    "emergencynoticev2": "aviso de emergencia",
    "dynamicbackgrounds": "fondos dinámicos",
    "lobby": "lobby",
    "loginmessage": "mensaje de inicio de sesión",
    "playlistinformation": "información de modos de juego",
}
STATUS_SUMMARY_URL = "https://status.epicgames.com/api/v2/summary.json"
STATUS_UPCOMING_URL = "https://status.epicgames.com/api/v2/scheduled-maintenances/upcoming.json"
STATUS_PAGE = "https://status.epicgames.com"
FORTNITE_COMPONENT_ID = "wf1ys2kx4pxc"
PRIORITY_KINDS = {"leak", "epic", "status"}   # van primero cuando se supera el tope de Telegram
GIT_PUSH_MIN_INTERVAL = 30   # s entre pushes (se agrupan los cambios)
BACKOFF_MAX = 300            # s, tope del backoff de una fuente que falla
PENDING_RETRY_SECS = 120     # s mínimos entre reintentos de una publicación que Telegram rechazó
EPIC_FLAP_CHANGES = 3        # cambios seguidos de una sección Epic -> se considera volátil
EPIC_MUTE_SECS = 1800        # silencio para una sección volátil
FUTURE_SKEW = timedelta(minutes=5)


# ----------------------------------------------------------------- utilidades
def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def http_json(url, headers=None, data=None, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})}, data=data)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def api(path, **params):
    params.setdefault("language", LANG)
    url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"
    return http_json(url).get("data") or {}


def pick(seed, options):
    """Elige una plantilla de forma determinista (misma noticia = mismo texto)."""
    h = int(hashlib.md5(seed.encode()).hexdigest(), 16)
    return options[h % len(options)]


def write_atomic(path, text):
    """Escribe en un temporal del mismo directorio y lo renombra: nunca queda un archivo a medias."""
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def load_state():
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text("utf-8"))
            if isinstance(state, dict) and isinstance(state.get("seen"), list):
                return state
        except (ValueError, OSError):
            pass
        # Estado ilegible: NO se vuelve a publicar todo. Se da por visto lo que ya está en la web y se
        # arranca como primera ejecución (nada va a Telegram); lo que se vea ahora queda "sembrado".
        print("[aviso] state.json ilegible; se continúa en modo silencioso", file=sys.stderr)
        try:
            ids = [n["id"] for n in load_feed() if isinstance(n, dict) and "id" in n]
        except (ValueError, OSError):
            ids = []
        return {"seen": ids, "first_run": True, "seeded": []}
    return {"seen": [], "first_run": True}


def save_state(state):
    state["seen"] = state["seen"][-3000:]
    state["first_run"] = False
    write_atomic(STATE_FILE, json.dumps(state, ensure_ascii=False, indent=1))


# ----------------------------------------------------------- estilo / plantillas
# Aquí vive TU voz. Edita estas listas para cambiar el tono.
T_NEWS = [
    "🚨 NOTICIA: {title}\n{body}",
    "👀 Epic acaba de soltar esto: {title}\n{body}",
    "⚡ Ojo, ojo: {title}\n{body}",
]
T_COSMETIC = [
    "🔥 Nuevo en los archivos: {name}{rarity}. Billetera, prepárate 💸",
    "👀 Se filtró {name}{rarity}. Que alguien avise a mi tarjeta 😭",
    "⚡ Llegó {name}{rarity}. El que no la compre, no es gamer 🗿",
]
# Cuando llegan muchos cosméticos de golpe (un parche nuevo) se publica un solo resumen.
T_COSMETIC_BATCH = [
    "🔥 {n} COSMÉTICOS NUEVOS en los archivos. Lo mejor: {names}. Tu billetera ya lloró 💸😭",
    "👀 Se filtraron {n} cosméticos. Destacan: {names}. Ahorra paVos desde YA 🗿",
]
BATCH_THRESHOLD = 5   # más de N cosméticos nuevos en una pasada -> un solo resumen
MAX_TELEGRAM_PER_RUN = 10   # tope de mensajes por pasada, por seguridad
MAX_TELEGRAM_TRIES = 3      # intentos por publicación antes de descartarla (una por pasada)
RARITY_RANK = {"mythic": 0, "legendary": 1, "icon": 1, "marvel": 1, "dc": 1, "starwars": 1,
               "epic": 2, "rare": 3, "uncommon": 4, "common": 5}
# Tienda: solo lo que entró hoy. {n} = novedades, {total} = ítems en tienda, {lines} = top por precio.
T_SHOP = [
    "🛒 TIENDA NUEVA: {n} novedades. Lo más fuerte:\n{lines}\n¿Te alcanzan los paVos? 💀",
    "💸 Llegó la tienda con {n} cosas nuevas:\n{lines}\nAhorra o llora 😭",
]
T_SHOP_SAME = [
    "🛒 Tienda renovada pero sin nada nuevo ({total} ítems repetidos). Hoy se ahorra 🗿",
]
SHOP_TOP = 6   # cuántas novedades se listan en el post de tienda
# Código de creador (Support-A-Creator). Epic exige declarar la relación cada vez que se promociona
# el código: #EpicPartner es una de las etiquetas que acepta. También está en docs/index.html.
CREATOR_CODE = "INFINITO_Z"
CREATOR_LINE = f"\n\n💜 Apoya al canal: usa el código {CREATOR_CODE} en la tienda. #EpicPartner"
T_BUILD = [
    "🚨 NUEVA VERSIÓN DETECTADA: {build}. Parche en camino, a preparar el pop-off 🔧",
    "⚙️ Cambió el build del juego: {build}. Algo se viene, mantente atento 👀",
]

# Filtraciones: SIEMPRE se deja claro que no está confirmado. Sin CREATOR_LINE (eso es solo de la tienda).
T_LEAK = [
    "🕵️ FILTRACIÓN (sin confirmar), vía @{handle}:\n\n{post}\n\n🔗 {url}\n⚠️ Es una filtración, Epic no lo ha confirmado.",
    "👀 Se filtró algo (sin confirmar), vía @{handle}:\n\n{post}\n\n🔗 {url}\n⚠️ Filtración: tómalo con pinzas hasta que Epic lo anuncie.",
    "🔎 Ojo, filtración sin confirmar, vía @{handle}:\n\n{post}\n\n🔗 {url}\n⚠️ No es oficial.",
]
T_STATUS_POST = "📢 Fortnite Status (vía @{handle}):\n\n{post}\n\n🔗 {url}"
T_EPIC = "🛰️ Epic actualizó «{label}» en el servidor de contenido del juego.{extra}"
T_STATUS_INCIDENT = "🛠️ Epic Status, Fortnite: {name}\nEstado: {status}\n{body}\nActualizado: {updated}\n{page}"
T_STATUS_COMPONENT = "⚠️ Epic Status: el componente «{name}» pasó de operativo a {status}.\nActualizado: {updated}\n{page}"
T_STATUS_MAINT = "🗓️ Mantenimiento programado que toca Fortnite: {name}\nEstado: {status}\nDesde: {start}\nHasta: {end}\n{body}\n{page}"
STATUS_ES = {
    "investigating": "investigando", "identified": "identificado", "monitoring": "monitoreando",
    "resolved": "resuelto", "postmortem": "postmortem", "scheduled": "programado",
    "in_progress": "en curso", "verifying": "verificando", "completed": "completado",
    "operational": "operativo", "degraded_performance": "rendimiento degradado",
    "partial_outage": "caída parcial", "major_outage": "caída total", "under_maintenance": "en mantenimiento",
}


def mk(kind, uid, title, text, image=None, tag=None):
    return {
        "id": uid,
        "kind": kind,
        "title": title,
        "text": text,
        "image": image,
        "tag": tag or kind,
        "ts": now_iso(),
    }


# ------------------------------------------------------------------ detectores
def big_image(url):
    """Las imágenes de noticias llegan con ?width=192; se piden a 1024 para la web y Telegram."""
    if not url or "width=" not in url:
        return url
    u = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qs(u.query)
    q["width"] = ["1024"]
    return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q, doseq=True)))


def detect_news(data):
    out = []
    for m in data.get("motds") or []:
        if m.get("hidden"):
            continue
        uid = f"news:{m.get('id') or m.get('title')}"
        title = (m.get("title") or m.get("tabTitle") or "").strip()
        body = (m.get("body") or "").strip()
        if not title:
            continue
        text = pick(uid, T_NEWS).format(title=title, body=body).strip()
        out.append(mk("news", uid, title, text, big_image(m.get("image") or m.get("tileImage")), "Noticia"))
    return out


_META_KEYS = {"build", "previousBuild", "hash", "date", "lastAddition", "globalHash", "globalLastAddition", "type"}


def _cosmetic_items(data):
    """Saca los cosméticos aunque vengan como lista, {"br": [...]} o {"br": {"items": [...]}}."""
    found, seen_ids = [], set()

    def add(x):
        if x.get("id") and x.get("name") and x["id"] not in seen_ids:
            seen_ids.add(x["id"])
            found.append(x)
            return True
        return False

    def walk(node, depth=0):
        if depth > 5:
            return
        if isinstance(node, list):
            for x in node:
                if isinstance(x, dict):
                    if not add(x):
                        walk(x, depth + 1)
                elif isinstance(x, list):
                    walk(x, depth + 1)
        elif isinstance(node, dict):
            if add(node):
                return
            for k, v in node.items():
                if k not in _META_KEYS:
                    walk(v, depth + 1)

    walk(data)
    return found


def detect_cosmetics(data):
    out = []
    for it in _cosmetic_items(data):
        uid = f"cosmetic:{it.get('id')}"
        name = it.get("name") or it.get("id")
        rar = it.get("rarity") or {}
        rarity_name = rar.get("displayValue")
        rarity = f" ({rarity_name})" if rarity_name else ""
        kind_name = (it.get("type") or {}).get("displayValue", "Cosmético")
        imgs = it.get("images") or {}
        image = imgs.get("featured") or imgs.get("icon") or imgs.get("smallIcon")
        text = pick(uid, T_COSMETIC).format(name=name, rarity=rarity)
        item = mk("cosmetic", uid, f"{kind_name}: {name}", text, image, "Cosmético")
        item["_rank"] = RARITY_RANK.get(str(rar.get("value", "")).lower(), 6)
        out.append(item)
    return out


def batch_cosmetics(items):
    """Si hay muchos cosméticos nuevos, los junta en un solo resumen."""
    if len(items) <= BATCH_THRESHOLD:
        return items
    ranked = sorted(items, key=lambda i: i.get("_rank", 6))
    names = ", ".join(i["title"].split(": ", 1)[-1] for i in ranked[:6])
    uid = "cosmetic-batch:" + hashlib.md5("".join(sorted(i["id"] for i in items)).encode()).hexdigest()[:10]
    text = pick(uid, T_COSMETIC_BATCH).format(n=len(items), names=names)
    image = next((i["image"] for i in ranked if i.get("image")), None)
    return [mk("cosmetic", uid, f"{len(items)} cosméticos nuevos", text, image, "Cosmético")]


def shop_entries(data):
    return data.get("entries") or []


def new_shop_entries(data, entries):
    """Ofertas que entraron hoy (inDate = fecha de la tienda). Sin inDate en ningún lado: todas cuentan."""
    day = (data.get("date") or "")[:10]
    if not day or not any(e.get("inDate") for e in entries):
        return entries
    return [e for e in entries if (e.get("inDate") or "")[:10] == day]


def shop_offer(e):
    """Nombre, clave para no repetir, precio e imagen de UNA oferta (todo sale del mismo ítem)."""
    br = e.get("brItems") or []
    tracks = e.get("tracks") or []
    cars = e.get("cars") or []
    bundle = e.get("bundle") or {}
    main = br[0] if br else (cars[0] if cars else {})
    main_name = (main.get("name") or (tracks[0].get("title") if tracks else "") or "").strip()
    name = (bundle.get("name") or main_name).strip()
    images = main.get("images") or {}
    render = ((e.get("newDisplayAsset") or {}).get("renderImages") or [{}])[0].get("image")
    image = bundle.get("image") or render or images.get("featured") or images.get("icon")
    if not image and tracks:
        image = tracks[0].get("albumArt")
    return {"name": name, "key": (main_name or name).lower(), "price": e.get("finalPrice") or 0, "image": image}


def pavos(n):
    return f"{n:,}".replace(",", ".")


def detect_shop(data):
    entries = shop_entries(data)
    if not entries:
        return []
    uid = f"shop:{data.get('hash') or data.get('date')}"

    # Lo más caro primero; un mismo ítem (lote y suelto) se lista una sola vez.
    offers, keys = [], set()
    for e in sorted(new_shop_entries(data, entries), key=lambda e: e.get("finalPrice") or 0, reverse=True):
        o = shop_offer(e)
        if o["name"] and o["key"] not in keys:
            keys.add(o["key"])
            offers.append(o)

    if not offers:
        text = pick(uid, T_SHOP_SAME).format(total=len(entries)) + CREATOR_LINE
        return [mk("shop", uid, "Tienda del día", text, None, "Tienda")]

    top = offers[:SHOP_TOP]
    lines = "\n".join(f"• {o['name']} — {pavos(o['price'])} paVos" for o in top)
    more = len(offers) - len(top)
    if more > 0:
        lines += f"\n…y {more} más"
    text = pick(uid, T_SHOP).format(n=len(offers), total=len(entries), lines=lines) + CREATOR_LINE
    image = next((o["image"] for o in top if o["image"]), None)
    return [mk("shop", uid, "Tienda del día", text, image, "Tienda")]


def detect_build(data):
    build = data.get("build") or data.get("version")
    if not build:
        return []
    uid = f"build:{build}"
    # "++Fortnite+Release-36.10-CL-123" -> "36.10"
    short = build.split("Release-")[-1].split("-CL")[0] if "Release-" in build else build
    return [mk("build", uid, f"Build {short}", pick(uid, T_BUILD).format(build=short), None, "Parche")]


# ------------------------------------------------- fuentes en tiempo real (--serve)
def parse_iso(value):
    """ISO 8601 -> datetime con zona (UTC si no trae). None si no se puede leer."""
    if not value:
        return None
    try:
        d = datetime.fromisoformat(re.sub(r"\.\d+", "", str(value)).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def bsky_web_url(uri, handle):
    """at://did:plc:x/app.bsky.feed.post/<rkey> -> https://bsky.app/profile/<handle>/post/<rkey>"""
    if uri.startswith("at://") and "/app.bsky.feed.post/" in uri:
        return f"https://bsky.app/profile/{handle}/post/{uri.rsplit('/', 1)[-1]}"
    return f"https://bsky.app/profile/{handle}"


_SAFE_IMG = re.compile(r"https://[^\s\"'<>`\\]+")
_SAFE_URI = re.compile(r"at://[\w:.%-]+/[\w.]+/[\w.~:%-]+")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def bsky_first_image(embed):
    """Primera imagen de un embed, también dentro de embed.media (cita con media)."""
    if not isinstance(embed, dict):
        return None
    nodes = [embed, embed.get("media")]
    for node in nodes:
        if isinstance(node, dict):
            for im in node.get("images") or []:
                url = im.get("fullsize") or im.get("thumb")
                if isinstance(url, str) and _SAFE_IMG.fullmatch(url):
                    return url
    for node in nodes:   # tarjeta de enlace con miniatura
        if isinstance(node, dict):
            thumb = (node.get("external") or {}).get("thumb")
            if isinstance(thumb, str) and _SAFE_IMG.fullmatch(thumb):
                return thumb
    return None


def bsky_post_to_item(entry, kind="leak", now=None, max_age=MAX_POST_AGE):
    """Un elemento de getAuthorFeed -> item, o None si hay que ignorarlo (repost, viejo, vacío)."""
    if "repost" in str((entry.get("reason") or {}).get("$type", "")).lower():
        return None
    post = entry.get("post") or {}
    uri = post.get("uri")
    handle = (post.get("author") or {}).get("handle")
    if not uri or not handle or not _SAFE_URI.fullmatch(uri) or not re.fullmatch(r"[\w.-]+", handle):
        return None
    record = post.get("record") or {}
    text = _CTRL.sub("", record.get("text") or "").strip()
    image = bsky_first_image(post.get("embed"))
    if not text and not image:
        return None
    created = parse_iso(record.get("createdAt") or post.get("indexedAt"))
    now = now or datetime.now(timezone.utc)
    if created is None or now - created > max_age or created - now > FUTURE_SKEW:
        return None
    if len(text) > POST_MAX_CHARS:
        text = text[:POST_MAX_CHARS].rstrip() + "…"
    url = bsky_web_url(uri, handle)
    uid = f"{kind}:{uri}"
    if kind == "leak":
        body = pick(uid, T_LEAK).format(handle=handle, post=text or "(solo imagen)", url=url)
        return mk("leak", uid, f"Filtración de @{handle}", body, image, "Filtración")
    body = T_STATUS_POST.format(handle=handle, post=text or "(solo imagen)", url=url)
    item = mk("status", uid, f"Fortnite Status (@{handle})", body, image, "Estado")
    item["_seed"] = "status_bsky"   # se siembra por fuente, no por tipo compartido con Epic Status
    return item


def fetch_bsky_items(handle, kind, now=None):
    q = urllib.parse.urlencode({"actor": handle, "limit": 10, "filter": "posts_no_replies"})
    data = http_json(f"{BSKY_FEED}?{q}")
    items, ids = [], set()
    for entry in data.get("feed") or []:
        it = bsky_post_to_item(entry, kind, now)
        if it and it["id"] not in ids:
            ids.add(it["id"])
            items.append(it)
    return items


def section_hash(section):
    """Huella de una sección. `lastModified` (metadato que Epic toca sin cambiar el contenido) no cuenta."""
    if isinstance(section, dict):
        section = {k: v for k, v in section.items() if k != "lastModified"}
    return hashlib.md5(json.dumps(section, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _notice_texts(node, out, depth=0):
    """Junta title/body de los avisos de emergencia sin depender de la forma exacta del JSON."""
    if depth > 6 or len(out) >= 3:
        return
    if isinstance(node, list):
        for x in node:
            _notice_texts(x, out, depth + 1)
    elif isinstance(node, dict):
        if node.get("hidden") is True:
            return
        parts = [str(node[k]).strip() for k in ("title", "body") if isinstance(node.get(k), str) and node[k].strip()]
        if parts:
            out.append(" — ".join(parts))
            return
        for v in node.values():
            _notice_texts(v, out, depth + 1)


def detect_epic(data, fp, now=None):
    """Compara las secciones elegidas contra la huella guardada. Devuelve (items, huella_nueva).
    Primera vez que se ve una sección: solo siembra el hash. Sección ausente: se conserva el hash.
    Si una sección cambia EPIC_FLAP_CHANGES veces seguidas se la considera volátil y se silencia
    EPIC_MUTE_SECS (se sigue actualizando el hash)."""
    now = time.time() if now is None else now
    fp = fp or {}
    old = dict(fp.get("sections") or {})
    flaps = {k: dict(v) for k, v in (fp.get("flaps") or {}).items()}
    sections, items = dict(old), []
    for key, label in EPIC_SECTIONS.items():
        if key not in data:
            continue
        h = section_hash(data[key])
        sections[key] = h
        if key not in old:
            continue
        f = flaps.setdefault(key, {"n": 0, "mute_until": 0})
        if old[key] == h:
            f["n"] = 0
            continue
        if now < f["mute_until"]:
            continue
        f["n"] += 1
        if f["n"] >= EPIC_FLAP_CHANGES:
            f["n"], f["mute_until"] = 0, now + EPIC_MUTE_SECS
            print(f"[aviso] la sección Epic '{key}' cambia en cada sondeo; se silencia {EPIC_MUTE_SECS // 60} min",
                  file=sys.stderr)
            continue
        extra = ""
        if key == "emergencynoticev2":
            notices = []
            _notice_texts(data[key], notices)
            if notices:
                extra = "\n" + "\n".join(f"• {n}" for n in notices)
        uid = f"epic:{key}:{h[:12]}"
        items.append(mk("epic", uid, f"Epic actualizó: {label}", T_EPIC.format(label=label, extra=extra), None, "Epic"))
    return items, {"lastModified": data.get("lastModified") or fp.get("lastModified"), "sections": sections,
                   "flaps": flaps}


def _is_fortnite(obj):
    if "fortnite" in (obj.get("name") or "").lower():
        return True
    return any(c_.get("id") == FORTNITE_COMPONENT_ID or "fortnite" in (c_.get("name") or "").lower()
               for c_ in obj.get("components") or [])


def _latest_body(obj):
    updates = obj.get("incident_updates") or []
    return ((updates[0].get("body") if updates else "") or "").strip()


def detect_statuspage(summary, upcoming, prev_components):
    """Incidentes, caídas de componentes y mantenimientos de Fortnite. Devuelve (items, componentes_nuevos).
    El id lleva estado + updated_at: un cambio de estado se re-anuncia, un sondeo idéntico no."""
    items = []
    for inc in (summary or {}).get("incidents") or []:
        if not _is_fortnite(inc):
            continue
        st, upd = inc.get("status") or "", inc.get("updated_at") or ""
        text = T_STATUS_INCIDENT.format(name=inc.get("name") or "incidente", status=STATUS_ES.get(st, st),
                                        body=_latest_body(inc), updated=upd, page=STATUS_PAGE)
        items.append(mk("status", f"status:incident:{inc.get('id')}:{st}:{upd}", f"Incidente: {inc.get('name')}",
                        text, None, "Estado"))
    comps = dict(prev_components or {})
    for comp in (summary or {}).get("components") or []:
        if comp.get("id") != FORTNITE_COMPONENT_ID and "fortnite" not in (comp.get("name") or "").lower():
            continue
        cid, st, upd = comp.get("id"), comp.get("status") or "", comp.get("updated_at") or ""
        if comps.get(cid) == "operational" and st != "operational":
            text = T_STATUS_COMPONENT.format(name=comp.get("name"), status=STATUS_ES.get(st, st), updated=upd,
                                             page=STATUS_PAGE)
            items.append(mk("status", f"status:component:{cid}:{st}:{upd}", f"{comp.get('name')}: {STATUS_ES.get(st, st)}",
                            text, None, "Estado"))
        comps[cid] = st
    for m in (upcoming or {}).get("scheduled_maintenances") or []:
        if not _is_fortnite(m):
            continue
        st, upd = m.get("status") or "", m.get("updated_at") or ""
        text = T_STATUS_MAINT.format(name=m.get("name") or "mantenimiento", status=STATUS_ES.get(st, st),
                                     start=m.get("scheduled_for") or "?", end=m.get("scheduled_until") or "?",
                                     body=_latest_body(m), page=STATUS_PAGE).replace("\n\n", "\n")
        items.append(mk("status", f"status:maint:{m.get('id')}:{st}:{upd}", f"Mantenimiento: {m.get('name')}",
                        text, None, "Estado"))
    for it in items:
        it["_seed"] = "statuspage"
    return items, comps


# Cada fuente: función(state) -> items. Pueden modificar state (huellas). Si fallan, lanzan excepción.
def src_news(state):
    return detect_news(api("/v2/news/br"))


def src_cosmetics(state):
    return detect_cosmetics(api("/v2/cosmetics/new"))


def src_shop(state):
    return detect_shop(api("/v2/shop"))


def src_aes(state):
    return detect_build(api("/v2/aes"))


def src_leaks(state):
    items, errors = [], 0
    for handle in LEAKER_HANDLES:
        try:
            items += fetch_bsky_items(handle, "leak")
        except Exception as e:
            errors += 1
            print(f"[aviso] Bluesky @{handle} falló: {e}", file=sys.stderr)
    if LEAKER_HANDLES and errors == len(LEAKER_HANDLES):
        raise RuntimeError("fallaron todas las cuentas de filtradores")
    return items


def src_status_bsky(state):
    return fetch_bsky_items(STATUS_HANDLE, "status")


def src_epic(state):
    data = http_json(EPIC_CONTENT_URL, timeout=40)
    items, state["epic_fp"] = detect_epic(data, state.get("epic_fp"))
    return items


def src_statuspage(state):
    summary = http_json(STATUS_SUMMARY_URL)
    upcoming = http_json(STATUS_UPCOMING_URL)
    items, state["status_components"] = detect_statuspage(summary, upcoming, state.get("status_components"))
    return items


def src_pending(state):
    return []   # no consulta nada: solo dispara el reintento de la cola pendiente


# nombre -> (intervalo s, función, tipos que se marcan como "sembrados" tras sondear)
SOURCES = {
    "news": (120, src_news, ()),
    "shop": (120, src_shop, ()),
    "aes": (120, src_aes, ()),
    "cosmetics": (120, src_cosmetics, ()),
    "leaks": (45, src_leaks, ("leak",)),
    "status_bsky": (45, src_status_bsky, ("status_bsky",)),
    "epic": (60, src_epic, ("epic",)),
    "statuspage": (60, src_statuspage, ("statuspage",)),
    "pending": (PENDING_RETRY_SECS, src_pending, ()),   # reintenta lo que Telegram rechazó
}


# ------------------------------------------------------------------ planificador
def backoff_delay(interval, fails):
    """Espera tras `fails` fallos seguidos: exponencial, tope BACKOFF_MAX, nunca menor que el intervalo."""
    return max(interval, min(interval * 2 ** fails, BACKOFF_MAX))


class Scheduler:
    """Intervalos por fuente. Reloj inyectable (now) para poder probarlo."""

    def __init__(self, intervals, now=0.0):
        self.intervals = dict(intervals)
        self.next_due = {k: now for k in self.intervals}
        self.fails = {k: 0 for k in self.intervals}

    def due(self, now):
        return sorted((k for k, t in self.next_due.items() if t <= now), key=lambda k: self.next_due[k])

    def wait(self, now):
        return max(1.0, min(self.next_due.values()) - now)

    def done(self, name, now, ok):
        self.fails[name] = 0 if ok else self.fails[name] + 1
        self.next_due[name] = now + backoff_delay(self.intervals[name], self.fails[name])


# ------------------------------------------------------------------ recolección
def fetch_all(demo=False):
    if demo:
        return json.loads((ROOT / "demo_data.json").read_text("utf-8"))
    out = {}
    for key, path in [
        ("news", "/v2/news/br"),
        ("cosmetics", "/v2/cosmetics/new"),
        ("shop", "/v2/shop"),
        ("aes", "/v2/aes"),
    ]:
        try:
            out[key] = api(path)
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            print(f"[aviso] {path} falló: {e}", file=sys.stderr)
            out[key] = {}
    return out


def collect(demo=False):
    raw = fetch_all(demo)
    found = (
        detect_build(raw.get("aes", {}))
        + detect_news(raw.get("news", {}))
        + detect_cosmetics(raw.get("cosmetics", {}))
        + detect_shop(raw.get("shop", {}))
    )
    return found


# ------------------------------------------------------------------- salida
def load_feed():
    f = SITE / "news.json"
    return json.loads(f.read_text("utf-8")) if f.exists() else []


def write_feed(feed):
    SITE.mkdir(exist_ok=True)
    feed = feed[:MAX_NEWS]
    payload = json.dumps(feed, ensure_ascii=False, indent=1)
    write_atomic(SITE / "news.json", payload)
    write_atomic(SITE / "news.js", f"window.NEWS = {payload};\nwindow.NEWS_UPDATED = {json.dumps(now_iso())};\n")


def telegram_config():
    return os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")


def telegram_call(token, method, payload):
    return http_json(
        f"https://api.telegram.org/bot{token}/{method}",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload).encode(),
    )


def send_telegram(item):
    """Publica un item en el canal. Devuelve True solo si Telegram lo aceptó."""
    token, chat = telegram_config()
    if not token or not chat:
        return False
    try:
        if item.get("image"):
            try:
                telegram_call(token, "sendPhoto", {"chat_id": chat, "photo": item["image"], "caption": item["text"][:1000]})
                return True
            except Exception as e:  # imagen rota o inaccesible: se publica solo el texto
                print(f"[aviso] sendPhoto falló ({e}); se envía sin imagen", file=sys.stderr)
        telegram_call(token, "sendMessage", {"chat_id": chat, "text": item["text"][:4096]})
        return True
    except Exception as e:  # nunca romper el ciclo por un fallo de publicación
        print(f"[aviso] Telegram falló: {e}", file=sys.stderr)
        return False
    finally:
        time.sleep(1.2)  # respeta límites de Telegram


def test_telegram():
    """Envía un mensaje de prueba al canal y muestra el resultado exacto."""
    token, chat = telegram_config()
    if not token or not chat:
        print("Faltan variables: TELEGRAM_BOT_TOKEN y/o TELEGRAM_CHAT_ID.")
        return
    try:
        telegram_call(token, "sendMessage", {"chat_id": chat, "text": "✅ FN Radar conectado. Mensaje de prueba."})
        print("OK: revisa tu canal, debe haber llegado el mensaje de prueba.")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        print(f"Telegram respondió {e.code}: {body}")
    except Exception as e:
        print(f"No se pudo conectar: {e}")


def seed_key(item):
    """Clave de sembrado de un item: su fuente si la declara (_seed), si no su tipo."""
    return item.get("_seed") or item["kind"]


def process(state, found, demo=False, polled_kinds=(), quiet=False, flush_pending=False, persist=None):
    """Pipeline de publicación: dedupe contra state['seen'], web, cola de Telegram con reintentos y tope.
    Modifica `state`; `persist` (guardar state.json) se llama ANTES de enviar y tras cada envío, así un
    corte a mitad no repite publicaciones (como mucho se repite la que estaba en vuelo).
    Devuelve la lista publicada, o None si no había nada nuevo.
    polled_kinds: claves de sembrado de fuentes sondeadas con éxito (se marcan aunque no traigan items).
    flush_pending: reintentar la cola pendiente (solo la fuente 'pending' y el modo de una pasada)."""
    seen = set(state["seen"])
    first_run = state.get("first_run", True)
    # Claves ya "sembradas" (su contenido existente no se publica en Telegram).
    # Estados antiguos sin este campo: asumimos que noticias/build/tienda ya estaban sembrados.
    # Fuentes nuevas (leak, epic, status_bsky, statuspage) en un estado antiguo: no están, se siembran en silencio.
    seeded = set(state.get("seeded") or ([] if first_run else ["news", "build", "shop"]))

    new = [n for n in found if n["id"] not in seen]
    # Publicaciones que Telegram rechazó antes: {"item": ..., "tries": N, "at": epoch del último intento}
    pending = state.get("pending") or []
    now_ts = time.time()
    due = [e for e in pending if flush_pending and now_ts - e.get("at", 0) >= PENDING_RETRY_SECS]
    keep = [e for e in pending if not any(e is d for d in due)]

    if not new and not due:
        if polled_kinds:
            state["seeded"] = sorted(seeded | set(polled_kinds))
        if not quiet:
            print("Sin novedades.")
        return None

    keys = {n["id"]: seed_key(n) for n in new}
    cosmetics = [n for n in new if n["kind"] == "cosmetic"]
    others = [n for n in new if n["kind"] != "cosmetic"]
    to_publish = others + batch_cosmetics(cosmetics)
    to_publish = [{k: v for k, v in n.items() if not k.startswith("_")} for n in to_publish]

    if to_publish:
        write_feed(to_publish + load_feed())

    queue = due + [
        {"item": n, "tries": 0}
        for n in to_publish
        if not (demo or first_run or keys.get(n["id"], n["kind"]) not in seeded)
    ]
    # Si hay más que el tope, filtraciones/Epic/estado van primero (sort estable: el resto conserva el orden).
    queue.sort(key=lambda e: e["item"]["kind"] not in PRIORITY_KINDS)

    # Visto + sembrado ANTES de enviar: si el proceso muere a mitad, no se republica (como mucho 1 en vuelo).
    for n in new:
        state["seen"].append(n["id"])
    for n in to_publish:
        state["seen"].append(n["id"])
    seeded |= {seed_key(n) for n in new} | set(polled_kinds)
    state["seeded"] = sorted(seeded)

    sent = 0
    if demo or not all(telegram_config()):
        state["pending"] = pending  # no hay a dónde publicar: conservar lo pendiente tal cual
    else:
        attempt, over = queue[:MAX_TELEGRAM_PER_RUN], queue[MAX_TELEGRAM_PER_RUN:]
        if over:
            print(f"[aviso] tope de {MAX_TELEGRAM_PER_RUN} mensajes por pasada; el resto solo va a la web")
        # los reintentos no se pierden por el tope; esperan a la próxima pasada
        remaining = keep + attempt + [e for e in over if e["tries"] > 0]
        for e in attempt:
            e["tries"] += 1
            e["at"] = now_ts
        state["pending"] = remaining
        if persist:
            persist()
        for e in attempt:
            ok = send_telegram(e["item"])
            if ok:
                sent += 1
            elif e["tries"] >= MAX_TELEGRAM_TRIES:
                print(f"[aviso] se descarta tras {MAX_TELEGRAM_TRIES} intentos: {e['item']['id']}", file=sys.stderr)
            if ok or e["tries"] >= MAX_TELEGRAM_TRIES:
                remaining.remove(e)
                if persist:
                    persist()

    print(f"{len(to_publish)} novedades en la web, {sent} publicadas en Telegram, {len(state['pending'])} pendientes")
    for n in to_publish:
        print(" -", n["text"].replace("\n", " | ")[:140])
    return to_publish


def run_once(demo=False):
    state = load_state()
    published = process(state, collect(demo), demo, flush_pending=True, persist=save_state)
    if published is None:
        return 0
    save_state(state)
    return len(published)


# ------------------------------------------------------------- sincronización con git
# Railway tiene disco efímero: state.json y docs/ se guardan en el repo de GitHub.
# Solo se activa con GITHUB_TOKEN + GITHUB_REPO (owner/nombre). El token viaja por variables de entorno
# de git (GIT_CONFIG_*), nunca en argumentos, en la URL del remote ni en .git/config.
# El checkout lo gestiona el bot: si el push es rechazado se hace fetch + reset --hard al remoto y se
# reaplican state.json y docs/news.*, así que NO lo uses sobre un checkout con cambios tuyos.
_next_attempt = 0.0    # time.monotonic() antes del cual no se vuelve a intentar sincronizar
_sync_fails = 0
_warned = set()
SYNC_FILES = ("state.json", "docs/news.json", "docs/news.js")


def git_settings():
    """(token, repo, rama). Si repo o rama no son válidos, repo=None -> sincronización desactivada."""
    token, repo, branch = os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPO"), os.environ.get("GIT_BRANCH") or "main"
    problem = None
    if repo and not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        problem = "GITHUB_REPO debe ser owner/nombre"
    elif not re.fullmatch(r"[\w./-]+", branch) or branch.startswith("-") or ".." in branch:
        problem = "GIT_BRANCH tiene caracteres no válidos"
    if problem:
        if problem not in _warned:
            _warned.add(problem)
            print(f"[aviso] {problem}; git_sync desactivado", file=sys.stderr)
        repo = None
    return token, repo, branch


def git_auth_b64(token):
    return base64.b64encode(f"x-access-token:{token}".encode()).decode()


def scrub(text, token):
    """Quita el token (y su forma base64) de cualquier texto antes de mostrarlo."""
    text = str(text)
    if token:
        text = text.replace(git_auth_b64(token), "***").replace(token, "***")
    return text


def git_run(args, cwd, token=None, timeout=120):
    """git con lista de args (sin shell). Si falla lanza RuntimeError con el error ya sin el token."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if token:
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                   GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {git_auth_b64(token)}")
    try:
        r = subprocess.run(["git", *args], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"git {args[0]} superó {timeout}s")
    if r.returncode != 0:
        raise RuntimeError(scrub(f"git {args[0]} falló ({r.returncode}): {(r.stderr or r.stdout).strip()}", token))
    return r.stdout


def git_prepare_workdir():
    """Arranque de --serve: si hay credenciales y el directorio no es un checkout, clona (shallow) el repo
    en GIT_WORKDIR (o en el tmp del sistema) y lo usa como ROOT, para que state.json y docs/ sobrevivan
    a los redeploys. Sin credenciales no hace nada."""
    global ROOT, SITE, STATE_FILE
    token, repo, branch = git_settings()
    if not (token and repo):
        return
    if not (ROOT / ".git").exists():
        work = Path(os.environ.get("GIT_WORKDIR") or Path(tempfile.gettempdir()) / "fn-radar-repo")
        if not (work / ".git").exists():
            work.parent.mkdir(parents=True, exist_ok=True)
            git_run(["clone", "--depth", "1", "--branch", branch, f"https://github.com/{repo}.git", str(work)],
                    work.parent, token, timeout=300)
        ROOT = work
        SITE, STATE_FILE = ROOT / "docs", ROOT / "state.json"
    git_run(["config", "user.name", "fn-radar-bot"], ROOT)
    git_run(["config", "user.email", "bot@users.noreply.github.com"], ROOT)
    try:   # solo trae lo remoto; si hay commits locales sin subir, git_sync los sube (o resuelve la divergencia)
        git_run(["fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], ROOT, token)
    except RuntimeError as e:
        print(f"[aviso] no se pudo actualizar el checkout: {e}", file=sys.stderr)
    print(f"[git] datos en {ROOT} (rama {branch})")


def git_sync(force=False, timeout=120):
    """Commit + push de state.json y docs/ si cambiaron o si hay commits locales sin subir. Máximo un push
    cada GIT_PUSH_MIN_INTERVAL s; tras un fallo la espera se duplica hasta BACKOFF_MAX. Si el push es
    rechazado: fetch + reset --hard al remoto, se reaplican los datos y se reintenta una vez.
    Devuelve: disabled | clean | debounced | pushed | error."""
    global _next_attempt, _sync_fails
    token, repo, branch = git_settings()
    if not (token and repo) or not (ROOT / ".git").exists():
        return "disabled"
    if not force and time.monotonic() < _next_attempt:
        return "debounced"

    def g(args, tok=None):
        return git_run(args, ROOT, tok, timeout)

    try:
        paths = [p for p in ("state.json", "docs") if (ROOT / p).exists()]
        dirty = bool(paths) and bool(g(["status", "--porcelain", "--", *paths]).strip())
        ahead = int(g(["rev-list", "--count", f"origin/{branch}..HEAD"]).strip() or 0)
        if not dirty and not ahead:
            return "clean"
        if dirty:
            g(["add", "--", *paths])
            g(["commit", "-m", "Nuevas noticias"])
        try:
            g(["push", "origin", f"HEAD:{branch}"], token)
        except RuntimeError:
            # Rechazado (alguien más escribió): nada de rebase con conflictos en state.json. Se toma lo
            # remoto, se reaplican nuestros datos (más recientes) y se vuelve a intentar una vez.
            data = {rel: (ROOT / rel).read_bytes() for rel in SYNC_FILES if (ROOT / rel).exists()}
            g(["fetch", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}"], token)
            g(["reset", "--hard", f"origin/{branch}"])
            for rel, content in data.items():
                (ROOT / rel).parent.mkdir(parents=True, exist_ok=True)
                (ROOT / rel).write_bytes(content)
            if data:
                g(["add", "--", *data])
                if g(["status", "--porcelain", "--", *data]).strip():
                    g(["commit", "-m", "Nuevas noticias"])
            g(["push", "origin", f"HEAD:{branch}"], token)
        _sync_fails = 0
        _next_attempt = time.monotonic() + GIT_PUSH_MIN_INTERVAL
        return "pushed"
    except Exception as e:
        _sync_fails += 1
        _next_attempt = time.monotonic() + min(GIT_PUSH_MIN_INTERVAL * 2 ** _sync_fails, BACKOFF_MAX)
        print(f"[aviso] git_sync falló: {scrub(e, token)}", file=sys.stderr)
        return "error"


# ------------------------------------------------------------------- modo --serve
def run_source(name):
    """Sondea UNA fuente y publica lo nuevo. Devuelve True si cambió state.json. Lanza si la fuente falla."""
    _, fn, polled = SOURCES[name]
    state = load_state()
    before = json.dumps(state, sort_keys=True)
    published = process(state, fn(state), polled_kinds=polled, quiet=True, flush_pending=(name == "pending"),
                        persist=lambda: save_state(state))
    if published is None and json.dumps(state, sort_keys=True) == before:
        return False
    save_state(state)
    return True


def serve():
    git_prepare_workdir()
    sched = Scheduler({k: v[0] for k, v in SOURCES.items()}, now=time.monotonic())
    print(f"[serve] {len(SOURCES)} fuentes: " + ", ".join(f"{k}/{v[0]}s" for k, v in SOURCES.items()), flush=True)
    dirty = False
    try:
        while True:
            now = time.monotonic()
            due = sched.due(now)
            if not due:
                if dirty:
                    dirty = git_sync() in ("debounced", "error")
                time.sleep(sched.wait(now))
                continue
            name, ok = due[0], True
            try:
                dirty = run_source(name) or dirty
            except Exception as e:   # una fuente caída nunca detiene el bucle ni a las demás
                ok = False
                print(f"[aviso] fuente {name} falló: {scrub(e, git_settings()[0])}", file=sys.stderr, flush=True)
            sched.done(name, time.monotonic(), ok)
            if dirty:
                dirty = git_sync() in ("debounced", "error")
    finally:
        git_sync(force=True, timeout=10)   # al apagar (SIGTERM en Railway): corto, que no cuelgue el cierre


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="usar demo_data.json sin internet")
    ap.add_argument("--loop", type=int, default=0, metavar="SEGUNDOS", help="repetir cada N segundos")
    ap.add_argument("--serve", action="store_true", help="proceso permanente con intervalo por fuente (Railway)")
    ap.add_argument("--test-telegram", action="store_true", help="enviar mensaje de prueba al canal")
    args = ap.parse_args()

    if args.test_telegram:
        test_telegram()
        return

    if args.serve:
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        serve()
        return

    if args.loop:
        while True:
            try:
                run_once(args.demo)
            except Exception as e:
                print(f"[error] {e}", file=sys.stderr)
            time.sleep(max(60, args.loop))
    else:
        run_once(args.demo)


if __name__ == "__main__":
    main()
