# FN Radar – Visión, estado y tareas

Documento para retomar el proyecto en Claude Code. Léelo completo antes de tocar código.

## 1. Visión

Un medio de noticias de Fortnite **en español, rápido y con humor (estilo meme)**, que publique en tiempo casi real
lo que pasa en el juego: noticias in-game, cosméticos nuevos o filtrados, tienda del día y parches.
Inspiración: cuentas como Hypex en X. **No se copian sus publicaciones**: se usan las mismas fuentes primarias
(datos del juego, cuentas oficiales de Epic) y se redacta con voz propia.

Principios:
- **Empezar con costo cero** y sumar canales de pago solo cuando haya audiencia.
- **Un solo recolector, varios canales de salida** (web, Telegram, luego X u otros).
- Todo lo automático debe ser **idempotente**: nunca publicar dos veces lo mismo ni inundar el canal.
- Voz propia: tono *rápido + meme*, emojis, frases cortas.

Canales:
- Telegram: `@fnradar_news` (https://t.me/fnradar_news), nombre del canal "FN Radar".
- Web: `docs/index.html` (estática, lee `news.js` / `news.json`).
- X, WhatsApp y otros: pendientes (ver sección 4).

## 2. Estado actual (15-oct-2026)

Carpeta del proyecto en la PC del usuario (Windows): `C:\Users\svsil\Desktop\Silvas-Indrustries\fortnite-news`
Repo (público): https://github.com/infinito-z/fortnite-news · Web: https://infinito-z.github.io/fortnite-news/

Estructura:
```
collector.py                 recolector + redactor + publicador (solo librería estándar de Python)
demo_data.json               datos de ejemplo para --demo
state.json                   qué ya se vio + cola "pending" de Telegram (se crea solo; NO borrar en producción)
docs/index.html              web del feed (filtros por tipo, refresco cada 60 s)
docs/news.js, news.json      feed generado (máx. 200 entradas)
.github/workflows/collect.yml  ejecución automática cada ~10 min en GitHub Actions
.gitignore                   excluye __pycache__, .env, .venv, etc.
README.md                    guía rápida
```

La web vive en `docs/` (antes `site/`) porque GitHub Pages con "Deploy from branch" solo publica desde `/` o `/docs`.

Fuentes (fortnite-api.com, gratis, sin API key, no oficial):
- `/v2/news/br` noticias in-game (`data.motds`)
- `/v2/cosmetics/new` cosméticos nuevos por categoría
- `/v2/shop` tienda del día (se actualiza 00:00 UTC = 19:00 Lima)
- `/v2/aes` se usa para detectar el build (parche) nuevo

Probado y funcionando:
- Recolección real contra la API: noticias, build (42.30), tienda (333 ítems) y 142 cosméticos nuevos, ya en español (`language=es`).
- Mensaje de prueba al canal de Telegram (`python collector.py --test-telegram`).
- Web local: `cd docs && python -m http.server 8000`.
- Lógica de "sembrado": la primera vez que se ve una fuente, solo llena la web y **no** publica en Telegram.
- Resumen único cuando hay más de 5 cosméticos nuevos en una pasada; tope de 10 mensajes de Telegram por pasada.
  Lo nuevo que exceda el tope **solo va a la web** (a propósito, para no inundar el canal).
- Reintentos de Telegram (probado con pruebas simuladas, no contra Telegram real):
  - Si `sendPhoto` falla (imagen rota o inaccesible), se reenvía como `sendMessage` sin imagen.
  - Si Telegram rechaza un post, va a `state.json` → `pending` y se reintenta en las siguientes pasadas;
    se descarta tras 3 intentos (`MAX_TELEGRAM_TRIES`). Los reintentos no se pierden por el tope de 10.
  - Sin token configurado (o con `--demo`), la cola `pending` se conserva intacta.
- Texto de `sendMessage` recortado a 4096 caracteres; pie de foto a 1000.
- Workflow: `concurrency` (sin ejecuciones simultáneas), `timeout-minutes: 10` y `git pull --rebase` antes del push.
- **En producción (15-oct-2026):** el workflow corre en GitHub Actions, el bot `fn-radar-bot` hace commit de
  `state.json` y `docs/`, Pages se reconstruye solo con esos commits, y la primera publicación automática real
  (tienda de las 19:00 Lima, con foto vía `sendPhoto`) llegó a @fnradar_news.
- Post de tienda (nuevo): solo lo que **entró hoy** (`inDate` = fecha de la tienda), top 6 por precio con
  nombre y precio en paVos, sin repetir un ítem que viene en lote y suelto; la imagen es de la primera novedad
  listada. Si no hay nada nuevo, un post corto "sin nada nuevo". Probado con la tienda real del 15-oct (55 novedades
  de 379); aún no publicado en Telegram con este formato.

**Aún no probado** (importante):
- Publicación automática de una **noticia** in-game, un **parche** o **cosméticos** reales (solo se ha visto la tienda).
- El nuevo formato de tienda en Telegram (primera tienda: 16-oct 00:00 UTC = 15-oct 19:00 Lima).
- Reintentos y respaldo sin imagen contra Telegram real (solo probados con simulación).
- Que el cron programado dispare solo (hasta ahora solo se corrió a mano).
- La forma exacta de la respuesta de `/v2/cosmetics/new`: el extractor `_cosmetic_items` es tolerante a varias formas y funcionó con datos reales, pero conviene validarlo cuando cambie de parche.

Paso en curso: vigilar las primeras ejecuciones automáticas del cron y el primer post de tienda con el formato nuevo.

## 3. Tareas inmediatas (en orden)

Hechas (15-oct-2026): repo subido y público, Pages en `main` / `/docs`, secrets de Telegram, permisos
Read and write, primera ejecución manual OK, `concurrency` en el workflow, primer post real en Telegram,
y nuevo formato del post de tienda.

1. Confirmar en Actions que el cron se dispara solo (ejecuciones con evento `schedule`, cada ~10 min).
2. A las 19:00 Lima, revisar el primer post de tienda con el formato nuevo (lista, precios, imagen).
3. Ajustar las plantillas `T_SHOP` / `T_SHOP_SAME` a tu voz si hace falta.
4. Esperar una noticia o parche real y confirmar que llega bien a Telegram.

## 4. Tareas futuras

Calidad de contenido
- Redactar con la API de Claude en vez de plantillas (centavos por noticia), conservando las plantillas como respaldo si falla. Mantener el tono rápido/meme con ejemplos propios del usuario.
- Tienda: marcar descuentos (`regularPrice` > `finalPrice`) y destacar atuendos sobre lotes; quizá un álbum
  (`sendMediaGroup`) con varias imágenes.
- Detectar cambios de mapa (`/v1/map`), playlists/modos (`/v1/playlists`) y llaves AES.
- Detectar noticias cuyo contenido cambió aunque conserven el mismo id (hoy solo se detecta por id).
- Mensajes largos: hoy se recorta a ciegas (4096 texto / 1000 pie de foto). Mejor: cortar en frase completa o
  mandar foto con pie corto + texto aparte.

Fuentes adicionales (verificar cada una antes de usar)
- Estado de servidores de Epic (mantenimientos y caídas).
- Blog/noticias oficiales de Fortnite y cuentas oficiales (@FortniteGame, @EpicGames, @FortniteStatus).
- Reddit r/FortNiteBR (feed RSS) para filtraciones que ya circulan.

Canales
- **Discord**: webhook gratis, muy fácil.
- **X (Twitter)**: la API es de pago por uso desde feb-2026 (según fuentes de terceros, confirmar en la página oficial de X): ~$0.015 por tuit de texto y ~$0.20 si el tuit lleva enlace; sin plan gratis para cuentas nuevas. Estimado: 10 tuits de texto/día ≈ $4.50/mes. Existe además el servidor MCP oficial de X (`api.x.com/mcp`) que factura igual. Etiquetar la cuenta como automatizada. Alternativa: servicios de publicación con tarifa fija.
- **WhatsApp (Canales)**: no hay API oficial para publicar; las opciones de terceros son frágiles y arriesgan el número. Dejar para el final.
- **Web**: dominio propio, RSS/Atom de salida, metadatos Open Graph para compartir, modo claro/oscuro, página por noticia.
- **MCP propio** (opcional): exponer el feed como herramientas (`ultimas_noticias`, `tienda_de_hoy`) para consultarlo desde Claude.

Robustez y operación
- Pruebas unitarias de los detectores con respuestas guardadas de la API (fixtures), y de la cola `pending`
  (hoy solo se probó con un script temporal, no hay pruebas en el repo).
- Si Telegram responde 429 (límite), respetar `retry_after` en vez de contarlo como intento fallido.
- El historial del repo crece con cada commit del bot; si molesta, mover `state.json`/feed a una rama aparte o a un gist.
- Aviso al administrador (mensaje privado de Telegram) si una fuente falla N veces seguidas o cambia de formato.
- Vigilar el ciclo de GitHub Actions: el cron puede retrasarse y GitHub desactiva los programados tras ~60 días sin actividad del repo.
- Si se necesita menos de 10 min de latencia: mover a un VPS barato (~$4–6/mes) con `python collector.py --loop 120`.
- Si una fuente se retira (ya pasó: `/v2/cosmetics/br/new` devolvió 410), actualizar la ruta y revisar el extractor.

## 5. Decisiones y cuidados a recordar

- fortnite-api.com **no es oficial** y puede cambiar o retirar endpoints sin aviso.
- El sitio debe llevar el aviso de "no oficial; Fortnite es marca de Epic Games" (ya está en el pie).
- No copiar textos ni imágenes de otras cuentas; usar fuentes primarias y redacción propia.
- La primera ejecución contra cualquier fuente nueva debe ser **silenciosa** (sembrado) para no inundar el canal.
- Código de creador `INFINITO_Z` (Support-A-Creator): cada vez que se promociona hay que declarar la relación con
  Epic de forma clara y visible, junto al código (no escondida en el pie). Se usa `#EpicPartner` (etiqueta aceptada
  por Epic) y en la web además la frase larga sobre pagos. Vive en dos sitios: `CREATOR_CODE`/`CREATOR_LINE` en
  `collector.py` (solo posts de tienda) y el bloque `.sac` de `docs/index.html`. Epic solo permite cambiar el código
  una vez cada 12 meses.
  No usar logos de Epic/Fortnite. Revisar las reglas cada ~3 meses (y ante cualquier correo de Epic) en
  https://www.epicgames.com/affiliate/overview y https://legal.epicgames.com/fortnite/eula/sac (pide iniciar sesión).
- Un id se marca como visto aunque Telegram falle (así la web no duplica); lo no enviado vive en `pending`, no en `seen`.
- Secretos solo como variables de entorno o *secrets* de GitHub; el token que se creó en BotFather nunca va en el repo ni en el chat.
- El usuario escribe en español (Lima, UTC-5); la tienda cambia a las 19:00 hora de Lima.

## 6. Cómo ejecutar (resumen)

```
python collector.py --demo            # prueba sin internet
python collector.py                   # una pasada real
python collector.py --loop 300        # cada 5 min
python collector.py --test-telegram   # mensaje de prueba al canal
```
Variables (CMD de Windows, sin comillas): `set TELEGRAM_BOT_TOKEN=...` y `set TELEGRAM_CHAT_ID=@fnradar_news`.
Idioma de la API: variable `FN_LANG` (por defecto `es`).
