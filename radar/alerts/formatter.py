"""Formato de alertas (Telegram HTML y Discord embed)."""
from __future__ import annotations

from html import escape

from ..models import Signal
from ..utils import human_usd, minutes_ago

CHAIN_LABEL = {"solana": "Solana", "base": "Base", "ethereum": "Ethereum", "bsc": "BSC"}


def _trend_icon(trend: str | None) -> str:
    return {"up": "↑", "down": "↓", "flat": "→"}.get(trend or "", "?")


def telegram_text(sig: Signal, new_leader: bool = False) -> str:
    n, t, sf = sig.news, sig.token, sig.safety
    header = "🔁 <b>NUEVO LÍDER DE LIQUIDEZ</b>" if new_leader else "🚨 <b>SEÑAL DE COMPRA</b>"
    llm = f" · LLM {sig.llm_score:.0f}" if sig.llm_score is not None else ""
    holders = f"{t.holder_count:,}" if t.holder_count is not None else "n/d"
    lines = [
        f"{header} · score <b>{sig.match_score:.0f}/100</b>{llm}",
        "",
        f"📰 <b>{escape(n.headline)}</b>",
        f"🗞 {escape(n.source)} · {minutes_ago(n.published_at)}" + (f' · <a href="{escape(n.url)}">link</a>' if n.url else ""),
        "",
        f"🪙 <b>{escape(t.name)} (${escape(t.symbol)})</b> · {CHAIN_LABEL.get(t.chain, t.chain)}"
        + (" · ✅ en Fomo" if t.on_fomo else ""),
        f"💰 MC {human_usd(t.market_cap)} | 💧 Liq {human_usd(t.liquidity_usd)}",
        f"📊 Vol 5m {human_usd(t.volume_5m)} {_trend_icon(sf.volume_trend if sf else None)}"
        + (f" ({t.buys_5m}B/{t.sells_5m}S)" if t.buys_5m is not None else "")
        + f" | 👥 Holders {holders}",
        f"⏱ Token creado {minutes_ago(t.created_at)}",
        "",
        f"🧠 <i>{escape(sig.reason)}</i>",
    ]
    if sf and sf.warnings:
        lines.append("🛡 " + escape(" · ".join(sf.warnings[:3])))
    elif sf:
        lines.append(f"🛡 Seguridad: {sf.honeypot}")
    lines += ["", f"📋 CA (toca para copiar):\n<code>{escape(t.token_address)}</code>"]
    return "\n".join(lines)


def telegram_keyboard(sig: Signal) -> dict:
    t = sig.token
    row1 = []
    if t.fomo_url:
        row1.append({"text": "🟢 Comprar en Fomo", "url": t.fomo_url})
    if t.dexscreener_url:
        row1.append({"text": "📈 DexScreener", "url": t.dexscreener_url})
    # copy_text: botón nativo de Telegram que copia el CA al portapapeles (Bot API 7.11+)
    row2 = [{"text": "📋 Copiar CA", "copy_text": {"text": t.token_address}}]
    if sig.news.url:
        row2.append({"text": "📰 Noticia", "url": sig.news.url})
    return {"inline_keyboard": [r for r in (row1, row2) if r]}


def discord_payload(sig: Signal, mention: str = "", new_leader: bool = False) -> dict:
    n, t, sf = sig.news, sig.token, sig.safety
    title = f"{'🔁 Nuevo líder' if new_leader else '🚨 Señal'}: {t.name} (${t.symbol}) · {sig.match_score:.0f}/100"
    links = " · ".join(x for x in (
        f"[🟢 Comprar en Fomo]({t.fomo_url})" if t.fomo_url else "",
        f"[📈 DexScreener]({t.dexscreener_url})" if t.dexscreener_url else "",
        f"[📰 Noticia]({n.url})" if n.url else "",
    ) if x)
    fields = [
        {"name": "Noticia", "value": f"{n.headline[:250]}\n_{n.source} · {minutes_ago(n.published_at)}_", "inline": False},
        {"name": "Red", "value": CHAIN_LABEL.get(t.chain, t.chain), "inline": True},
        {"name": "Market Cap", "value": human_usd(t.market_cap), "inline": True},
        {"name": "Liquidez", "value": human_usd(t.liquidity_usd), "inline": True},
        {"name": "Vol 5m", "value": f"{human_usd(t.volume_5m)} {_trend_icon(sf.volume_trend if sf else None)}", "inline": True},
        {"name": "Holders", "value": f"{t.holder_count:,}" if t.holder_count is not None else "n/d", "inline": True},
        {"name": "LLM", "value": sig.reason[:200] or "-", "inline": False},
        {"name": "CA", "value": f"```{t.token_address}```", "inline": False},
        {"name": "Links", "value": links or "-", "inline": False},
    ]
    if sf and sf.warnings:
        fields.append({"name": "⚠️ Avisos", "value": "\n".join(sf.warnings[:3])[:1000], "inline": False})
    return {
        "content": f"{mention} {title}".strip(),
        "allowed_mentions": {"parse": ["everyone", "roles", "users"]},
        "embeds": [{
            "title": title[:256],
            "url": t.fomo_url or t.dexscreener_url,
            "color": 0x00E676 if not new_leader else 0xFFB300,
            "fields": fields,
            "timestamp": sig.created_at.isoformat(),
        }],
    }
