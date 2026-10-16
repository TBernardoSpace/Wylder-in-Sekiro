# Cómo encontrar los endpoints internos de fomo.family (Vía A)

`fomo.family` no documenta una API pública, pero su frontend obtiene la lista de tokens de algún
endpoint (REST, GraphQL o WebSocket). Esta guía explica cómo encontrarlo y pasarlo a `.env`.

> **Atajo:** `python tools/capture_fomo_network.py --seconds 90` abre Chromium, registra todo el
> tráfico JSON/WS mientras navegas y al final imprime las líneas `FOMO_A_*` listas para pegar.
> Lo que sigue es el proceso manual, útil para entender y depurar lo que detecta el script.

---

## 1. Preparar DevTools

1. Abre **Chrome** (o Brave/Edge) en una ventana normal, no de incógnito, para conservar el login
   si la app lo pide.
2. Abre `https://fomo.family` y pulsa **F12** (o `Cmd+Opt+I` en macOS) → pestaña **Network**.
3. Activa:
   - ☑ **Preserve log**: no se borra el registro al navegar.
   - ☑ **Disable cache**: fuerza peticiones reales.
4. Pulsa **Ctrl+R** para recargar con DevTools abierto. Si no, las primeras peticiones no quedan registradas.

## 2. Encontrar la petición que trae los tokens

1. Ve a la sección que te interesa: tokens nuevos, *trending* o la categoría **Memes**.
2. En la barra de filtros de Network pulsa **Fetch/XHR**.
3. **Truco clave:** pulsa `Ctrl+F` *dentro* del panel Network (abre "Search", que busca dentro de
   los cuerpos de respuesta) y escribe un ticker o un fragmento de contract address que estés
   viendo en pantalla (ej. `PENNY`). La búsqueda te lleva a la respuesta que contiene ese dato.
4. Haz clic en esa petición y revisa:

| Pestaña | Qué mirar | Variable `.env` |
|---|---|---|
| **Headers → General** | `Request URL`, `Request Method` | `FOMO_A_URL`, `FOMO_A_METHOD` |
| **Headers → Request Headers** | `authorization`, `x-api-key`, `x-app-version`, `x-client-*`, `cookie` | `FOMO_A_HEADERS` (JSON) |
| **Payload** | cuerpo de POST/GraphQL (`operationName`, `variables`, `query`) y query string | `FOMO_A_BODY` / la propia URL |
| **Preview / Response** | dónde está el array de tokens (ej. `data.tokens`) y cómo se llaman los campos | `FOMO_A_ITEMS_PATH`, `FOMO_A_FIELD_MAP` |

### Mapeo de campos

El normalizador ya reconoce los nombres más comunes (`symbol`/`ticker`, `address`/`mint`/`tokenAddress`,
`marketCap`/`mcap`/`fdv`, `liquidity.usd`/`liquidityUsd`, `volume.m5`/`volume5m`, `holders`/`holderCount`,
`createdAt`/`created_timestamp`...). Si algún campo sale `n/d` en el dashboard, fija la ruta exacta:

```env
# Respuesta: {"data":{"items":[{"token":{"sym":"PENNY","ca":"..."},"stats":{"liq":31000,"h":420}}]}}
FOMO_A_ITEMS_PATH=data.items
FOMO_A_FIELD_MAP={"symbol":"token.sym","token_address":"token.ca","liquidity_usd":"stats.liq","holder_count":"stats.h"}
```

Campos mapeables: `name, symbol, token_address, chain, market_cap, liquidity_usd, volume_5m,
holder_count, buys_5m, sells_5m, price_usd, created_at, fomo_url`.

## 3. Validar fuera del navegador

1. Clic derecho sobre la petición → **Copy → Copy as cURL (bash)**.
2. Pégalo en una terminal y añade `| jq '.data.tokens[0]'` (ajusta la ruta) para ver un item.
3. **Reduce cabeceras**: borra cabeceras una por una y vuelve a ejecutar. Así encuentras el mínimo
   necesario. Suelen bastar `user-agent`, `origin`/`referer` y, si existe, `authorization`.
4. Si hay un `authorization: Bearer eyJ...` (JWT), decodifica su parte central en local
   (`echo '<payload>' | base64 -d`) y mira el campo `exp`. Si caduca en minutos, usa el modo Playwright
   (paso 5), que lo renueva solo.

```bash
curl 'https://<host-que-viste>/<ruta>?limit=50' \
  -H 'accept: application/json' -H 'origin: https://fomo.family' -H 'referer: https://fomo.family/' \
  -H 'user-agent: Mozilla/5.0 ...' | jq '.data.tokens[0]'
```

## 4. Si los tokens llegan por WebSocket

1. En Network filtra por **WS** (o *Socket*) y recarga.
2. Clic en la conexión → pestaña **Messages**:
   - Flechas **verdes ↑** = mensajes enviados por el cliente (suscripciones, `ping`, `auth`).
     Cópialos tal cual en `FOMO_A_WS_SUBSCRIBE` (lista JSON; los strings se envían sin modificar).
   - Flechas **rojas ↓** = datos recibidos. Comprueba que contienen símbolo/address de los tokens.
3. `Request URL` de la conexión (`wss://...`) → `FOMO_A_WS_URL`. Los prefijos de socket.io/Phoenix
   (`42[...]`) se eliminan automáticamente.

```env
FOMO_A_WS_URL=wss://<host>/socket?EIO=4&transport=websocket
FOMO_A_WS_SUBSCRIBE=["40", "42[\"subscribe\",{\"channel\":\"new_tokens\"}]"]
```

## 5. Cloudflare, cookies y tokens efímeros

Síntomas: `curl` devuelve **403**, HTML con "Just a moment..." o `cf-mitigated: challenge`.

- **Opción recomendada:** `FOMO_A_PLAYWRIGHT=true`. El radar abre la web en Chromium headless,
  supera el challenge como un navegador normal e intercepta las respuestas JSON/WS cuya URL coincida
  con `FOMO_A_PLAYWRIGHT_URL_REGEX`. Ajusta la regex al host/ruta que viste en el paso 2.
- **Opción manual:** copia la cookie `cf_clearance` y el **mismo** `User-Agent` del navegador en
  `FOMO_A_HEADERS`. Caduca y va ligada a tu IP: sirve para pruebas, no para producción.

```env
FOMO_A_HEADERS={"cookie":"cf_clearance=...; session=...","user-agent":"<el mismo UA del navegador>"}
```

## 6. Si la web no expone el feed (solo la app móvil)

fomo.family es sobre todo una app móvil. Si la versión web no muestra el listado:

1. Ejecuta la app en un emulador Android o en tu móvil con **HTTP Toolkit** o **mitmproxy**, e instala
   su certificado.
2. Repite los pasos 2-4 en el inspector del proxy.
3. Si la app usa *certificate pinning* (no ves tráfico), la alternativa práctica es la **Vía C**
   (on-chain), sola o con la **Vía B**.

## 7. URL de compra (`FOMO_TOKEN_URL_TEMPLATE`)

Abre cualquier token en fomo.family, pulsa **Share/Copy link** y compara la URL con el contract
address. Ajusta la plantilla (`{chain}` y `{address}` se reemplazan):

```env
FOMO_TOKEN_URL_TEMPLATE=https://fomo.family/tokens/{chain}/{address}
```

Ese link es el botón **"🟢 Comprar en Fomo"** de Telegram, Discord y el dashboard. Comprueba el
formato antes de operar.

## 8. Buenas prácticas

- No bajes `FOMO_POLL_SECONDS` de 3-5 s. Si existe un WebSocket, úsalo en lugar de polling.
- Revisa los términos de servicio de fomo.family y de los wrappers que uses.
- Los endpoints internos cambian sin aviso. Cuando el estado `tokens:fomo:A` del dashboard pase a
  ✖, repite esta guía o ejecuta de nuevo el script de captura.
