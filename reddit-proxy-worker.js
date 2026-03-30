// Cloudflare Worker — Reddit API Proxy
// Deploy at: https://dash.cloudflare.com → Workers & Pages → Create Worker
// Paste this code, deploy, then copy the worker URL to Railway env var REDDIT_PROXY_URL

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname + url.search;
    const redditUrl = "https://www.reddit.com" + path;

    const resp = await fetch(redditUrl, {
      headers: {
        "User-Agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        Accept: "application/json, text/html",
      },
      redirect: "follow",
    });

    const body = await resp.text();
    return new Response(body, {
      status: resp.status,
      headers: {
        "Content-Type":
          resp.headers.get("Content-Type") || "application/json",
        "Access-Control-Allow-Origin": "*",
      },
    });
  },
};
