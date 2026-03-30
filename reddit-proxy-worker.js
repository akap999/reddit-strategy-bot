// Cloudflare Worker — Reddit API Proxy
// Deploy at: https://dash.cloudflare.com → Workers & Pages → Create Worker
// Paste this code, deploy, then copy the worker URL to Railway env var REDDIT_PROXY_URL

export default {
  async fetch(request) {
    const url = new URL(request.url);
    const path = url.pathname + url.search;

    // Use old.reddit.com — much less aggressive bot blocking than www.reddit.com
    const redditUrl = "https://old.reddit.com" + path;

    const resp = await fetch(redditUrl, {
      headers: {
        "User-Agent":
          "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        Accept:
          "text/html,application/xhtml+xml,application/xml;q=0.9,application/json;q=0.8,*/*;q=0.7",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Cache-Control": "no-cache",
        Pragma: "no-cache",
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
