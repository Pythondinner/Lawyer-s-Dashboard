import json
import os

import openai
from openai import OpenAI

from context import today_str
from retry import with_retry

_client = None

EXTRACT_SYSTEM_PROMPT = """你是一个信息提取助手。给你一个研究问题和一段网页内容(标题、来源URL、发布日期、摘要、内容总结),判断这段内容能不能回答这个问题。

- 如果能回答,提取出跟问题直接相关的具体事实,尽量包含具体数值、日期等关键信息,不要加你自己的推测。
- 发布日期和URL是辅助线索,不是唯一依据——结合正文内容一起判断时效性和相关性
  (比如正文讲的是历史事件,不能因为"发布日期是最近"就当成最新动态)。
- 如果不能回答(内容跟问题无关,或者信息不够具体到能支撑一个结论),明确说明不相关,不要硬凑。

只输出如下 JSON 格式,不要输出任何其他文字:
{"relevant": true 或 false, "claim": "提取出的具体事实,如果 relevant 为 false 则为 null"}
"""


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")
    return _client


@with_retry(max_retries=2, base_delay=1.5, exceptions=(openai.APIError,))
def extract_claim(question: str, page: dict) -> dict:
    """这一步是纯 LLM 推理,不是 Tool Use——加工的是 search 工具已经拿回来的结果。"""
    content_for_llm = (
        f"标题: {page.get('name')}\n"
        f"来源URL: {page.get('url')}\n"
        f"发布日期: {page.get('date_published')}\n"
        f"摘要: {page.get('snippet')}\n"
        f"内容总结: {page.get('summary')}"
    )
    client = _get_client()
    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"今天的日期是{today_str()}。\n\n研究问题: {question}\n\n网页内容:\n{content_for_llm}",
            },
        ],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=1000,
    )
    return json.loads(response.choices[0].message.content)
