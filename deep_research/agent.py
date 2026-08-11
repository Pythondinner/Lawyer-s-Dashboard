"""v2: 真正的 Tool Use 循环 —— "要不要再搜一轮"这个决策,交给 DeepSeek 通过
function calling 自主判断,不再是 v1 里写死的顺序代码。

跟之前设计的"规则型停止条件"不冲突,是升级关系:判断"证据够不够"这件事,
从写死的阈值(>=2条success)交给 LLM 自己判断,更接近真实的 Planning。
唯一保留的规则型控制是硬性步数上限(MAX_TOOL_CALLS)——这不是"够不够"的判断,
是纯粹的稳定性兜底,防止 LLM 判断失误导致无限循环,所以继续用代码写死,不交给 LLM。
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone

import openai
from openai import OpenAI

from context import today_str
from retry import with_retry
from storage import insert_evidence
from tools.extract import extract_claim
from tools.search import tavily_search

MAX_TOOL_CALLS = 6

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search",
            "description": "在互联网上搜索信息,自动抽取跟研究问题相关的事实并返回结果摘要",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词,可以跟研究问题原文不同,比如换个角度、加限定词、加时间范围",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "当已收集到足够证据、或反复尝试后确认证据不足时调用,结束本次研究",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "enum": ["sufficient_evidence", "insufficient_evidence"],
                        "description": "sufficient_evidence=证据足够;insufficient_evidence=多次尝试后仍不足",
                    },
                    "note": {"type": "string", "description": "简短说明做出这个判断的依据"},
                },
                "required": ["reason"],
            },
        },
    },
]

SYSTEM_PROMPT = """你是一个研究助手,任务是针对研究问题反复调用 search 工具收集证据,直到能给出可靠结论,再调用 finish 结束。

判断"证据是否足够"时可以参考(不是硬性规则,自己判断):
- 至少有 2 条独立来源、状态为 success 的证据,内容能互相印证,通常算足够
- 如果不同来源的数据互相矛盾,可以换个关键词再搜一次试图厘清,而不是直接采信其中一个
- 如果连续搜索都找不到相关信息,不要无限重试,调用 finish 并如实说明证据不足

每一步你必须调用 search 或 finish 中的一个工具,不要只输出文字。
"""


def run_research_loop(question: str, memory_topic: str | None = None, log_prefix: str = "") -> dict:
    """question 是这次循环真正要研究的问题(可能是 Planner 拆出来的子问题)。
    memory_topic 是长期记忆里用来分组的"原始问题"标签,不传就等于 question——
    独立调用(run_v1.py/run_v2.py)时行为不变;被 run_research.py 按子问题调用时,
    传入原始问题,让同一次研究里所有子问题的证据都能按同一个 question 分组、跨 session 查询到。
    log_prefix 是 Multi-Agent 版本里,多个 Researcher 线程并发打印时用来区分"这行是哪个子问题打的",
    避免几个线程的输出交错在一起看花眼;单独跑的时候不传,默认空字符串,输出跟以前一样。
    """
    memory_topic = memory_topic or question
    session_id = uuid.uuid4().hex[:8]
    start_time = time.monotonic()
    client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com")

    @with_retry(max_retries=2, base_delay=1.5, exceptions=(openai.APIError,))
    def call_model(msgs):
        return client.chat.completions.create(
            model="deepseek-chat",
            messages=msgs,
            tools=TOOLS,
            tool_choice="auto",
            max_tokens=2000,
        )

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"今天的日期是{today_str()}——搜索关键词、判断信息新旧时以这个日期为准,不要凭训练数据默认的年份猜。\n\n研究问题: {question}",
        },
    ]

    all_evidence = []
    tool_call_count = 0
    stop_reason = None
    stop_note = None

    while True:
        response = call_model(messages)
        msg = response.choices[0].message

        if not msg.tool_calls:
            # 模型没按要求调用工具,直接把它当成隐式的 finish,不让循环卡死
            stop_reason = "model_responded_without_tool_call"
            stop_note = msg.content
            break

        tool_call = msg.tool_calls[0]
        args = json.loads(tool_call.function.arguments or "{}")

        messages.append(
            {
                "role": "assistant",
                "content": msg.content,
                "tool_calls": [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.function.name,
                            "arguments": tool_call.function.arguments,
                        },
                    }
                ],
            }
        )

        if tool_call.function.name == "finish":
            stop_reason = args.get("reason", "unspecified")
            stop_note = args.get("note")
            print(f"{log_prefix}  [判断] 停止研究,原因: {stop_reason}" + (f" —— {stop_note}" if stop_note else ""))
            break

        # tool_call.function.name == "search"
        tool_call_count += 1
        query = args.get("query", question)
        print(f"{log_prefix}  [轮次{tool_call_count}] LLM决定搜索:「{query}」")

        results = tavily_search(query)
        round_summary = []
        for page in results:
            extraction = extract_claim(question, page)
            status = "success" if extraction.get("relevant") else "irrelevant"

            claim_preview = extraction.get("claim")
            preview_text = f" | {claim_preview[:50]}" if status == "success" and claim_preview else ""
            print(f"{log_prefix}    [筛选] {str(page.get('name'))[:35]} -> {status}{preview_text}")

            retrieved_at = datetime.now(timezone.utc).isoformat()
            source_id = insert_evidence(
                session_id=session_id,
                question=memory_topic,
                for_query=query,
                url=page.get("url"),
                name=page.get("name"),
                retrieved_at=retrieved_at,
                status=status,
                extracted_claim=extraction.get("claim"),
            )
            entry = {
                "source_id": source_id,
                "name": page.get("name"),
                "status": status,
                "extracted_claim": extraction.get("claim"),
            }
            round_summary.append(entry)
            all_evidence.append({**entry, "url": page.get("url"), "for_query": query})

        messages.append(
            {
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": json.dumps(round_summary, ensure_ascii=False),
            }
        )

        if tool_call_count >= MAX_TOOL_CALLS:
            stop_reason = "forced_stop_max_tool_calls"
            stop_note = f"已达到硬性上限 {MAX_TOOL_CALLS} 次工具调用,代码强制停止(不是模型自己决定的)"
            print(f"{log_prefix}  [判断] {stop_note}")
            break

    return {
        "session_id": session_id,
        "question": question,
        "evidence": all_evidence,
        "tool_call_count": tool_call_count,
        "stop_reason": stop_reason,
        "stop_note": stop_note,
        "elapsed_seconds": round(time.monotonic() - start_time, 1),
    }
