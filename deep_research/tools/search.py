import os

import requests

from retry import with_retry

TAVILY_SEARCH_URL = "https://api.tavily.com/search"


@with_retry(max_retries=2, base_delay=1.5, exceptions=(requests.RequestException,))
def tavily_search(query: str, count: int = 5) -> list[dict]:
    """真正的 Tool Use:调用 Tavily Search API(专为 AI Agent 检索场景设计),返回候选来源列表(不做任何加工)。
    网络抖动/超时/5xx 会自动重试 2 次,不会直接把整个流程崩掉。

    Tavily 只返回一层正文(content),不像博查那样区分 snippet/summary 两层,
    所以这里把 summary 留空,不编造一个不存在的字段——上层 extract.py 的 prompt 本来就把
    这两个字段当辅助线索处理,留空不影响判断逻辑。
    """
    api_key = os.environ["TAVILY_API_KEY"]
    resp = requests.post(
        TAVILY_SEARCH_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={"query": query, "max_results": count, "search_depth": "basic"},
        timeout=15,
    )
    resp.raise_for_status()
    body = resp.json()
    results = body.get("results", [])
    return [
        {
            "name": r.get("title"),
            "url": r.get("url"),
            "snippet": r.get("content"),
            "summary": None,
            "date_published": r.get("published_date"),
        }
        for r in results
    ]
