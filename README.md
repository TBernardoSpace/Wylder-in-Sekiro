# News-to-Token Signal Radar · fomo.family

Bot y visor de señales: detecta noticias virales o de último momento, encuentra la memecoin que se
lanza sobre esa narrativa en `fomo.family`, verifica liquidez/volumen/honeypot y te alerta en
segundos por **Telegram / Discord** y en un **dashboard en vivo**.

> ⚠️ Herramienta de análisis, no consejo financiero. Las memecoins recién lanzadas son extremadamente
> volátiles y la mayoría van a cero. Los filtros reducen el riesgo pero no lo eliminan.

```bash
pip install -r requirements.txt
python -m radar --demo          # simulación offline → http://localhost:8000
cp .env.example .env            # luego rellena claves y elige vías
python -m radar
```

---

## 1. Arquitectura

**Stack elegido:** Python 3.10+ · `asyncio` · FastAPI/uvicorn · httpx · websockets · SQLite (WAL) ·
rapidfuzz · SDK oficial de Anthropic (Claude Haiku) · Playwright (opcional).

Python + asyncio es aquí más robusto que Node porque todo el trabajo es I/O concurrente (decenas de
polls/WS simultáneos). Además, el ecosistema de scraping (Playwright), NLP/fuzzy (rapidfuzz, C++) y
los SDKs de LLM son de primera clase. Un único proceso basta para cientos de eventos por segundo.

Arquitectura **event-driven**: cada módulo publica en un bus pub/sub en memoria y no conoce a los demás.

```mermaid
flowchart LR
  subgraph Ingesta de noticias
    CP[CryptoPanic] --> NW
    NA[NewsAPI] --> NW
    GD[GDELT] --> NW
    RSS[RSS / puentes X<br/>WatcherGuru, DB...] --> NW
    WS[WebSocket push<br/>Tree of Alpha] --> NW
    NW[News Watcher<br/>dedupe + edad] --> EX[Extractor<br/>Claude Haiku + heurística]
  end
  subgraph Token Scanner
    D[Vía D ★<br/>compras de la app FOMO<br/>on-chain, co-firmante] --> TS
    A[Vía A<br/>userscript en fomo.family<br/>/ XHR internos / Playwright] --> TS
    B[Vía B<br/>fomoapi.io / parse.bot] --> TS
    C[Vía C<br/>pump.fun en vivo<br/>+ DexScreener] --> TS
    TS[Scanner<br/>merge + on_fomo]
  end
  EX -- news --> BUS((Event Bus))
  TS -- token / token_update --> BUS
  BUS --> NR[Narrativas emergentes<br/>clúster de tokens] -- news sintética --> BUS
  BUS --> M[Matcher<br/>fuzzy → LLM judge]
  M --> SF[Filtro seguridad<br/>liq · vol 5m ↑ · honeypot]
  SF --> L{¿Líder de la<br/>noticia?}
  L -- sí, 1ª vez --> AL[Telegram / Discord]
  L --> BUS
  BUS --> DB[(SQLite<br/>dedupe/anti-spam)]
  BUS --> API[FastAPI /ws] --> UI[Dashboard 3 columnas<br/>+ alerta sonora]
```

### Flujo de datos paso a paso

1. **Ingesta.** Cada fuente se consulta cada `NEWS_POLL_SECONDS`, o recibe push por WebSocket.
   Cada titular se normaliza y se convierte en un `sha1`. Así, la misma noticia publicada por varias
   fuentes se procesa una sola vez, primero en memoria y luego en SQLite, que sobrevive a reinicios.
   Las noticias más viejas que `NEWS_MAX_AGE_MINUTES` se descartan.
2. **Extracción de entidades.** Claude Haiku (`claude-haiku-5-5`, `effort: low`, salida JSON con
   esquema estricto) devuelve `keywords`, `potential_tickers`, `narrative` y `meme_potential`.
   En paralelo corre una **heurística determinista**: nombres propios, cifras+unidad (`1 cent` → `1CENT`),
   cashtags, frases entre comillas y combinaciones sujeto+gancho (`TRUMPPENNY`, `TPENNY`). Ambas listas
   se fusionan, así que el sistema funciona aunque no haya API key o el LLM falle.
3. **Escaneo de tokens.** Las vías activas (`FOMO_MODES=d,c`, por ejemplo) devuelven JSON
   heterogéneo. La **Vía D** escucha en Solana todas las transacciones que firma la app FOMO
   (co-firmante `AgmLJBMD…`) y calcula por token las compras, los traders únicos y el USD de los
   últimos 5 min. Ese flujo es la señal "está explotando en fomo" sin depender de su API. Un normalizador lo convierte en `TokenInfo` (`name, symbol, token_address, chain,
   market_cap, liquidity_usd, volume_5m, holder_count, created_at, fomo_url`). Si el mismo token llega
   por varias vías, se fusiona, y si una de ellas es A o B se marca `on_fomo=True`.
   **pump.fun en vivo** (`PUMPFUN_STREAM`): cada lanzamiento llega al instante, por PumpPortal o
   decodificando los eventos on-chain con el IDL oficial de pump.fun. Se baja su metadata
   (descripción, twitter, web), y en modo `rpc` también cada compra o venta en la curva: market cap,
   volumen 5m y compras/ventas antes de que DexScreener indexe el token.
4. **Narrativas emergentes.** Si en 10 min aparecen ≥ 4 tokens nuevos con un mismo término y eso
   supera 3× su frecuencia normal, se publica una noticia sintética. Su link es el tweet o la web que
   más citan esos tokens, normalmente la noticia original. Así se detecta la ola aunque ningún feed
   haya publicado el titular todavía.
5. **Cruce (matching)**, en ambos sentidos: un token nuevo se compara con las noticias de las últimas
   `NEWS_WINDOW_HOURS`, y una noticia nueva con los tokens recientes, porque a veces el token se crea
   antes de que la noticia llegue a tus fuentes.
   - **Pre-filtro fuzzy** (Levenshtein, rapidfuzz): ticker vs. tickers potenciales, cobertura de
     palabras del nombre en keywords/titular, símbolo dentro del titular, palabras de la
     **descripción** del token y, sobre todo, si el token **enlaza el mismo tweet o la misma
     noticia** (score 100). Una sola palabra compartida (p. ej. solo "Trump") se penaliza, y en
     tickers cortos solo vale la coincidencia exacta (`TDOLLAR` ≠ `EDOLLAR`).
   - Los pares con fuzzy ≥ `FUZZY_PREFILTER` pasan al **juez LLM**, en un solo request por token con
     hasta 8 noticias: *"¿Este token recién creado hace referencia directa a esta noticia? Score 0-100
     + justificación en 1 línea"*.
   - `match_score = 0.7·LLM + 0.3·fuzzy`. Sin LLM, se usa solo el fuzzy.
6. **Filtro anti-scam** (solo si `match_score ≥ MATCH_THRESHOLD`):
   - Las métricas se refrescan con DexScreener (par con más liquidez).
   - Liquidez ≥ `MIN_LIQUIDITY_USD`.
   - Volumen 5m ≥ `MIN_VOLUME_5M` y **creciendo** según el historial de snapshots. Si aún no hay
     historial, se usa presión compradora (buys ≥ sells) como proxy.
   - **Honeypot.** En Solana se usa RugCheck: *freeze/mint authority* activas o riesgos "danger" ⇒
     descarte definitivo. En EVM (Base/ETH/BSC) se usa honeypot.is, que simula una venta, y un sell
     tax ≥ 50 % ⇒ descarte. Si el proveedor no responde pero hay ventas reales en los últimos 5 min,
     se acepta con aviso. Con `HONEYPOT_POLICY=strict` se bloquea.
   - Si no pasa por liquidez o volumen, queda en **watch** y se re-evalúa cada `RECHECK_SECONDS`
     durante `RECHECK_MINUTES`.
7. **Selección del ganador.** Por cada noticia suelen salir ~10 monedas basura. Solo se alerta la
   **líder**: cuando una noticia cruza varios tokens a la vez, se evalúan de mayor a menor
   liquidez, así que alerta primero el que concentra el dinero. Otra moneda de la misma noticia solo vuelve a
   alertar ("🔁 nuevo líder") si tiene ≥ `LEADER_SWITCH_RATIO`× su liquidez. El resto queda como
   *secundaria* en el dashboard.
8. **Anti-spam.** La tabla `alerts` usa la clave `chain:address` y un `INSERT OR IGNORE` atómico:
   un token nunca se alerta dos veces, ni tras reiniciar el proceso.
9. **Entrega.** La alerta va a Telegram (botones *Comprar en Fomo*, *DexScreener*, *Copiar CA* nativo
   y *Noticia*) y a Discord (embed + `@here`). Todos los eventos se emiten por `/ws` al dashboard.

## 2. Estructura de carpetas

```
.
├── radar/
│   ├── __main__.py            # CLI: python -m radar [--demo]
│   ├── app.py                 # ensamblado + supervisión de tareas + estado para el dashboard
│   ├── config.py              # Settings desde env/.env
│   ├── models.py              # NewsItem, TokenInfo, SafetyReport, Signal (pydantic)
│   ├── bus.py                 # Event bus pub/sub asyncio
│   ├── storage.py             # SQLite: dedupe noticias, tokens, alerts (anti-spam), signals
│   ├── llm.py                 # Claude Haiku: extracción + juez (structured outputs)
│   ├── demo.py                # escenario simulado "Trump Penny"
│   ├── news/
│   │   ├── base.py            # contratos Polling/Streaming
│   │   ├── sources.py         # CryptoPanic, NewsAPI, GDELT, RSS, WebSocket genérico
│   │   ├── extractor.py       # LLM + heurística de tickers
│   │   └── watcher.py         # orquestador de ingesta
│   ├── fomo/
│   │   ├── base.py            # TokenSource + normalizador JSON→TokenInfo
│   │   ├── via_a_network.py   # Vía A: HTTP / WS internos / Playwright stealth
│   │   ├── via_b_wrapper.py   # Vía B: wrappers de terceros
│   │   ├── via_c_onchain.py   # Vía C: DexScreener + pump.fun (polling)
│   │   ├── via_c_pumpfun_stream.py  # pump.fun en vivo: PumpPortal o eventos on-chain
│   │   ├── via_d_flow.py      # Vía D: trades de la app FOMO on-chain (logs/blockSubscribe)
│   │   └── scanner.py         # orquestador de vías
│   ├── onchain/
│   │   ├── dexscreener.py     # métricas de pares
│   │   ├── security.py        # RugCheck (Solana) / honeypot.is (EVM)
│   │   └── pumpfun.py         # decodificador de eventos (IDL oficial) + metadata IPFS
│   ├── matcher/
│   │   ├── fuzzy.py           # pre-filtro Levenshtein
│   │   ├── safety.py          # liquidez, tendencia de volumen, honeypot
│   │   ├── narratives.py      # narrativas emergentes por clúster de tokens
│   │   └── engine.py          # cruce, decisión de líder, re-chequeo
│   ├── alerts/
│   │   ├── formatter.py       # mensajes Telegram (HTML) / Discord (embed)
│   │   └── dispatcher.py      # envío con reintentos
│   └── api/server.py          # FastAPI: /, /ws, /api/snapshot, /api/signals, /health
├── web/index.html             # dashboard (HTML+JS sin dependencias, tema terminal)
├── tools/capture_fomo_network.py  # descubre endpoints de fomo.family (Vía A)
├── tools/fomo_bridge.user.js      # userscript: reenvía al radar lo que carga fomo.family
├── docs/FOMO_NETWORK_INSPECTION.md
├── tests/test_core.py
├── .env.example · requirements.txt · pyproject.toml
```

## 3. Puesta en marcha

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Configuración mínima recomendada:

```env
ANTHROPIC_API_KEY=sk-ant-...
CRYPTOPANIC_TOKEN=...                 # y/o NEWSAPI_KEY, RSS_FEEDS, WS_NEWS_URL
FOMO_MODES=d,c                        # flujo FOMO on-chain + DexScreener/pump.fun
SOLANA_RPC_WSS=wss://...              # Helius/QuickNode/Chainstack (plan gratuito sirve para empezar)
SOLANA_RPC_HTTPS=https://...
TELEGRAM_BOT_TOKEN=123:ABC...
TELEGRAM_CHAT_ID=123456789
```

```bash
python -m radar                       # dashboard en http://localhost:8000
python -m pytest -q                   # tests
```

### Telegram con sonido de prioridad
1. Crea el bot con **@BotFather** y copia el token. Escríbele `/start`.
2. Obtén tu `chat_id` con `https://api.telegram.org/bot<TOKEN>/getUpdates`, o usa un grupo/canal
   con el bot como admin.
3. Las alertas se envían con `disable_notification=false`. Telegram no permite que un bot imponga un
   tono, así que en la app abre el chat del bot → **Notificaciones → Sonido** y elige uno distintivo,
   y añádelo a las excepciones de "No molestar" del sistema.
4. `<code>CA</code>` se copia con un toque, y el botón **📋 Copiar CA** usa `copy_text` (Bot API 7.11+).

### Vías de fomo.family
fomo.family no tiene API pública. Su API interna (`prod-api.fomo.family`) está detrás de Cloudflare,
que rechaza cualquier cliente que no sea un navegador real (HTTP 430). El detalle está en
[`docs/FOMO_NETWORK_INSPECTION.md`](docs/FOMO_NETWORK_INSPECTION.md) §0.
- **D (recomendada)**: `FOMO_MODES=d` y un RPC de Solana. Ve cada compra hecha desde la app FOMO
  segundos después de confirmarse. Con `MIN_FOMO_BUYERS_5M=3`, por ejemplo, solo alertan tokens que
  ya tienen tracción real dentro de fomo.
- **A en navegador**: instala `tools/fomo_bridge.user.js` en Tampermonkey y deja abierta una pestaña
  de fomo.family. Reenvía `trendingTokens` y similares (docs §9). Las otras variantes de A (HTTP
  directo, WS, Playwright) quedan para el caso de que fomo deje de bloquear clientes externos.
- **B**: pon en `FOMO_B_URL` la URL exacta del endpoint de listado según la doc del proveedor
  (`{category}` → `FOMO_B_CATEGORY=Memes`) y su clave. El normalizador autodetecta los campos.
- **C**: funciona sin configuración. Con `FOMO_MODES=d,c` y `REQUIRE_ON_FOMO=true` solo alertan
  los tokens que, además de existir on-chain, ya compran usuarios de FOMO.

## 4. Afinar
| Síntoma | Ajuste |
|---|---|
| Demasiados falsos positivos | sube `MATCH_THRESHOLD` (85-90) o `MIN_LIQUIDITY_USD`; usa `HONEYPOT_POLICY=strict` |
| Llegas tarde | baja `FOMO_POLL_SECONDS`, usa WS (Vía A / `WS_NEWS_URL`), `REQUIRE_VOLUME_GROWTH=false` |
| Coste LLM | sube `FUZZY_PREFILTER`; el juez ya agrupa hasta 8 noticias por llamada |
| Sin API key | funciona solo con heurística + fuzzy (menos preciso con juegos de palabras) |

## 5. Limitaciones conocidas
- Los endpoints internos de fomo.family y de los wrappers no son públicos y pueden cambiar. Por eso
  la Vía A/B es 100 % configurable y las Vías C/D sirven de red de seguridad.
- La Vía D depende de que FOMO siga usando el mismo co-firmante. Si `tokens:fomo:flow` deja de
  recibir trades, revisa `docs/07-addresses.md` del repo de Chainstack Labs.
- La Vía D solo ve con certeza las compras de tokens de Solana. Las compras que cruzan a Base o
  Robinhood Chain no revelan el token en Solana.
- `holder_count` no viene en DexScreener. Se completa con RugCheck/honeypot.is o con la Vía A/B.
- SQLite + bus en memoria = un solo proceso. Para escalar horizontalmente, sustituye `bus.py` por
  Redis Streams y `storage.py` por Redis/Postgres. Las interfaces están aisladas para ello.
