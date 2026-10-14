# FN Radar – noticias de Fortnite (costo cero)

## Qué hace
`collector.py` consulta fortnite-api.com (gratis, sin API key), detecta lo nuevo
(noticias in-game, cosméticos nuevos/filtrados, tienda, build del juego), lo redacta
en estilo rápido + meme y lo guarda en `docs/news.json` y `docs/news.js`.
`docs/index.html` es la web que muestra el feed (se actualiza sola cada 60 s).

## Probar ya (sin internet, datos de ejemplo)
    python collector.py --demo
    cd docs && python -m http.server 8000     # abre http://localhost:8000

Para volver a empezar de cero: borra `state.json`, `docs/news.json` y deja `docs/news.js` como `window.NEWS = [];`.

## Modo real
    python collector.py              # una pasada
    python collector.py --loop 300   # cada 5 min

La primera pasada real "siembra" la web con lo que ya existe y NO publica en Telegram
(para no spamear). Desde la segunda, solo se publica lo nuevo.

## Telegram (gratis)
1. En Telegram habla con @BotFather → /newbot → copia el token.
2. Crea un canal, añade el bot como administrador.
3. Variables de entorno:
       TELEGRAM_BOT_TOKEN=...   TELEGRAM_CHAT_ID=@tu_canal

## Hosting gratis (GitHub)
1. Sube esta carpeta a un repo de GitHub.
2. Settings → Pages → Deploy from branch → rama `main`, carpeta `/docs`.
3. Settings → Secrets → añade `TELEGRAM_BOT_TOKEN` y `TELEGRAM_CHAT_ID` (opcional).
4. El workflow `.github/workflows/collect.yml` corre cada ~10 min, actualiza y publica.

## Tu estilo
Edita las listas `T_NEWS`, `T_COSMETIC`, `T_SHOP`, `T_BUILD` en `collector.py`.
Zona horaria de la tienda: se actualiza a las 00:00 UTC (19:00 en Lima).

## Siguientes pasos
- Redactar con Claude (API) en vez de plantillas (cuesta centavos por noticia).
- Vigilar @FortniteGame / @EpicGames / Epic Status vía RSS.
- Publicar en X (ver costos en la conversación).
