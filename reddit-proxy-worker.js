// Cloudflare Worker — Reddit API Proxy (v3: in-isolate cache + serve-stale on 429)
// Deploy: dash.cloudflare.com → Workers & Pages → open your existing Worker →
// Edit code → paste (replace all) → Deploy. URL stays the same, so REDDIT_PROXY_URL
// on Railway is unchanged. No secrets, no KV, no custom domain needed.
//
// Why v3: this Worker runs on a *.workers.dev URL, where Cloudflare's Cache API
// (caches.default) is a NO-OP. So we cache in a module-level Map that persists
// across requests handled by the same isolate (best-effort, free, no limits).
// Reddit OAuth is NOT available for this project, so the levers are: cache
// responses (collapse repeat sub/query hits) and, on a 429, retry once then
// SERVE A STALE cached copy instead of propagating the 429.

const FRESH_TTL = 120;    // seconds a cached copy is "fresh"
const STALE_TTL = 1800;   // seconds we keep a copy to serve on 429 / 5xx
const MAX_ENTRIES = 200;  // cap isolate memory (~200 × ≤400KB)
const MAX_BODY = 400_000; // don't cache huge bodies

// Module scope persists across requests in the same isolate (best-effort cache).
const MEM = new Map(); // key -> { body, ct, ts }

export default {
  async fetch(request) {
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
    const key = apiPath;
    const now = Date.now() / 1000;

    // 1) Fresh in-isolate hit → serve immediately (zero upstream load).
    const entry = MEM.get(key);
    if (entry && now - entry.ts < FRESH_TTL) {
      return respond(entry.body, entry.ct, 200, "HIT");
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

    // 3) On 429/5xx: serve a STALE cached copy if we have one within STALE_TTL.
    if ((resp.status === 429 || resp.status >= 500) && entry && now - entry.ts < STALE_TTL) {
      return respond(entry.body, entry.ct, 200, "STALE");
    }

    const body = await resp.text();
    const ct = resp.headers.get("Content-Type") || "";
    const isJson = body.trimStart().startsWith("[") || body.trimStart().startsWith("{");
    const isXml = body.trimStart().startsWith("<?xml");
    const outCT = isJson ? "application/json" : (isXml ? "application/atom+xml" : ct || "application/json");

    // 4) Cache only successful, real payloads (skip 429/HTML challenge/huge bodies).
    if (resp.status === 200 && (isJson || isXml) && body.length <= MAX_BODY) {
      MEM.delete(key);                       // move to most-recent (LRU-ish)
      MEM.set(key, { body, ct: outCT, ts: now });
      if (MEM.size > MAX_ENTRIES) MEM.delete(MEM.keys().next().value); // evict oldest
    }
    return respond(body, outCT, resp.status, "MISS");
  },
};

function respond(body, ct, status, cacheState) {
  return new Response(body, {
    status,
    headers: {
      "Content-Type": ct,
      "Access-Control-Allow-Origin": "*",
      "X-Proxy-Cache": cacheState,
    },
  });
}
function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
  });
}
function sleep(ms) { return new Promise((res) => setTimeout(res, ms)); }
