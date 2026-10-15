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

## Modo casi en tiempo real (`--serve`)
    python collector.py --serve

Proceso permanente: cada fuente tiene su propio intervalo y una fuente caída nunca frena a las demás
(backoff exponencial hasta 5 min, se reinicia al recuperarse).

| Fuente | Qué detecta | Cada |
|---|---|---|
| fortnite-api.com (noticias, tienda, build, cosméticos) | lo de siempre | 120 s |
| Bluesky: `lpleaksnews`, `newsandleaksbr`, `blackgokunews` | filtraciones (`leak`), siempre marcadas "sin confirmar" con enlace al post | 45 s |
| Bluesky: `fortnite-status` | avisos oficiales de estado (`status`) | 45 s |
| API de contenido de Epic | cambios en avisos de emergencia, fondos, lobby, mensaje de login y playlists (`epic`) | 60 s |
| Epic Status (status.epicgames.com) | incidentes, caídas de componentes y mantenimientos que mencionan Fortnite (`status`) | 60 s |

- Los filtradores se pueden cambiar con `FN_LEAKERS=cuenta1.bsky.social,cuenta2.bsky.social`.
- Los posts de filtradores se publican como están (normalmente en inglés/portugués, sin traducir) y se ignoran
  reposts, posts vacíos y posts de más de 6 h.
- La primera vez que se ve cada tipo (`leak`, `epic`, `status`) solo se siembra: no se manda a Telegram.
- Si hay más mensajes que el tope por pasada, filtraciones/Epic/estado salen primero.
- Honestidad: son fuentes gratis; van minutos por detrás de los filtradores en X. Para ir más rápido
  habría que pagar la API de X o mirar sus cuentas directamente.
- Un incidente de Epic Status que se resuelve desaparece de `summary.json`, así que el "resuelto" no se anuncia.

`python collector.py` (una pasada) y `--demo` siguen igual que antes; `--loop` también.

## Desplegar en Railway (proceso permanente)
Railway tiene disco efímero, por eso `state.json` y `docs/` se guardan en el repo de GitHub (`git_sync`).

1. Sube el repo a GitHub y crea un proyecto en Railway desde ese repo (`railway.json` ya define
   el comando de arranque `python collector.py --serve`; `requirements.txt` vacío y `.python-version`
   solo sirven para que detecte Python).
2. Variables de entorno en Railway:

| Variable | Obligatoria | Para qué |
|---|---|---|
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | no (sin ellas solo actualiza la web) | publicar en Telegram |
| `GITHUB_TOKEN` | sí, para persistir | token con permiso `contents: write` sobre el repo |
| `GITHUB_REPO` | sí, para persistir | `owner/nombre` |
| `GIT_BRANCH` | no | rama (por defecto `main`) |
| `FN_LEAKERS` | no | cuentas de Bluesky separadas por coma |
| `GIT_WORKDIR` | no | dónde clonar si el deploy no trae `.git` (por defecto el tmp del sistema) |

3. Al arrancar, si hay `GITHUB_TOKEN` y `GITHUB_REPO` y no hay checkout, se clona el repo (shallow) y se usa
   como directorio de datos. Cada cambio en `state.json`/`docs/` se commitea ("Nuevas noticias") y se sube
   con un máximo de un push cada 30 s (si falla, la espera se duplica hasta 5 min). Si el push es rechazado
   hace `git fetch` + `reset --hard` al remoto, reaplica `state.json` y `docs/news.*` y reintenta una vez:
   el checkout lo gestiona el bot, no lo uses con cambios tuyos sin guardar.
   El token no se imprime ni se guarda en `.git/config` (se pasa por variables de entorno de git).

**ATENCIÓN, doble publicación:** cuando Railway esté funcionando, DESACTIVA el cron de GitHub Actions
(`.github/workflows/collect.yml`: borra el bloque `schedule` o desactiva el workflow). Si siguen los dos
activos, ambos publican en Telegram y se pisan `state.json` en el repo.

## Pruebas
    python -m unittest discover -s tests

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
- Publicar en X (ver costos en la conversación).
