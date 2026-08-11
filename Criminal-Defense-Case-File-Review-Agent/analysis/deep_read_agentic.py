"""阅卷分析层,升级版:不再是"写完就交卷",而是加了一道模型自己发起的工具调用。

流程:①模型照 deep_read.py 那套 prompt 先写一版初稿(跟之前一样)→②告诉模型"现在去核实
你刚才写的每一条引用",给它一个 verify_citations 工具,工具内部直接调用
verification/quote_check.py 里已经验证过的字符串匹配逻辑,不是又找一个模型来评判"像不像",
是精确查证据台账→③把核验结果原样喂回给模型,让它照着结果自己出一份修正版。

这跟 deep_research_agent 的 agent.py 是同一个"Tool Use 循环"模式,只是这次的工具从
"网页搜索"换成了"查证据台账",不需要额外框架,复用的是 OpenAI 兼容接口原生的
function calling 协议。"""

import json
import os
import re

from dotenv import load_dotenv
from openai import OpenAI

from analysis.deep_read import SCOPE_LIMITATION_NOTICE, SYSTEM_PROMPT, build_corpus
from analysis.report_health import check_report, estimate_max_tokens, looks_corrupted
from verification.quote_check import FINDING_RE, build_ledger_index, build_pages_by_volume, verify_one

load_dotenv()

_client = None

VERIFY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "verify_citations",
            "description": (
                "核对一批引用是否真的能在证据台账里找到对应内容。每条引用包括卷宗标签、页码、"
                "从原文逐字摘抄的一句话。这是精确的字符串查找,不是靠印象判断,结果可信。"
                "把你刚才写的报告里,每一条【卷宗标签 第Y页】+原文摘句,都作为一条 item 提交。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "volume": {"type": "string", "description": "卷宗标签,比如'卷2'"},
                                "page": {"type": "integer", "description": "你报告里写的页码"},
                                "quote": {"type": "string", "description": "你报告里那句逐字摘抄的原文"},
                            },
                            "required": ["volume", "page", "quote"],
                        },
                    }
                },
                "required": ["items"],
            },
        },
    }
]

REVISE_INSTRUCTION = """现在请调用 verify_citations 工具,把你刚才写的报告里,每一条引用(卷宗标签+页码+原文摘句)
都作为一条 item 提交上去核实,不要漏掉任何一条。

拿到核验结果之后,输出一份修正后的最终报告(完整重新输出,不是只列改动):
- verdict 是 matched 的,页码不用改
- verdict 是 found_on_different_page 的,把页码改成 actual_volume/actual_page 给出的正确出处,并且在括号里注明"经核验修正,原引第X页"
- verdict 是 not_found 的,不要删掉这条发现(内容本身可能是对的,只是引用位置没找到),但要把页码标注去掉,换成"(经核验,未能定位到确切页码,内容待人工核实原文)"

输出格式跟你刚才的报告保持一致,分类、结构都不变,只改引用部分。"""


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com", timeout=600.0)
    return _client


# 抠单条引用用的宽松正则——不要求整个字符串是合法 JSON,只找长得像
# {"volume": "...", "page": N, "quote": "..."} 的片段。用在 json.loads() 解析失败之后,
# 一条内容里出现没转义的引号会让严格解析从那个位置起整个失败,但正则可以跳过坏的那条,
# 继续在字符串别的地方找到其余写得对的条目——救回来的比"整批全部作废"多。
_ITEM_SALVAGE_RE = re.compile(
    r'\{\s*"volume"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"page"\s*:\s*(\d+)\s*,\s*"quote"\s*:\s*"((?:[^"\\]|\\.)*)"\s*\}'
)


def _parse_tool_call_items(raw_args: str) -> list[dict]:
    """解析核验工具调用的参数,失败时依次尝试几种兜底方式,不是一失败就整批放弃。"""
    try:
        return json.loads(raw_args).get("items", [])
    except json.JSONDecodeError:
        pass

    # 尝试补全常见的缺失收尾符号(比如生成到一半被截断,只差几个括号)
    for suffix in ('"}]}', '"}]', "}]}", "]}", "}"):
        try:
            return json.loads(raw_args + suffix).get("items", [])
        except json.JSONDecodeError:
            continue

    # 严格解析救不回来,退而求其次:正则逐条抠,能抢救多少条算多少条
    salvaged = []
    for m in _ITEM_SALVAGE_RE.finditer(raw_args):
        salvaged.append({"volume": m.group(1), "page": int(m.group(2)), "quote": m.group(3)})
    return salvaged


def _execute_verify_citations(items: list[dict], ledger_index: dict, pages_by_volume: dict) -> list[dict]:
    results = []
    for item in items:
        r = verify_one(item.get("quote", ""), item.get("volume", ""), item.get("page", 0), ledger_index, pages_by_volume)
        if r is None:
            results.append({"volume": item.get("volume"), "page": item.get("page"), "verdict": "skipped_empty_quote"})
            continue
        results.append(
            {
                "volume": r.volume,
                "page": r.page,
                "verdict": r.verdict,
                "actual_volume": r.actual_volume or r.volume,
                "actual_page": r.correct_page if r.correct_page else r.page,
            }
        )
    return results


def run_deep_read_agentic(
    ledger_paths: list[str], max_tokens: int = 8000, debug_verify_path: str | None = None
) -> tuple[str, dict, bool]:
    """返回 (最终修正版报告文本, usage统计, 是否健康)。usage 是三次调用(初稿+工具调用+修正版)
    加总。healthy=False 涵盖"核验不完整/工具调用失败退回初稿""输出损坏退回初稿""输出被截断"
    这几种情况——调用方(尤其是 cli/run_case.py 要写 manifest 那一层)不该只看报告文本里有没有
    嵌入提示文字才知道这次运行不完美,应该有一个能直接判断的结构化字段。"""
    corpus = build_corpus(ledger_paths)
    client = _get_client()
    ledger_index = build_ledger_index(ledger_paths)
    pages_by_volume = build_pages_by_volume(ledger_index)

    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": corpus},
    ]

    # 第一步:照常写初稿,不带工具
    print("  [1/3] 生成初稿...")
    resp = client.chat.completions.create(model="deepseek-chat", messages=messages, max_tokens=max_tokens)
    total_usage["prompt_tokens"] += resp.usage.prompt_tokens
    total_usage["completion_tokens"] += resp.usage.completion_tokens
    draft = resp.choices[0].message.content
    messages.append({"role": "assistant", "content": draft})

    # 第二步:要求模型自己调用核验工具。工具调用参数要把初稿里每一条引用都列成一个 item,
    # 条数越多参数越长——固定给8000之前只按"电子烟案~100条够用"这一次的经验值定的,没有
    # 真正跟引用条数挂钩,遇到更极端的案子还是有被截断的风险,改成动态估算,跟修正版那一步
    # 用同一个函数、同一个思路。
    print("  [2/3] 请模型调用核验工具...")
    messages.append({"role": "user", "content": REVISE_INSTRUCTION})
    tool_call_max_tokens = estimate_max_tokens(draft, floor=8000)
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=messages,
        tools=VERIFY_TOOL,
        tool_choice="required",
        max_tokens=tool_call_max_tokens,
    )
    total_usage["prompt_tokens"] += resp.usage.prompt_tokens
    total_usage["completion_tokens"] += resp.usage.completion_tokens
    msg = resp.choices[0].message

    if not msg.tool_calls:
        # 模型没有按要求调用工具,初稿就是能拿到的最好结果,不硬凑
        print("  模型没有调用工具,返回初稿")
        return draft + SCOPE_LIMITATION_NOTICE, total_usage, False

    tool_call = msg.tool_calls[0]
    raw_args = tool_call.function.arguments
    items = _parse_tool_call_items(raw_args)

    if not items:
        # 连正则抢救都没救回来任何一条,才是真的没辙——这种情况才值得把完整原始字符串存下来
        # 供事后分析(用 JSONDecodeError 的报错位置能直接定位到具体是哪个字符导致解析失败)。
        try:
            json.loads(raw_args)
        except json.JSONDecodeError as e:
            dump_path = f"scratch_json_failure_{len(raw_args)}.txt"
            with open(dump_path, "w", encoding="utf-8") as f:
                f.write(f"finish_reason={resp.choices[0].finish_reason}\n")
                f.write(f"error={e}\n")
                f.write(f"error_pos={e.pos}, lineno={e.lineno}, colno={e.colno}\n")
                f.write(f"字符总长={len(raw_args)}\n")
                f.write(f"报错位置前后各100字符: ...{raw_args[max(0, e.pos - 100):e.pos + 100]}...\n")
                f.write("\n===== 完整原始字符串 =====\n")
                f.write(raw_args)
            print(f"  [诊断] JSON解析和正则抢救都失败,详情已存 {dump_path}(错误位置字符={e.pos})")

    if not items:
        # 工具调用本身失败/被截断(比如引用条数太多、参数被截断解析不出来),不能硬着头皮往下走,
        # 那样容易把不完整的技术性输出当成"最终报告"存下来。宁可退回初稿,也不要交付一份坏报告。
        print("  工具调用没有拿到有效的引用列表,判定为核验失败,返回初稿")
        return draft + SCOPE_LIMITATION_NOTICE, total_usage, False

    draft_citation_count = len(FINDING_RE.findall(draft))
    if draft_citation_count >= 5 and len(items) < draft_citation_count * 0.5:
        # 遇到过模型把引用核验拆成"这次先提交一小部分,下次继续"——但工具调用协议里没有
        # "下一次"这回事,它想接着提交的内容只能以纯文本形式混进后面本该是正文的输出里,
        # 把整份报告写坏。与其等第三步产出坏报告才发现,不如在这里就按不完整核验处理,
        # 直接返回初稿。
        print(f"  模型只提交了{len(items)}条引用,但初稿里有约{draft_citation_count}条引用,判定为核验不完整,返回初稿")
        return draft + SCOPE_LIMITATION_NOTICE, total_usage, False

    print(f"  模型提交了{len(items)}条引用去核实...")
    verify_results = _execute_verify_citations(items, ledger_index, pages_by_volume)

    if debug_verify_path:
        # 存下模型自己提交核验的原始记录——比事后从最终报告里重新提取更可靠,
        # 因为最终修订版经常会把多条引用压缩合并,事后提取会漏掉一部分。
        with open(debug_verify_path, "w", encoding="utf-8") as f:
            json.dump(verify_results, f, ensure_ascii=False, indent=2)

    messages.append(
        {
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tool_call.id,
                    "type": "function",
                    "function": {"name": tool_call.function.name, "arguments": tool_call.function.arguments},
                }
            ],
        }
    )
    messages.append(
        {"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps(verify_results, ensure_ascii=False)}
    )

    # 第三步:模型看着核验结果,出修正版。修正版要把初稿的全部内容原样保留再逐条改引用,
    # 长度跟初稿差不多甚至更长——重跑电子烟案(~100条引用)时实测到:光按初稿字数估算的
    # 固定余量(+4000)不够用,因为膨胀幅度主要跟引用条数挂钩(每条都可能被加上"经核验修正,
    # 原引第X页"或者"未能定位到确切页码"这类批注),不是单纯跟初稿长度成正比,原来的估算
    # 公式漏了这个变量。这里把余量改成随条数一起放大。
    revise_max_tokens = estimate_max_tokens(draft, floor=max_tokens, margin=4000 + len(items) * 100)
    print(f"  [3/3] 生成修正版...(max_tokens={revise_max_tokens})")
    resp = client.chat.completions.create(model="deepseek-chat", messages=messages, max_tokens=revise_max_tokens)
    total_usage["prompt_tokens"] += resp.usage.prompt_tokens
    total_usage["completion_tokens"] += resp.usage.completion_tokens
    final_report, healthy = check_report(resp.choices[0].message.content, resp.choices[0].finish_reason)

    # 截断了,但还没顶到64000这个封顶值——说明还有加大预算重跑的空间,不用马上认输。原来只
    # 重试一次,实测过电子烟案有一次翻倍到39458还是不够,改成循环重试,直到健康、或者顶到
    # 64000封顶值、或者重试次数到上限(留一个硬上限防止意外情况下无限重试烧钱)。乱码那种
    # 情况不重试(重试不会让乱码消失)。
    retry_count = 0
    while not healthy and not looks_corrupted(final_report) and revise_max_tokens < 64000 and retry_count < 3:
        retry_count += 1
        revise_max_tokens = min(revise_max_tokens * 2, 64000)
        print(f"  修正版输出被截断,加大预算重试(第{retry_count}次)...(max_tokens={revise_max_tokens})")
        resp = client.chat.completions.create(model="deepseek-chat", messages=messages, max_tokens=revise_max_tokens)
        total_usage["prompt_tokens"] += resp.usage.prompt_tokens
        total_usage["completion_tokens"] += resp.usage.completion_tokens
        final_report, healthy = check_report(resp.choices[0].message.content, resp.choices[0].finish_reason)

    if not healthy and looks_corrupted(final_report):
        # 损坏(混入工具调用格式痕迹)是模型采样时偶发的噪声,不是这份输入必然触发的确定性
        # 问题——同样的请求换一次采样,很可能就不再出现,原来"损坏就不重试"的判断没有真的
        # 验证过、大概率过于保守。重试一次(参数不变,纯粹是换一次生成),还不行才真的放弃。
        print("  修正版输出里混入了格式错乱的工具调用文本,重试一次(换一次采样)...")
        resp = client.chat.completions.create(model="deepseek-chat", messages=messages, max_tokens=revise_max_tokens)
        total_usage["prompt_tokens"] += resp.usage.prompt_tokens
        total_usage["completion_tokens"] += resp.usage.completion_tokens
        final_report, healthy = check_report(resp.choices[0].message.content, resp.choices[0].finish_reason)

    if not healthy:
        if looks_corrupted(final_report):
            # 修正版这一步没有真实工具可调用,模型却输出了工具调用格式的文本,说明这段输出
            # 不是真的报告正文,不能交付。核验结果本身(verify_results/debug_verify_path)依然
            # 有效,只是"模型照着结果重写报告"这一步失败了——退回初稿,不硬凑。
            print("  修正版输出里混入了格式错乱的工具调用文本,重试后依然如此,判定为损坏,返回初稿")
            return draft + SCOPE_LIMITATION_NOTICE, total_usage, False
        print("  警告:修正版输出被截断(finish_reason=length),重试后依然不够,已在报告末尾标注")

    return final_report + SCOPE_LIMITATION_NOTICE, total_usage, healthy


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("ledger_paths", nargs="+")
    parser.add_argument("--out", default="analysis_agentic_result.txt")
    parser.add_argument("--max-tokens", type=int, default=8000)
    args = parser.parse_args()

    text, usage, healthy = run_deep_read_agentic(args.ledger_paths, args.max_tokens)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[tokens] prompt={usage['prompt_tokens']} completion={usage['completion_tokens']}")
    print(f"结果写入 {args.out}(healthy={healthy})")
