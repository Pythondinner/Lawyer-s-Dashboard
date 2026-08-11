"""把卷宗里明确写出来的日期事件按时间顺序重排——不是新的分析,是同一份台账、同样已经验证过
可以引用的事实,换一个排序维度(按时间,不是按9条原则分类)。

设计边界(讨论后定的,故意收得很紧):
1. 只摘录材料里写明的具体日期(能对应到日历上某一天的),不摘录、不推断模糊/相对时间
   ("案发前""几天后"这种),宁可漏掉一条模糊的,也不猜一个具体日期出来。
2. 不同材料对同一个日期记载的内容不一样甚至矛盾,原样各自列出来,绝不合并成一句话、
   也不判断哪个对——矛盾在时间线上会自然表现成同一天挂着好几条互相打架的记录,这正是
   要给律师看的东西,系统不能替律师选一个"更可信"的版本先讲。
3. 不写任何叙事性衔接词("于是""随后""这导致")。每条都是独立的"【日期】【出处】材料记了
   什么",条与条之间不建立因果关系——因果关系的判断权在律师,不在这一步。

最终的时间顺序排列是纯 Python 按日期字符串排序,不是模型决定的——保证"谁先谁后"这件事
完全是机械事实,不掺入任何模型的编排判断。"""

import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from openai import OpenAI

from analysis.batch_analysis import plan_batches
from analysis.deep_read import build_corpus
from analysis.report_health import estimate_max_tokens

load_dotenv()

_client = None
DEFAULT_TOKEN_BUDGET = 150_000

TIMELINE_EXTRACTION_PROMPT = """下面是卷宗材料原文(带【卷宗标签 第Y页】标注)。

你的任务:找出材料里所有**明确写出具体日期**(能精确到年月日、可以对应到日历上某一天)的
内容,摘录"这份材料在这个日期记载了什么事实",标注出处和原文摘句。

严格规则:
1. 只摘录精确到"年月日"的日期。像"案发前""几天后""上个月"这种模糊/相对表述,不要摘,
   也不要自己去推算出一个具体日期——没写清楚就不摘,不要猜。
2. 同一天如果不同材料记载的内容不一样、甚至矛盾,分别作为独立条目列出来,不要合并、
   不要判断哪个对——原样并列摆出来就行。
3. 不要添加任何因果关系或者叙事性衔接词，每条只是"这份材料写了什么"，不要写"因此""随后"
   这类连接词，也不要把多条内容合并成一段叙事。
4. summary 只做客观转述，不要加"矛盾""不一致""值得注意"这类评价性措辞。

输出JSON格式：
{{"entries": [
  {{"date": "2024-11-29", "date_raw": "2024年11月29日", "citation": "卷1 第8页",
    "summary": "简要转述这条记载的内容", "quote": "原文摘句"}}
]}}
date 字段必须是标准"YYYY-MM-DD"格式且真实对应材料里写出的日期；如果材料只写了年月、
没写具体日，这条不要摘录进去。找不到任何符合条件的日期就返回 {{"entries": []}}。

## 材料原文：

{corpus}
"""


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com", timeout=600.0)
    return _client


def _repair_json(content: str) -> dict:
    content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    if not content.endswith("}"):
        try:
            return json.loads(content + "}")
        except json.JSONDecodeError:
            pass
        if not content.endswith("]}"):
            try:
                return json.loads(content + "]}")
            except json.JSONDecodeError:
                pass
    raise ValueError("无法修复的 JSON: " + content[:200])


def _is_valid_date(date_str: str) -> bool:
    if not isinstance(date_str, str) or len(date_str) != 10:
        return False
    try:
        year, month, day = date_str.split("-")
        return len(year) == 4 and len(month) == 2 and len(day) == 2 and 1 <= int(month) <= 12 and 1 <= int(day) <= 31
    except (ValueError, AttributeError):
        return False


def _extract_batch(ledger_paths: list[str], max_tokens: int) -> tuple[list[dict], dict]:
    """处理一批卷宗,返回(条目列表, usage统计)。解析失败不重试、不报错——直接跳过这一批,
    时间线本来就是"锦上添花"的辅助视角,不应该因为一次解析失败就让整个流程中断。"""
    corpus = build_corpus(ledger_paths)
    prompt = TIMELINE_EXTRACTION_PROMPT.format(corpus=corpus)
    dynamic_max_tokens = estimate_max_tokens(corpus, floor=max_tokens)

    client = _get_client()
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=dynamic_max_tokens,
        response_format={"type": "json_object"},
    )
    usage = {"prompt_tokens": resp.usage.prompt_tokens, "completion_tokens": resp.usage.completion_tokens}
    content = resp.choices[0].message.content

    try:
        parsed = _repair_json(content)
    except ValueError:
        print("  时间线提取:这一批JSON解析失败,跳过(不影响其他批次)")
        return [], usage

    entries = [e for e in parsed.get("entries", []) if _is_valid_date(e.get("date"))]
    return entries, usage


def run_timeline(ledger_paths: list[str], token_budget: int = DEFAULT_TOKEN_BUDGET, max_tokens: int = 8000) -> tuple[str, dict, bool]:
    """按卷分批提取(不拆单卷,复用 batch_analysis.plan_batches 同一套装箱逻辑)、并发跑,
    最后合并——合并这一步是纯 Python 按日期排序,不再过一次模型,不会有"整合步骤输出损坏"
    这类风险,这也是这个功能比主报告结构上更简单、更不容易出问题的地方。

    返回 (时间线文本, usage统计, 是否健康)。healthy 目前只反映"有没有任何一批彻底提取失败",
    单批失败不算整体不健康(容忍度比主报告更高,因为漏一批时间线条目的后果,远比主报告漏了
    一条核查发现更轻)。"""
    batches = plan_batches(ledger_paths, token_budget)
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    all_entries: list[dict] = []
    any_batch_failed = False

    print(f"提取时间线({len(batches)}批,并发)...")
    with ThreadPoolExecutor(max_workers=len(batches)) as pool:
        futures = {pool.submit(_extract_batch, batch, max_tokens): i for i, batch in enumerate(batches)}
        for future in as_completed(futures):
            entries, usage = future.result()
            total_usage["prompt_tokens"] += usage["prompt_tokens"]
            total_usage["completion_tokens"] += usage["completion_tokens"]
            if not entries and len(batches) > 1:
                any_batch_failed = True
            all_entries.extend(entries)

    all_entries.sort(key=lambda e: (e["date"], e["citation"]))

    if not all_entries:
        return "本案材料中未提取到任何精确到年月日的日期记载,无法生成时间线。", total_usage, not any_batch_failed

    lines = [
        "# 卷宗时间线(按日期重排,不是新的分析)",
        "",
        "> 说明:本时间线只摘录材料中明确写出的具体日期,不推断模糊时间;同一天如果不同材料"
        "记载不一样甚至矛盾,原样并列列出,不做取舍、不做判断,判断权在律师。",
        "",
    ]
    current_date = None
    for e in all_entries:
        if e["date"] != current_date:
            current_date = e["date"]
            # 标题栏统一用规整的"YYYY年M月D日",不用某一条摘录里恰好带出来的原始日期文字——
            # 不同文书写同一天的方式不一样(有的带具体时分,有的只写日期),混着用会显得不统一。
            year, month, day = current_date.split("-")
            lines.append(f"\n## {int(year)}年{int(month)}月{int(day)}日")
        lines.append(f"\n**【{e['citation']}】** {e['summary']}\n原文:\"{e['quote']}\"")

    return "\n".join(lines), total_usage, not any_batch_failed


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="按时间顺序重排卷宗里的日期事件")
    parser.add_argument("ledger_paths", nargs="+")
    parser.add_argument("--out", default="timeline.txt")
    args = parser.parse_args()

    text, usage, healthy = run_timeline(args.ledger_paths)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[tokens] prompt={usage['prompt_tokens']} completion={usage['completion_tokens']}")
    print(f"结果写入 {args.out}(healthy={healthy})")
