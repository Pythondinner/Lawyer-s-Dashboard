"""起诉书事实核对——把检察院起诉书里的事实性表述,跟已经做好的事实还原报告并列摆出来。

设计原则(跟用户讨论确定的):
1. 起诉书必须作为**单独一份、明确标注的输入**,不依赖模型自己从一堆混合内容里识别"这段是不是
   起诉书"——这是"硬接口优于自然语言理解"这条原则的延伸:起诉书该由律师单独整理成一份PDF、
   单独摄取、打上明确的卷宗标签,交给这个函数的时候直接指定,不是让模型去猜。
2. 输出只做**纯并列**,不判断两者是否一致、不用"矛盾""建议""存在问题"这类措辞、不下任何结论——
   跟"事实还原不做主观定性判断"这条边界完全一致,只是把"起诉书怎么说"和"卷宗证据怎么说"平行
   摆出来,判断权完全在律师。
3. 这是**叠加**在已有事实还原报告之上的新产出,不是替换或者收窄——已有报告里那些起诉书没提到
   的疑点原样保留,不因为这个新功能就被筛掉,理由是检察院后续补充侦查很可能正好去查这些起诉书
   还没提的疑点,提前留着对律师有预判价值。"""

import json
import os
from datetime import datetime, timezone

from dotenv import load_dotenv
from openai import OpenAI

from analysis.report_health import (
    VERDICT_LANGUAGE_NOTE,
    check_report,
    estimate_max_tokens,
    has_verdict_language,
    looks_corrupted,
)
from ingestion.ledger import load_ledger

load_dotenv()

_client = None

INDICTMENT_COMPARISON_PROMPT = """下面是{doc_type}的原文,以及针对同一案子卷宗证据已经做好的事实还原报告。

你的任务:把{doc_type}里的关键事实性表述(涉及数量、金额、时间、经过等可以用证据核实的具体断言)
一条条摘出来,分别标注【{doc_type} 第X页】+原文摘句;再从已有的事实还原报告和卷宗原文里,找到跟
这条表述对应的证据内容,同样标注出处+原文摘句(如果报告里已经有独立验算过程,直接摘录验算
结果)。

两条并列列出即可,**不要评价两者是否一致、不要写"矛盾""不一致""建议""存在问题"这类措辞,
不要下任何结论**——你的任务只是把"{doc_type}怎么说"和"证据怎么说"这两件事平行摆出来,判断权
完全在律师。这条规则没有例外:哪怕是你自己在核对某份证据材料内部的数字(比如验算一张表格
自己的单价×数量对不对得上总价)时,也不要用"不一致""矛盾"这类词去描述计算结果——只中性地
把算出来的数字摆出来(比如"单价×数量算出来是16,000元,表格上写的总价是24,000元"),不评价
这两个数字是否对得上,同样交给律师自己判断。

如果{doc_type}某条表述在卷宗证据/事实还原报告里找不到对应内容,如实写"未在卷宗证据中找到直接
对应的核实材料",不要编。

格式示例:
【{doc_type} 第2页】被告人张某销售涉案物品共计11445件。
【第一卷 第81页】账单换算得出10377件。原文:"...10377件"

## {doc_type}原文:

{indictment}

## 已有的事实还原报告:

{report}
"""


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com", timeout=600.0)
    return _client


def parse_comparison_groups(comparison_text: str, doc_type: str = "起诉书") -> list[dict]:
    """把 run_indictment_comparison() 产出的文本按"{doc_type}"这一行分组——每组以一条
    {doc_type}表述开头,后面跟着若干条对应的证据引用,直到下一条{doc_type}表述出现为止。
    doc_type 要跟生成这段文本时用的 doc_type 一致(比如"起诉意见书"的对照文本里,分组标记
    是"【起诉意见书 ...】",不是"【起诉书 ...】")。indictment_docx.py(渲染Word表格)和
    web/app.py(渲染网页表格)都要用同一份分组逻辑,不要各写一份、容易在两处产生不一致的
    分组结果。"""
    marker = f"【{doc_type}"
    groups: list[dict] = []
    current: dict | None = None
    for raw_line in comparison_text.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(marker):
            if current is not None:
                groups.append(current)
            current = {"indictment": [line], "evidence": []}
        elif current is not None:
            current["evidence"].append(line)
    if current is not None:
        groups.append(current)
    return groups


def load_annotations(path: str) -> dict[int, dict]:
    """备注按对照组的序号(在 parse_comparison_groups() 返回列表里的下标)存,不改动
    对照文本本身——跟 verification_log.json 的设计是同一个原则(见 deep_dive_core.py):
    律师的判断单独存一层,不覆盖 Agent 生成的原文。"""
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    return {int(k): v for k, v in raw.items()}


def save_annotation(path: str, group_index: int, note: str) -> dict:
    annotations = load_annotations(path)
    entry = {"note": note, "timestamp": datetime.now(timezone.utc).isoformat()}
    annotations[group_index] = entry
    with open(path, "w", encoding="utf-8") as f:
        json.dump({str(k): v for k, v in annotations.items()}, f, ensure_ascii=False, indent=2)
    return entry


def run_indictment_comparison(
    indictment_ledger_path: str, report: str, doc_type: str = "起诉书", max_tokens: int = 8000
) -> tuple[str, dict, bool]:
    """indictment_ledger_path 是律师确认过的起诉书/起诉意见书台账(跟其他证据卷分开,不混在
    一起)。doc_type 是"起诉书"或"起诉意见书",决定 prompt 里怎么称呼这份文书、以及输出里
    每条表述标注成【起诉书 ...】还是【起诉意见书 ...】。report 是已经跑好的事实还原报告
    全文(不用重新读原始卷宗,复用已经做过的工作)。返回 (对照文本, usage统计, 是否健康)。

    这一步跟其他"一次性生成长文本"的步骤一样,会踩截断/输出损坏的坑,复用
    report_health.py 里已经验证过的动态预算估算+健康检查,不再各自硬编码一遍。另外单独
    加了"是否夹带下结论措辞"的检查——这是这个功能专属的边界,比其他报告更严格(不只是不做
    法律推理,连"对不对得上"这种事实层面的评价都不允许),prompt 里明确禁止过,实测下来
    模型不是100%守规矩,需要代码兜底提醒。"""
    entries = load_ledger(indictment_ledger_path)
    indictment_text = "\n\n".join(f"【第{e.page}页】\n{e.text}" for e in entries)

    prompt = INDICTMENT_COMPARISON_PROMPT.format(doc_type=doc_type, indictment=indictment_text, report=report)
    reference_text = indictment_text + report
    dynamic_max_tokens = estimate_max_tokens(reference_text, floor=max_tokens)

    client = _get_client()
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=dynamic_max_tokens,
    )
    usage = {"prompt_tokens": resp.usage.prompt_tokens, "completion_tokens": resp.usage.completion_tokens}
    content, healthy = check_report(resp.choices[0].message.content, resp.choices[0].finish_reason)

    if not healthy and not looks_corrupted(content) and dynamic_max_tokens < 64000:
        # 截断了但还没顶到封顶值,加大预算重试一次——跟 deep_read_agentic.py 修正版那一步
        # 用的是同一套思路。
        retry_max_tokens = min(dynamic_max_tokens * 2, 64000)
        resp = client.chat.completions.create(
            model="deepseek-chat", messages=[{"role": "user", "content": prompt}], max_tokens=retry_max_tokens
        )
        usage["prompt_tokens"] += resp.usage.prompt_tokens
        usage["completion_tokens"] += resp.usage.completion_tokens
        content, healthy = check_report(resp.choices[0].message.content, resp.choices[0].finish_reason)

    if has_verdict_language(content):
        content += VERDICT_LANGUAGE_NOTE
        healthy = False

    return content, usage, healthy


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="起诉书事实核对:把起诉书表述跟已有事实还原报告并列摆出来")
    parser.add_argument("indictment_ledger", help="起诉书/起诉意见书台账json路径")
    parser.add_argument("report_path", help="已有事实还原报告txt路径")
    parser.add_argument("--doc-type", default="起诉书", choices=["起诉书", "起诉意见书"])
    parser.add_argument("--out", default="indictment_comparison.txt")
    parser.add_argument("--max-tokens", type=int, default=8000)
    args = parser.parse_args()

    with open(args.report_path, "r", encoding="utf-8") as f:
        report_text = f.read()

    text, usage, healthy = run_indictment_comparison(args.indictment_ledger, report_text, args.doc_type, args.max_tokens)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[tokens] prompt={usage['prompt_tokens']} completion={usage['completion_tokens']}")
    print(f"结果写入 {args.out}(healthy={healthy})")
