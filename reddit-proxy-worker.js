// Cloudflare Worker — Reddit API Proxy
// Deploy at: https://dash.cloudflare.com → Workers & Pages → Create Worker
// Paste this code, deploy, then copy the worker URL to Railway env var REDDIT_PROXY_URL

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname + url.search;

    // Resolve mode: follow redirects on www.reddit.com and return final URL
    // Used for /s/ short share links that need redirect resolution
    if (url.pathname.startsWith("/resolve/")) {
      const targetPath = url.pathname.slice("/resolve".length);
      const redditUrl = "https://www.reddit.com" + targetPath;
      try {
        const resp = await fetch(redditUrl, {
          headers: {
            "User-Agent":
              "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
          },
          redirect: "follow",
        });
        return new Response(JSON.stringify({ url: resp.url }), {
          headers: {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
          },
        });
      } catch (e) {
        return new Response(JSON.stringify({ url: "", error: e.message }), {
          status: 502,
          headers: {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
          },
        });
      }
    }

    // Use old.reddit.com — much less aggressive bot blocking than www.reddit.com
    // Ensure path ends with .json for Reddit API
    let apiPath = path;
    if (!apiPath.includes('.json')) {
      apiPath = apiPath.replace(/\/?(\?|$)/, '.json$1');
    }
    const redditUrl = "https://old.reddit.com" + apiPath;

    const resp = await fetch(redditUrl, {
      headers: {
        "User-Agent":
          request.headers.get("User-Agent") || "SubredditStrategyBot/2.0 (by /u/strategy_bot_admin)",
        Accept: "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        Pragma: "no-cache",
      },
      redirect: "follow",
    });

    const body = await resp.text();

    // If Reddit returned HTML despite .json URL, try to detect and set proper content type
    const ct = resp.headers.get("Content-Type") || "";
    const isJson = body.trimStart().startsWith("[") || body.trimStart().startsWith("{");
    const responseContentType = isJson ? "application/json" : ct || "application/json";

    return new Response(body, {
      status: resp.status,
      headers: {
        "Content-Type": responseContentType,
        "Access-Control-Allow-Origin": "*",
        "X-Proxy-Original-CT": ct,
      },
    });
  },
};
