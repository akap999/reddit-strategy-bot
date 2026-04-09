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
          request.headers.get("User-Agent") || "SubredditStrategyBot/2.0 (by /u/strategy_bot_admin)",
        Accept: "application/json, text/html;q=0.5, */*;q=0.1",
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
