// ==UserScript==
// @name         fomo.family → Signal Radar bridge
// @namespace    news-token-radar
// @version      1.0.0
// @description  Reenvía al radar local los listados de tokens que la propia app de fomo.family carga (trending, verified, filterTokens, tokenDetails).
// @match        https://fomo.family/*
// @run-at       document-start
// @grant        GM_xmlhttpRequest
// @grant        unsafeWindow
// @connect      127.0.0.1
// @connect      localhost
// ==/UserScript==

/*
 * Por qué en el navegador: prod-api.fomo.family está detrás de Cloudflare y rechaza en el borde
 * cualquier cliente que no sea un navegador real (HTTP 430), aunque lleve un token válido. Además,
 * el JWT de sesión (Privy) caduca cada hora. Este script no "ataca" nada: escucha las respuestas
 * que la app YA recibe en tu sesión y, opcionalmente, repite la misma petición de listado que la
 * app hizo (misma URL, cuerpo y cabeceras) cada REPLAY_MS para que el radar reciba datos frescos
 * aunque no toques la página.
 *
 * Instalación: Tampermonkey o Violentmonkey → "Crear script" → pega este archivo → guarda.
 * Abre https://fomo.family, entra en la sección de tokens trending/nuevos y deja la pestaña abierta.
 */
(function () {
  'use strict';

  // ---------------- configuración ----------------
  const RADAR_URL = 'http://127.0.0.1:8000/api/ingest/fomo';
  const INGEST_TOKEN = '';          // = INGEST_TOKEN del .env (recomendado)
  const REPLAY_MS = 15000;          // 0 = solo escucha pasiva
  const FORWARD = /prod-api\.fomo\.family\/(proxy\/(trendingTokens|filterTokens|verifiedTokens|tokenDetails|mostHeld|cryptoTokens)|tokenAllowList)/i;
  const REPLAYABLE = /proxy\/(trendingTokens|verifiedTokens)|tokenAllowList/i;  // listados, no detalles
  // ------------------------------------------------

  const w = typeof unsafeWindow !== 'undefined' ? unsafeWindow : window;
  const origFetch = w.fetch.bind(w);
  const lastRequests = new Map();   // ruta -> {url, method, body, headers}
  let lastAuth = null, sent = 0, accepted = 0, errors = 0;

  function forward(url, payload) {
    GM_xmlhttpRequest({
      method: 'POST',
      url: RADAR_URL,
      headers: Object.assign({ 'content-type': 'application/json' }, INGEST_TOKEN ? { 'x-radar-token': INGEST_TOKEN } : {}),
      data: JSON.stringify({ url, payload }),
      onload: (r) => {
        sent += 1;
        try { accepted += JSON.parse(r.responseText).accepted || 0; } catch (e) { errors += 1; }
        badge();
      },
      onerror: () => { errors += 1; badge(); },
    });
  }

  function headersToObject(h) {
    const out = {};
    try { new Headers(h || {}).forEach((v, k) => { if (!/^(host|content-length|cookie)$/i.test(k)) out[k] = v; }); } catch (e) { /* noop */ }
    return out;
  }

  function remember(url, method, body, headers, credentials) {
    if (headers.authorization) lastAuth = headers.authorization;
    if (!REPLAYABLE.test(url)) return;
    const key = url.split('?')[0];
    lastRequests.set(key, { url, method: method || 'GET', body: typeof body === 'string' ? body : undefined, headers,
                            credentials: credentials || 'same-origin' });
  }

  // --- fetch ---
  w.fetch = function (input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const p = origFetch(input, init);
    try {
      if (url.includes('prod-api.fomo.family')) {
        const headers = headersToObject((init && init.headers) || (input instanceof w.Request ? input.headers : undefined));
        if (headers.authorization) lastAuth = headers.authorization;
        if (FORWARD.test(url)) {
          remember(url, (init && init.method) || (input && input.method), init && init.body, headers,
                   (init && init.credentials) || (input instanceof w.Request ? input.credentials : undefined));
          p.then((res) => res.clone().json().then((j) => forward(url, j)).catch(() => {})).catch(() => {});
        }
      }
    } catch (e) { /* nunca romper la app */ }
    return p;
  };

  // --- XMLHttpRequest ---
  const XHR = w.XMLHttpRequest.prototype;
  const origOpen = XHR.open, origSend = XHR.send, origSet = XHR.setRequestHeader;
  XHR.open = function (method, url) { this.__r = { method, url: String(url || ''), headers: {} }; return origOpen.apply(this, arguments); };
  XHR.setRequestHeader = function (k, v) { try { this.__r && (this.__r.headers[String(k).toLowerCase()] = v); } catch (e) { /* noop */ } return origSet.apply(this, arguments); };
  XHR.send = function (body) {
    const r = this.__r;
    try {
      if (r && FORWARD.test(r.url)) {
        remember(r.url, r.method, body, r.headers, this.withCredentials ? 'include' : 'same-origin');
        this.addEventListener('load', () => { try { forward(r.url, JSON.parse(this.responseText)); } catch (e) { /* no JSON */ } });
      } else if (r && r.url.includes('prod-api.fomo.family') && r.headers.authorization) {
        lastAuth = r.headers.authorization;
      }
    } catch (e) { /* noop */ }
    return origSend.apply(this, arguments);
  };

  // --- replay de los listados con la sesión vigente ---
  if (REPLAY_MS > 0) {
    setInterval(async () => {
      for (const req of lastRequests.values()) {
        const headers = Object.assign({}, req.headers, lastAuth ? { authorization: lastAuth } : {});
        try {
          const res = await origFetch(req.url, { method: req.method, headers, body: req.method === 'GET' ? undefined : req.body, credentials: req.credentials });
          if (res.ok) forward(req.url, await res.json());
          else errors += 1;   // 401/430: la app renovará el token en su próxima petición
        } catch (e) { errors += 1; }
        await new Promise((r) => setTimeout(r, 400));
      }
      badge();
    }, REPLAY_MS);
  }

  // --- indicador en pantalla ---
  let el;
  function badge() {
    if (!document.body) return;
    if (!el) {
      el = document.createElement('div');
      el.style.cssText = 'position:fixed;bottom:8px;right:8px;z-index:2147483647;font:11px monospace;' +
        'background:#0d1117;color:#2ee59d;border:1px solid #2ee59d;border-radius:6px;padding:3px 8px;opacity:.85';
      document.body.appendChild(el);
    }
    el.textContent = `radar ▸ ${sent} envíos · ${accepted} tokens · ${lastRequests.size} listados${errors ? ` · ${errors} err` : ''}`;
  }
  document.addEventListener('DOMContentLoaded', badge);
})();
