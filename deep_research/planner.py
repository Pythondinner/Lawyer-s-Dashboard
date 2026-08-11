"""Planner:新模块,发生在 Tool Use 循环之前。

这次 LLM 调用刻意不带 tools 参数——它只做"问题够不够清楚 + 怎么拆解"这个纯推理判断,
不涉及"要调用哪个工具"。跟 agent.py 里的 Tool Use 循环是两次完全独立的调用,
Planner 交卷之后,由我们自己的编排代码把它的输出喂给 Tool Use 循环,不是同一次推理的延续。
"""

import json
import os

import openai
from openai import OpenAI

from context import today_str
from retry import with_retry

MAX_SUB_QUERIES = 3

PLANNER_SYSTEM_PROMPT = """你是一个研究助手的规划模块。给你一个用户提出的研究问题,你要做以下两件事之一:

1. 如果问题已经足够具体、清楚,能直接据此去搜索,就把它拆解成 1~3 个具体的搜索子问题
   (如果问题本身已经很聚焦,拆成 1 个也可以,不用为了拆而拆)。

2. 如果问题太模糊、有歧义,没法直接搜索(比如"帮我看看球队数据"——不知道哪支球队、什么数据),
   不要瞎猜,提出一个反问,并给出 2~4 个具体的例子选项帮用户更容易回答
   (即使用户最后不按选项回答也没关系,给例子是为了降低回答门槛,不是限定死答案)。

只输出如下 JSON,不要输出其他文字。

问题清楚时:
{"status": "ready", "sub_queries": ["...", "..."]}

问题模糊时:
{"status": "needs_clarification", "clarification_question": "...", "clarification_options": ["...", "...", "..."]}
"""


@with_retry(max_retries=2, base_delay=1.5, exceptions=(openai.APIError,))
def plan(question: str, clarification_answer: str | None = None) -> dict:
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")

    user_content = f"今天的日期是{today_str()}——拆解搜索子问题时,以这个日期判断'最新/近期'指的是哪年,不要凭自己训练数据默认的年份猜。\n\n研究问题: {question}"
    if clarification_answer:
        user_content += f"\n\n(这是用户对上一轮反问的回答: {clarification_answer})"

    response = client.chat.completions.create(
        model="deepseek-chat",
        messages=[
            {"role": "system", "content": PLANNER_SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        response_format={"type": "json_object"},
        temperature=0,
        max_tokens=1500,
    )
    result = json.loads(response.choices[0].message.content)

    if result.get("status") == "ready":
        # 规则型硬上限:prompt 里只是软性建议"1~3个",这里代码强制截断,不完全指望模型自觉
        result["sub_queries"] = result.get("sub_queries", [])[:MAX_SUB_QUERIES]

    return result
