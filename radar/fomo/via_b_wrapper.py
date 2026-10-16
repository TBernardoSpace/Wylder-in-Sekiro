"""Vía B — Wrappers de terceros sobre fomo.family (api.fomoapi.io, parse.bot `fomo-family-api`, ...).

Estos servicios cambian de rutas y planes con frecuencia, así que el adaptador es genérico:
  FOMO_B_URL            URL completa del listado (puede incluir {category}), copiada de la doc del proveedor
  FOMO_B_API_KEY        clave del proveedor
  FOMO_B_API_KEY_HEADER cabecera donde va la clave (x-api-key, Authorization, ...). Usa "query:<param>"
                        para enviarla como parámetro de URL (?api_key=...).
  FOMO_B_ITEMS_PATH     ruta a la lista de tokens en la respuesta (vacío = autodetección)
  FOMO_B_FIELD_MAP      mapeo opcional de campos (JSON)
"""
from __future__ import annotations

from ..config import Settings
from .base import JsonHttpTokenSource


class FomoWrapperSource(JsonHttpTokenSource):
    def __init__(self, s: Settings) -> None:
        if not s.fomo_b_url:
            raise ValueError("FOMO_MODES incluye 'b' pero FOMO_B_URL está vacío")
        headers: dict[str, str] = {"Accept": "application/json"}
        params: dict[str, str] = {}
        key_header = s.fomo_b_api_key_header
        if s.fomo_b_api_key:
            if key_header.lower().startswith("query:"):
                params[key_header.split(":", 1)[1]] = s.fomo_b_api_key
            elif key_header.lower() == "authorization":
                headers["Authorization"] = f"Bearer {s.fomo_b_api_key}"
            else:
                headers[key_header] = s.fomo_b_api_key
        super().__init__(
            name="fomo:B",
            url=s.fomo_b_url.replace("{category}", s.fomo_b_category),
            headers=headers,
            params=params,
            items_path=s.fomo_b_items_path,
            field_map=s.fomo_b_field_map,
            default_chain=s.fomo_b_default_chain,
            url_template=s.fomo_token_url_template,
            on_fomo=True,
        )
