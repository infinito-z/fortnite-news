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
  python collector.py --test-telegram  # mensaje de prueba al canal
"""
import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = "https://fortnite-api.com"
LANG = os.environ.get("FN_LANG", "es")
ROOT = Path(__file__).parent
SITE = ROOT / "docs"   # GitHub Pages solo publica desde / o /docs
STATE_FILE = ROOT / "state.json"
MAX_NEWS = 200
UA = "fortnite-news-bot/0.1"


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


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text("utf-8"))
    return {"seen": [], "first_run": True}


def save_state(state):
    state["seen"] = state["seen"][-3000:]
    state["first_run"] = False
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=1), "utf-8")


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
T_BUILD = [
    "🚨 NUEVA VERSIÓN DETECTADA: {build}. Parche en camino, a preparar el pop-off 🔧",
    "⚙️ Cambió el build del juego: {build}. Algo se viene, mantente atento 👀",
]


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
        text = pick(uid, T_SHOP_SAME).format(total=len(entries))
        return [mk("shop", uid, "Tienda del día", text, None, "Tienda")]

    top = offers[:SHOP_TOP]
    lines = "\n".join(f"• {o['name']} — {pavos(o['price'])} paVos" for o in top)
    more = len(offers) - len(top)
    if more > 0:
        lines += f"\n…y {more} más"
    text = pick(uid, T_SHOP).format(n=len(offers), total=len(entries), lines=lines)
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
    (SITE / "news.json").write_text(payload, "utf-8")
    (SITE / "news.js").write_text(f"window.NEWS = {payload};\nwindow.NEWS_UPDATED = {json.dumps(now_iso())};\n", "utf-8")


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


def run_once(demo=False):
    state = load_state()
    seen = set(state["seen"])
    first_run = state.get("first_run", True)
    # Tipos de fuente ya "sembrados" (su contenido existente no se publica en Telegram).
    # Estados antiguos sin este campo: asumimos que noticias/build/tienda ya estaban sembrados.
    seeded = set(state.get("seeded") or ([] if first_run else ["news", "build", "shop"]))

    found = collect(demo)
    new = [n for n in found if n["id"] not in seen]
    # Publicaciones que Telegram rechazó en pasadas anteriores: {"item": ..., "tries": N}
    pending = state.get("pending") or []

    if not new and not pending:
        print("Sin novedades.")
        return 0

    cosmetics = [n for n in new if n["kind"] == "cosmetic"]
    others = [n for n in new if n["kind"] != "cosmetic"]
    to_publish = others + batch_cosmetics(cosmetics)
    to_publish = [{k: v for k, v in n.items() if not k.startswith("_")} for n in to_publish]

    if to_publish:
        write_feed(to_publish + load_feed())

    queue = pending + [
        {"item": n, "tries": 0}
        for n in to_publish
        if not (demo or first_run or n["kind"] not in seeded)
    ]
    sent, retry = 0, []
    if demo or not all(telegram_config()):
        retry = pending  # no hay a dónde publicar: conservar lo pendiente tal cual
    else:
        for i, entry in enumerate(queue):
            if i >= MAX_TELEGRAM_PER_RUN:
                print(f"[aviso] tope de {MAX_TELEGRAM_PER_RUN} mensajes por pasada; el resto solo va a la web")
                # los reintentos no se pierden por el tope; esperan a la próxima pasada
                retry += [e for e in queue[i:] if e["tries"] > 0]
                break
            if send_telegram(entry["item"]):
                sent += 1
            elif entry["tries"] + 1 < MAX_TELEGRAM_TRIES:
                retry.append({"item": entry["item"], "tries": entry["tries"] + 1})
            else:
                print(f"[aviso] se descarta tras {MAX_TELEGRAM_TRIES} intentos: {entry['item']['id']}", file=sys.stderr)
    state["pending"] = retry

    for n in new:
        state["seen"].append(n["id"])
    for n in to_publish:
        state["seen"].append(n["id"])
    seeded |= {n["kind"] for n in new}
    state["seeded"] = sorted(seeded)
    save_state(state)

    print(f"{len(to_publish)} novedades en la web, {sent} publicadas en Telegram, {len(retry)} pendientes")
    for n in to_publish:
        print(" -", n["text"].replace("\n", " | ")[:140])
    return len(to_publish)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="usar demo_data.json sin internet")
    ap.add_argument("--loop", type=int, default=0, metavar="SEGUNDOS", help="repetir cada N segundos")
    ap.add_argument("--test-telegram", action="store_true", help="enviar mensaje de prueba al canal")
    args = ap.parse_args()

    if args.test_telegram:
        test_telegram()
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
