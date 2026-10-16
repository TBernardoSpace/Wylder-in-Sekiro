"""Configuración centralizada leída de variables de entorno (y de un archivo .env opcional)."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def load_dotenv(path: str | os.PathLike = ".env") -> None:
    """Cargador mínimo de .env (KEY=VALUE). No sobreescribe variables ya definidas."""
    p = Path(path)
    if not p.is_file():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = re.sub(r"(^|\s+)#.*$", "", value).strip()  # comentario al final de línea
        os.environ.setdefault(key.strip(), value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _bool(name: str, default: bool) -> bool:
    v = _env(name)
    return default if not v else v.lower() in {"1", "true", "yes", "on", "si", "sí"}


def _float(name: str, default: float) -> float:
    v = _env(name)
    return float(v) if v else default


def _int(name: str, default: int) -> int:
    v = _env(name)
    return int(v) if v else default


def _list(name: str, default: str = "") -> list[str]:
    return [x.strip() for x in _env(name, default).split(",") if x.strip()]


def _json(name: str, default: Any) -> Any:
    v = _env(name)
    if not v:
        return default
    try:
        return json.loads(v)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} no es JSON válido: {exc}") from exc


@dataclass
class Settings:
    # --- General ---
    demo_mode: bool = False
    db_path: str = "radar.db"
    host: str = "127.0.0.1"
    port: int = 8000
    log_level: str = "INFO"

    # --- LLM (Claude) ---
    llm_enabled: bool = True
    llm_model: str = "claude-haiku-5-5"
    llm_effort: str = "low"
    llm_timeout_s: float = 12.0
    llm_max_concurrency: int = 4

    # --- News Watcher ---
    news_poll_seconds: float = 10.0
    news_max_age_minutes: int = 60
    cryptopanic_token: str = ""
    cryptopanic_url: str = "https://cryptopanic.com/api/developer/v2/posts/"
    newsapi_key: str = ""
    newsapi_query: str = ""
    newsapi_country: str = "us"
    gdelt_enabled: bool = False
    gdelt_query: str = '(trump OR musk OR "breaking") sourcelang:english'
    rss_feeds: list[str] = field(default_factory=list)
    ws_news_url: str = ""
    ws_news_headers: dict = field(default_factory=dict)
    ws_news_subscribe: list = field(default_factory=list)

    # --- Fomo Token Scanner ---
    fomo_modes: list[str] = field(default_factory=lambda: ["c"])
    fomo_poll_seconds: float = 5.0
    fomo_token_url_template: str = "https://fomo.family/tokens/{chain}/{address}"
    token_max_age_minutes: int = 240
    chains: list[str] = field(default_factory=lambda: ["solana", "base"])
    # Vía A (endpoints internos)
    fomo_a_url: str = ""
    fomo_a_method: str = "GET"
    fomo_a_body: Any = None
    fomo_a_headers: dict = field(default_factory=dict)
    fomo_a_items_path: str = ""
    fomo_a_field_map: dict = field(default_factory=dict)
    fomo_a_default_chain: str = "solana"
    fomo_a_ws_url: str = ""
    fomo_a_ws_subscribe: list = field(default_factory=list)
    fomo_a_playwright: bool = False
    fomo_a_playwright_page: str = "https://fomo.family/"
    fomo_a_playwright_url_regex: str = r"api|graphql|tokens|trending"
    chromium_path: str = ""
    # Vía A en navegador (userscript tools/fomo_bridge.user.js -> POST /api/ingest/fomo)
    ingest_token: str = ""
    # Vía D (flujo on-chain de la app FOMO en Solana)
    solana_rpc_wss: str = ""
    solana_rpc_https: str = ""
    fomo_flow_transport: str = "logs"  # logs (cualquier RPC) | blocks (blockSubscribe)
    fomo_flow_window_s: float = 300.0
    fomo_flow_min_buyers: int = 2
    fomo_flow_max_rps: float = 8.0
    min_fomo_buyers_5m: int = 0
    # Vía B (wrappers de terceros)
    fomo_b_url: str = ""
    fomo_b_api_key: str = ""
    fomo_b_api_key_header: str = "x-api-key"
    fomo_b_category: str = "Memes"
    fomo_b_items_path: str = ""
    fomo_b_field_map: dict = field(default_factory=dict)
    fomo_b_default_chain: str = "solana"
    # Vía C (on-chain fallback)
    fomo_c_sources: list[str] = field(default_factory=lambda: ["dexscreener", "pumpfun"])
    pumpfun_url: str = (
        "https://frontend-api-v3.pump.fun/coins?offset=0&limit=50"
        "&sort=created_timestamp&order=DESC&includeNsfw=false"
    )
    require_on_fomo: bool = False
    # Stream de pump.fun: pumpportal (gratis, solo lanzamientos) | rpc (eventos on-chain: lanzamientos
    # + cada compra/venta en la curva, usa SOLANA_RPC_WSS) | off
    pumpfun_stream: str = "pumpportal"
    pumpportal_wss: str = "wss://pumpportal.fun/api/data"
    ipfs_gateway: str = "https://ipfs.io/ipfs/"
    pumpfun_coin_url: str = "https://frontend-api-v3.pump.fun/coins/{mint}"
    sol_price_usd: float = 0.0  # 0 = se obtiene de DexScreener
    # Detector de narrativas emergentes (clúster de tokens con el mismo tema)
    narrative_enabled: bool = True
    narrative_window_min: float = 10.0
    narrative_min_tokens: int = 4
    narrative_spike_ratio: float = 3.0
    narrative_baseline_hours: float = 6.0
    narrative_warmup_min: float = 15.0
    narrative_cooldown_min: float = 60.0

    # --- Matcher ---
    news_window_hours: float = 6.0
    fuzzy_prefilter: float = 55.0
    match_threshold: float = 80.0
    llm_weight: float = 0.7
    max_news_per_judge: int = 8
    recheck_seconds: float = 15.0
    recheck_minutes: float = 30.0

    # --- Filtros de seguridad ---
    min_liquidity_usd: float = 5000.0
    min_volume_5m: float = 1000.0
    require_volume_growth: bool = True
    volume_growth_min_pct: float = 0.0
    honeypot_policy: str = "warn"  # strict | warn | off
    leader_switch_ratio: float = 2.0
    enrich_with_dexscreener: bool = True

    # --- Alertas ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    discord_webhook_url: str = ""
    discord_mention: str = ""

    @property
    def anthropic_configured(self) -> bool:
        return bool(_env("ANTHROPIC_API_KEY") or _env("ANTHROPIC_AUTH_TOKEN"))

    @classmethod
    def from_env(cls, env_file: str | None = ".env") -> "Settings":
        if env_file:
            load_dotenv(env_file)
        return cls(
            demo_mode=_bool("DEMO_MODE", False),
            db_path=_env("DB_PATH", "radar.db"),
            host=_env("HOST", "127.0.0.1"),
            port=_int("PORT", 8000),
            log_level=_env("LOG_LEVEL", "INFO"),
            llm_enabled=_bool("LLM_ENABLED", True),
            llm_model=_env("LLM_MODEL", "claude-haiku-5-5"),
            llm_effort=_env("LLM_EFFORT", "low"),
            llm_timeout_s=_float("LLM_TIMEOUT_S", 12.0),
            llm_max_concurrency=_int("LLM_MAX_CONCURRENCY", 4),
            news_poll_seconds=_float("NEWS_POLL_SECONDS", 10.0),
            news_max_age_minutes=_int("NEWS_MAX_AGE_MINUTES", 60),
            cryptopanic_token=_env("CRYPTOPANIC_TOKEN"),
            cryptopanic_url=_env("CRYPTOPANIC_URL", "https://cryptopanic.com/api/developer/v2/posts/"),
            newsapi_key=_env("NEWSAPI_KEY"),
            newsapi_query=_env("NEWSAPI_QUERY"),
            newsapi_country=_env("NEWSAPI_COUNTRY", "us"),
            gdelt_enabled=_bool("GDELT_ENABLED", False),
            gdelt_query=_env("GDELT_QUERY", '(trump OR musk OR "breaking") sourcelang:english'),
            rss_feeds=_list("RSS_FEEDS"),
            ws_news_url=_env("WS_NEWS_URL"),
            ws_news_headers=_json("WS_NEWS_HEADERS", {}),
            ws_news_subscribe=_json("WS_NEWS_SUBSCRIBE", []),
            fomo_modes=[m.lower() for m in _list("FOMO_MODES", "c")],
            fomo_poll_seconds=_float("FOMO_POLL_SECONDS", 5.0),
            fomo_token_url_template=_env(
                "FOMO_TOKEN_URL_TEMPLATE", "https://fomo.family/tokens/{chain}/{address}"
            ),
            token_max_age_minutes=_int("TOKEN_MAX_AGE_MINUTES", 240),
            chains=[c.lower() for c in _list("CHAINS", "solana,base")],
            fomo_a_url=_env("FOMO_A_URL"),
            fomo_a_method=_env("FOMO_A_METHOD", "GET").upper(),
            fomo_a_body=_json("FOMO_A_BODY", None),
            fomo_a_headers=_json("FOMO_A_HEADERS", {}),
            fomo_a_items_path=_env("FOMO_A_ITEMS_PATH"),
            fomo_a_field_map=_json("FOMO_A_FIELD_MAP", {}),
            fomo_a_default_chain=_env("FOMO_A_DEFAULT_CHAIN", "solana"),
            fomo_a_ws_url=_env("FOMO_A_WS_URL"),
            fomo_a_ws_subscribe=_json("FOMO_A_WS_SUBSCRIBE", []),
            fomo_a_playwright=_bool("FOMO_A_PLAYWRIGHT", False),
            fomo_a_playwright_page=_env("FOMO_A_PLAYWRIGHT_PAGE", "https://fomo.family/"),
            fomo_a_playwright_url_regex=_env("FOMO_A_PLAYWRIGHT_URL_REGEX", r"api|graphql|tokens|trending"),
            chromium_path=_env("CHROMIUM_PATH"),
            ingest_token=_env("INGEST_TOKEN"),
            solana_rpc_wss=_env("SOLANA_RPC_WSS"),
            solana_rpc_https=_env("SOLANA_RPC_HTTPS"),
            fomo_flow_transport=_env("FOMO_FLOW_TRANSPORT", "logs").lower(),
            fomo_flow_window_s=_float("FOMO_FLOW_WINDOW_S", 300.0),
            fomo_flow_min_buyers=_int("FOMO_FLOW_MIN_BUYERS", 2),
            fomo_flow_max_rps=_float("FOMO_FLOW_MAX_RPS", 8.0),
            min_fomo_buyers_5m=_int("MIN_FOMO_BUYERS_5M", 0),
            fomo_b_url=_env("FOMO_B_URL"),
            fomo_b_api_key=_env("FOMO_B_API_KEY"),
            fomo_b_api_key_header=_env("FOMO_B_API_KEY_HEADER", "x-api-key"),
            fomo_b_category=_env("FOMO_B_CATEGORY", "Memes"),
            fomo_b_items_path=_env("FOMO_B_ITEMS_PATH"),
            fomo_b_field_map=_json("FOMO_B_FIELD_MAP", {}),
            fomo_b_default_chain=_env("FOMO_B_DEFAULT_CHAIN", "solana"),
            fomo_c_sources=[s.lower() for s in _list("FOMO_C_SOURCES", "dexscreener,pumpfun")],
            pumpfun_url=_env("PUMPFUN_URL", cls.pumpfun_url),
            require_on_fomo=_bool("REQUIRE_ON_FOMO", False),
            pumpfun_stream=_env("PUMPFUN_STREAM", "pumpportal").lower(),
            pumpportal_wss=_env("PUMPPORTAL_WSS", "wss://pumpportal.fun/api/data"),
            ipfs_gateway=_env("IPFS_GATEWAY", "https://ipfs.io/ipfs/"),
            pumpfun_coin_url=_env("PUMPFUN_COIN_URL", "https://frontend-api-v3.pump.fun/coins/{mint}"),
            sol_price_usd=_float("SOL_PRICE_USD", 0.0),
            narrative_enabled=_bool("NARRATIVE_ENABLED", True),
            narrative_window_min=_float("NARRATIVE_WINDOW_MIN", 10.0),
            narrative_min_tokens=_int("NARRATIVE_MIN_TOKENS", 4),
            narrative_spike_ratio=_float("NARRATIVE_SPIKE_RATIO", 3.0),
            narrative_baseline_hours=_float("NARRATIVE_BASELINE_HOURS", 6.0),
            narrative_warmup_min=_float("NARRATIVE_WARMUP_MIN", 15.0),
            narrative_cooldown_min=_float("NARRATIVE_COOLDOWN_MIN", 60.0),
            news_window_hours=_float("NEWS_WINDOW_HOURS", 6.0),
            fuzzy_prefilter=_float("FUZZY_PREFILTER", 55.0),
            match_threshold=_float("MATCH_THRESHOLD", 80.0),
            llm_weight=_float("LLM_WEIGHT", 0.7),
            max_news_per_judge=_int("MAX_NEWS_PER_JUDGE", 8),
            recheck_seconds=_float("RECHECK_SECONDS", 15.0),
            recheck_minutes=_float("RECHECK_MINUTES", 30.0),
            min_liquidity_usd=_float("MIN_LIQUIDITY_USD", 5000.0),
            min_volume_5m=_float("MIN_VOLUME_5M", 1000.0),
            require_volume_growth=_bool("REQUIRE_VOLUME_GROWTH", True),
            volume_growth_min_pct=_float("VOLUME_GROWTH_MIN_PCT", 0.0),
            honeypot_policy=_env("HONEYPOT_POLICY", "warn").lower(),
            leader_switch_ratio=_float("LEADER_SWITCH_RATIO", 2.0),
            enrich_with_dexscreener=_bool("ENRICH_WITH_DEXSCREENER", True),
            telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=_env("TELEGRAM_CHAT_ID"),
            discord_webhook_url=_env("DISCORD_WEBHOOK_URL"),
            discord_mention=_env("DISCORD_MENTION"),
        )
