"""A network-free stand-in for generators.base.ClaudeClient, for $0 offline blog-pipeline tests.

Every method matches the real signature and default return SHAPE, so a BlogGenerator built with a
StubClaude exercises the real pipeline logic without any API call. Handlers let a test script the
responses (usually by sniffing the prompt/brief), e.g.:

    StubClaude(call_handler=lambda p: {...}, search_handler=lambda brief, allowed, blocked: [...])
"""


class StubClaude:
    def __init__(self, *, call_handler=None, search_handler=None,
                 site_facts=None, official_domain=None, model="claude-sonnet-4-6"):
        self.model = model
        self.last_error = None
        self._usage = {"input_tokens": 0, "output_tokens": 0, "web_search_requests": 0}
        self._call_handler = call_handler
        self._search_handler = search_handler
        self._site_facts = site_facts
        self._official_domain = official_domain
        # Recorders so a test can assert what was asked.
        self.calls = []
        self.searches = []
        self.site_fact_calls = []
        self.domain_calls = []

    # --- usage accounting (mirrors the real client so cost plumbing doesn't crash) ---
    def reset_usage(self):
        self._usage = {"input_tokens": 0, "output_tokens": 0, "web_search_requests": 0}

    def usage_cost(self):
        return 0.0

    # --- LLM calls ---
    def call(self, prompt, max_tokens=1024, max_retries=3, temperature=None, system_prompt=None):
        self.calls.append(prompt)
        if callable(self._call_handler):
            r = self._call_handler(prompt)
            return r if isinstance(r, dict) else {}
        return {}

    def call_text(self, prompt, max_tokens=1024, temperature=None, system_prompt=None):
        if callable(self._call_handler):
            r = self._call_handler(prompt)
            return r if isinstance(r, str) else ""
        return ""

    # --- web-search-backed helpers ---
    def search_sources(self, brief, max_searches=4, allowed_domains=None, blocked_domains=None,
                       first_party=False):
        self.searches.append({"brief": brief, "allowed": allowed_domains,
                              "blocked": blocked_domains, "first_party": first_party})
        if callable(self._search_handler):
            return self._search_handler(brief, allowed_domains, blocked_domains) or []
        return []

    def fetch_site_facts(self, domain, brand, brief, max_searches=2):
        self.site_fact_calls.append({"domain": domain, "brand": brand, "brief": brief})
        if callable(self._site_facts):
            return self._site_facts(domain, brand, brief) or ""
        return ""

    def find_official_domain(self, brand, context=""):
        self.domain_calls.append({"brand": brand, "context": context})
        if callable(self._official_domain):
            return self._official_domain(brand, context) or ""
        return ""
