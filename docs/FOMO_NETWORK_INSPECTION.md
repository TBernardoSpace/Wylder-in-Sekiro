# La "API" de fomo.family: qué existe y cómo usarla

## 0. Qué se sabe (investigación del 16-10-2026)

fomo.family **no tiene API pública**. La app usa una API interna que otros desarrolladores ya
capturaron desde navegadores reales y desde la app de iOS:

| Dato | Valor | Fuente |
|---|---|---|
| Host de la API interna | `https://prod-api.fomo.family` | [cvxv666/fomo-robinhood-radar `docs/fomo-endpoints.md`](https://github.com/cvxv666/fomo-robinhood-radar/blob/main/docs/fomo-endpoints.md) (HAR del 04-09-2026), [chainstacklabs/fomo-solana-rh-listeners](https://github.com/chainstacklabs/fomo-solana-rh-listeners) (iOS v1.87.1) |
| Endpoints de descubrimiento de tokens | `/proxy/trendingTokens`, `/proxy/filterTokens`, `/proxy/verifiedTokens`, `/proxy/tokenDetails`, `/tokenAllowList/detailed` | registry.json de Chainstack |
| Otros | `/v2/leaderboard/{24h,7d,30d}`, `/hodlers/top`, `/v2/users/{id}/swaps`, `POST /proxy/tokenWarnings` | cvxv666 |
| Auth | `Authorization: Bearer <JWT de Privy>` que **caduca a la hora**; en el navegador está en `localStorage['privy:token']` | cvxv666, phinolex/fomo-extension-bot-order |
| Cabecera extra | `x-supported-chains: 1,56,143,4663,8453,1399811149` | cvxv666 |
| IDs de red | Solana `1399811149`, Base `8453`, BSC `56`, Robinhood `4663`. Ids de token `"<address>:<networkId>"` | cvxv666, phinolex |
| Datos de mercado | los `/proxy/*` reenvían a **Codex** (defined.fi) y Mobula | cvxv666, Chainstack |
| URL de un token | `https://fomo.family/tokens/solana/<mint>` | confirmado con el link `2bCC…pump` |

**Bloqueo de Cloudflare.** `prod-api.fomo.family` rechaza en el borde todo cliente que no sea un
navegador real, con `430 {"error":"unauthorized"}`. Pasa incluso con un token válido y repitiendo
exactamente el cURL que genera Chrome. Por eso no aparecía nada al probarla desde fuera: no es un
error tuyo. Además, el login es con Google vía Privy, y Google bloquea los navegadores automatizados,
así que Playwright tampoco puede iniciar sesión.

**Qué hace el radar con esto:**

1. **Vía D (recomendada, sin API):** la app FOMO firma y paga *todas* sus transacciones en Solana
   con la cuenta `AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51`. El radar se suscribe a ella por RPC
   y ve en vivo qué tokens compran los usuarios de FOMO, con compras, traders únicos y USD en 5 min.
   No pasa por Cloudflare ni necesita sesión. Configura `FOMO_MODES=d` y `SOLANA_RPC_WSS/HTTPS`.
2. **Userscript (Vía A en navegador):** `tools/fomo_bridge.user.js` corre *dentro* de tu pestaña de
   fomo.family (Tampermonkey/Violentmonkey). Reenvía al radar las respuestas de `/proxy/trendingTokens`
   y similares que la app ya recibe, y repite esa misma petición cada 15 s con tu sesión vigente.
   Ver §9.

Lo que sigue (§1-8) es el método manual por si quieres ver tú mismo las peticiones o fomo cambia
algo.

---

`fomo.family` obtiene la lista de tokens de algún endpoint (REST, GraphQL o WebSocket). Esta guía
explica cómo encontrarlo y pasarlo a `.env`.

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

Formato confirmado con un token real: `https://fomo.family/tokens/solana/2bCCHQadi4QeHWPKG3w13siCyWWQPgkFtniouTqVpump`.
En EVM sigue el mismo patrón (`/tokens/base/0x…`, `/tokens/robinhood/0x…`). Es el valor por defecto:

```env
FOMO_TOKEN_URL_TEMPLATE=https://fomo.family/tokens/{chain}/{address}
```

## 8. Buenas prácticas

- No bajes `FOMO_POLL_SECONDS` de 3-5 s. Si existe un WebSocket, úsalo en lugar de polling.
- Revisa los términos de servicio de fomo.family y de los wrappers que uses.
- Los endpoints internos cambian sin aviso. Cuando el estado `tokens:fomo:A` del dashboard pase a
  ✖, repite esta guía o ejecuta de nuevo el script de captura.

## 9. Userscript `tools/fomo_bridge.user.js` (paso a paso)

1. Instala **Tampermonkey** o **Violentmonkey** en Chrome/Brave/Firefox.
2. Crea un script nuevo, pega el contenido de `tools/fomo_bridge.user.js` y ajusta arriba:
   - `RADAR_URL` (por defecto `http://127.0.0.1:8000/api/ingest/fomo`);
   - `INGEST_TOKEN`, que debe ser igual al `INGEST_TOKEN` del `.env`;
   - `REPLAY_MS` (15 s; `0` = solo escucha pasiva).
3. Arranca el radar (`python -m radar`). Abre https://fomo.family, inicia sesión y entra en la
   sección de tokens trending o nuevos. La primera vez, Tampermonkey te pide permiso para conectar
   con `127.0.0.1`: acéptalo.
4. Abajo a la derecha aparece `radar ▸ N envíos · M tokens · K listados`. En el dashboard verás la
   fuente `tokens:fomo:bridge` y los tokens con la etiqueta **FOMO**.
5. Si `M tokens` se queda en 0 con envíos > 0, la forma del JSON cambió. El log del radar imprime
   una muestra; con ella define `FOMO_A_ITEMS_PATH` y `FOMO_A_FIELD_MAP` (el bridge usa ese mapeo).

Cómo funciona: intercepta `fetch` y `XMLHttpRequest` de la página. Solo lee las respuestas de
`prod-api.fomo.family` que coinciden con `/proxy/(trendingTokens|filterTokens|verifiedTokens|tokenDetails|…)`.
Para el replay reutiliza la URL, el método, el cuerpo y las cabeceras de la última petición que
hizo la app, con el último `Authorization` visto. Cuando Privy renueva el token, la app lo usa en su
siguiente petición y el script lo adopta. No guarda tokens ni los envía a ningún sitio fuera de
`127.0.0.1`.
