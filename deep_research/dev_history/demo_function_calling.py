"""最小demo,只看 function calling 这一个动作,别的都先不管。
目的:亲眼看到"模型输出了什么"和"我们的代码做了什么"是两件分开的事。

[教学脚本,不是系统的一部分] 单纯用来讲清楚 function calling 机制,跟 agent.py 的真实实现
没有直接关系(agent.py 的循环逻辑更完整),留着是因为这是很好的"从零解释Tool Use"素材。
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.stdout.reconfigure(encoding="utf-8")
load_dotenv(PROJECT_ROOT / ".env")

client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")

SEARCH_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "在互联网上搜索信息",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                "required": ["query"],
            },
        },
    }
]

print("=== 第一步:把问题 + 一份「工具菜单」发给模型,看它想干嘛 ===\n")

response = client.chat.completions.create(
    model="deepseek-chat",
    messages=[{"role": "user", "content": "今日国际原油价格是多少"}],
    tools=SEARCH_TOOL,
)

msg = response.choices[0].message
tool_call = msg.tool_calls[0]

print("模型没有直接回答问题,而是返回了一个「调用请求」(纯文字,不是真的执行):")
print(f"    想调用的函数名   : {tool_call.function.name}")
print(f"    想传的参数(字符串): {tool_call.function.arguments}")
print("\n>>> 到这一步为止,没有任何真实的网络请求发生。模型只是「说」它想干什么。<<<\n")

print("=== 第二步:我们自己的代码,读懂这句话,真的去执行 ===\n")

args = json.loads(tool_call.function.arguments)
query = args["query"]
print(f"代码解析出 query = 「{query}」,现在真的调用 bocha_search() 发 HTTP 请求...\n")

from tools.search import bocha_search

results = bocha_search(query, count=2)
print(f"真实搜到了 {len(results)} 条结果,第一条标题: {results[0]['name']}\n")

print("=== 结论 ===")
print("模型自己完全没有能力上网、执行代码,它只能『请求』。")
print("真正执行这个请求的,是我们自己写的 Python 代码(agent.py 里的 if tool_call.function.name == 'search': ...)。")
print("这就是 function calling 的全部机制:模型负责判断『要不要调用、调用哪个、传什么参数』,")
print("执行权 100% 在我们自己的代码手里。")
