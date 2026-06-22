// Cloudflare Worker — Reddit API Proxy (v2: caching + serve-stale on 429)
// Deploy: dash.cloudflare.com → Workers & Pages → create/edit Worker → paste → deploy.
// Then set REDDIT_PROXY_URL on Railway to this Worker's URL.
//
// Why v2: Cloudflare Workers egress from SHARED Cloudflare IPs that Reddit
// rate-limits collectively, so a heavy fan-out (many keywords × subs) 429-storms
// anonymous requests. Reddit OAuth is NOT available for this project, so the
// levers are: (1) CACHE responses so repeated sub-hits don't re-hit Reddit, and
// (2) on a 429, retry once and otherwise SERVE A STALE cached copy instead of
// propagating the 429. Both cut the effective request rate Reddit sees.

const FRESH_TTL = 120;   // seconds a cached copy is considered "fresh"
const STALE_TTL = 1800;  // seconds we keep a copy around to serve on 429 (stale-if-error)

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    // --- Resolve mode: follow redirects for /s/ short links, return final URL ---
    if (url.pathname.startsWith("/resolve/")) {
      const targetPath = url.pathname.slice("/resolve".length);
      try {
        const resp = await fetch("https://www.reddit.com" + targetPath, {
          headers: { "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36" },
          redirect: "follow",
        });
        return json({ url: resp.url });
      } catch (e) {
        return json({ url: "", error: e.message }, 502);
      }
    }

    // --- Data request: route to old.reddit.com, ensure .json ---
    let apiPath = url.pathname + url.search;
    if (!apiPath.includes(".json") && !apiPath.includes(".rss")) {
      apiPath = apiPath.replace(/\/?(\?|$)/, ".json$1");
    }
    const redditUrl = "https://old.reddit.com" + apiPath;
    const cache = caches.default;
    // Cache key: the Worker URL+path (method GET). Identical sub/query requests
    // across the app's many keyword fetches collapse to one upstream hit.
    const cacheKey = new Request(url.toString(), { method: "GET" });

    // 1) Fresh cache hit → serve immediately (zero upstream load).
    const cached = await cache.match(cacheKey);
    if (cached) {
      const age = parseInt(cached.headers.get("X-Proxy-Age-Epoch") || "0", 10);
      if (age && Date.now() / 1000 - age < FRESH_TTL) {
        return withHeader(cached, "X-Proxy-Cache", "HIT");
      }
    }

    // 2) Fetch upstream (retry once on 429, honoring a small Retry-After).
    let resp = null;
    for (let attempt = 0; attempt < 2; attempt++) {
      resp = await fetch(redditUrl, {
        headers: {
          "User-Agent": request.headers.get("User-Agent") || "SubredditStrategyBot/2.0 (by /u/strategy_bot_admin)",
          Accept: "application/json",
          "Accept-Language": "en-US,en;q=0.9",
        },
        redirect: "follow",
      });
      if (resp.status !== 429 || attempt === 1) break;
      const ra = parseFloat(resp.headers.get("Retry-After") || "0");
      await sleep(Math.min(ra > 0 ? ra * 1000 : 1500, 3000));
    }

    // 3) On 429/5xx: serve a STALE cached copy if we have one (better than failing).
    if ((resp.status === 429 || resp.status >= 500) && cached) {
      return withHeader(cached, "X-Proxy-Cache", "STALE");
    }

    const body = await resp.text();
    const ct = resp.headers.get("Content-Type") || "";
    const isJson = body.trimStart().startsWith("[") || body.trimStart().startsWith("{");
    const isXml = body.trimStart().startsWith("<?xml");
    const outCT = isJson ? "application/json" : (isXml ? "application/atom+xml" : ct || "application/json");

    const out = new Response(body, {
      status: resp.status,
      headers: {
        "Content-Type": outCT,
        "Access-Control-Allow-Origin": "*",
        "X-Proxy-Original-CT": ct,
        "X-Proxy-Cache": "MISS",
        // Stamp time + a long s-maxage so cache.match keeps a stale copy for STALE_TTL.
        "X-Proxy-Age-Epoch": String(Math.floor(Date.now() / 1000)),
        "Cache-Control": `public, max-age=${STALE_TTL}`,
      },
    });

    // 4) Cache only successful, real payloads (don't cache 429/HTML challenge pages).
    if (resp.status === 200 && (isJson || isXml)) {
      ctx.waitUntil(cache.put(cacheKey, out.clone()));
    }
    return out;
  },
};

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
  });
}
function withHeader(resp, k, v) {
  const r = new Response(resp.body, resp);
  r.headers.set(k, v);
  return r;
}
function sleep(ms) { return new Promise((res) => setTimeout(res, ms)); }
