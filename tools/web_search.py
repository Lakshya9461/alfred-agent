import httpx

try:
    from duckduckgo_search import DDGS
    HAS_DDGS = True
except ImportError:
    # Fallback if the user has `ddgs` installed but import fails or they installed something else
    HAS_DDGS = False

from config import TAVILY_API_KEY

def search(query: str, max_results: int = 5) -> str:
    """
    Searches the web for a given query.
    If TAVILY_API_KEY is set, uses Tavily's API. Otherwise falls back to ddgs.
    """
    if TAVILY_API_KEY:
        return _tavily_search(query, max_results)
    else:
        return _ddgs_search(query, max_results)

def _tavily_search(query: str, max_results: int) -> str:
    try:
        response = httpx.post(
            "https://api.tavily.com/search",
            json={
                "api_key": TAVILY_API_KEY,
                "query": query,
                "search_depth": "basic",
                "max_results": max_results
            },
            timeout=10.0
        )
        response.raise_for_status()
        data = response.json()
        
        results = data.get("results", [])
        if not results:
            return "No results found."
            
        formatted = []
        for i, res in enumerate(results, 1):
            title = res.get("title", "No Title")
            url = res.get("url", "")
            content = res.get("content", "")
            formatted.append(f"{i}. {title}\n   URL: {url}\n   Snippet: {content}\n")
            
        return "\n".join(formatted)
    except Exception as e:
        return f"Web search failed (Tavily): {e}"

def _ddgs_search(query: str, max_results: int) -> str:
    if not HAS_DDGS:
        return "DDGS library not available. Please install duckduckgo-search."
    try:
        formatted = []
        with DDGS() as ddgs:
            results = ddgs.text(query, max_results=max_results)
            if not results:
                return "No results found."
                
            for i, res in enumerate(results, 1):
                title = res.get("title", "No Title")
                url = res.get("href", "")
                snippet = res.get("body", "")
                formatted.append(f"{i}. {title}\n   URL: {url}\n   Snippet: {snippet}\n")
                
        if not formatted:
            return "No results found."
        return "\n".join(formatted)
    except Exception as e:
        return f"Web search failed (DDGS): {e}"
