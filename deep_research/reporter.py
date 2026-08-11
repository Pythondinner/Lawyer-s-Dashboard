"""记者(Reporter):新模块,发生在 Multi-Agent 收集之后、笔杆子写作之前。

跟 Tool Use 循环那种"横向并行多个 Researcher"是不同方向的多智能体模式——这是"纵向接力":
记者先核实、整理、分类,笔杆子再各写各的板块。记者的职责是"核实+归类",不负责决定怎么呈现,
那是笔杆子的活。
"""

import json
import os

import openai
from openai import OpenAI

from context import today_str
from events import emit
from retry import with_retry

MAX_THEMES = 5

REPORTER_SYSTEM_PROMPT = f"""你是一个新闻编辑部的核实记者。给你一个研究问题,以及 Multi-Agent 小组搜集回来的
原始证据(每条有 source_id、status、extracted_claim)。

你的任务:
1. 只使用 status 为 "success" 的证据支撑事实,不要用 "irrelevant" 的。
2. 识别哪些证据其实在说同一件事——同一个事实被多个独立来源报道,合并成一条,source_ids 里列出所有
   支持它的编号,corroboration 标为 "multiple";只有一个来源支持的,corroboration 标为 "single"。
   不要把措辞不同但说的是同一件事的证据当成两条不同的事实。
3. 把合并后的事实按主题分组,最多 {MAX_THEMES} 个主题。主题要从证据内容里自然归纳出来,不要套用固定
   分类模板,也不要为了凑数硬拆——如果证据本来就少,归成 1 个主题完全可以,不要勉强分裂成好几个单薄主题。
   如果证据里有一套操作/流程类的事实,本质是"第一步做什么、第二步做什么"这种有先后顺序、可以照着
   执行的内容(比如菜谱、教程),优先把它们单独归成一个"步骤/做法"主题,不要拆散混进别的主题里——
   读者需要能看到一份完整、连贯的操作顺序,不是从好几段文字里自己拼凑步骤。
4. 写一个新闻标题(headline,不是问题复述,要有新闻感)和一段导语(lede,概括全文重点)。
5. 如果不同来源之间有实质性冲突,或者某方面证据明显不足,列进 preliminary_caveats,如实说明——包括
   "这个话题本身公开信息就有限"这种情况也要老实承认,不要用几句空话带过。

只输出如下 JSON,不要输出其他文字:
{{
  "headline": "...",
  "lede": "...",
  "themes": [
    {{"title": "主题名", "facts": [{{"claim": "...", "source_ids": ["s1", "s2"], "corroboration": "single 或 multiple"}}]}}
  ],
  "preliminary_caveats": [{{"issue": "...", "severity": "missing_data 或 conflict 或 stale", "source_id": "可选"}}]
}}
"""


@with_retry(max_retries=2, base_delay=1.5, exceptions=(openai.APIError,))
def _call_llm(question: str, evidence_for_llm: list[dict], concise: bool = False) -> dict:
    """DeepSeek 不显式设 max_tokens 时默认只给4000 tokens的输出空间,证据一多,记者要合并/分类/
    写headline+lede+caveats,很容易在生成到一半时被硬截断,导致返回的JSON不完整、解析报错。
    显式拉满到8000(deepseek-chat支持的上限)降低概率,但证据池足够大时依然可能不够,所以
    json.loads 失败时还有一层重试:明确要求模型这次写得更精简。
    """
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")

    content = (
        f"今天的日期是{today_str()}——判断信息是否'最新'、标注stale caveat时以这个日期为准。\n\n"
        f"研究问题: {question}\n\n证据列表:\n"
        f"{json.dumps(evidence_for_llm, ensure_ascii=False, indent=2)}"
    )
    if concise:
        content += (
            "\n\n注意:你上一次的输出因为太长被截断了,这次必须更精简——主题数量和每个主题下的"
            "事实条数适当减少,只保留最重要、最核心的内容,确保能在限定长度内完整写完,不要被截断。"
        )

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": REPORTER_SYSTEM_PROMPT},
            {"role": "user", "content": content},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=8000,
    )
    return json.loads(response.choices[0].message.content)


def consolidate(question: str, evidence_for_llm: list[dict], on_event=None) -> dict:
    try:
        result = _call_llm(question, evidence_for_llm)
    except json.JSONDecodeError:
        emit("[记者] 上一次输出过长被截断,要求精简后重试...", on_event)
        result = _call_llm(question, evidence_for_llm, concise=True)

    themes = result.get("themes", [])[:MAX_THEMES]
    result["themes"] = themes
    emit(f"[记者] 归纳出 {len(themes)} 个主题: {[t.get('title') for t in themes]}", on_event)
    return result
