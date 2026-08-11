"""稳定性机制:同一份材料独立跑N次(每次都是全新对话,互不干扰),
再让模型自己把N份报告合并成一份,标注每条发现是"跑几次都独立复现"还是"只出现过一次"。

背景:实测过同一份材料、同一套 prompt,跑三次,发现的具体内容不完全一样——有些发现
(比如炸药量反推)三次都稳定出现,有些(比如个别边角矛盾)只出现一次。这不代表"只出现一次"
就是错的,但"跑了几次都独立找到"这件事本身,是一个可以量化的置信度信号,应该让律师看得到,
不能假装每条发现都一样可信。"""

import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv
from openai import OpenAI

from analysis.deep_read import PRINCIPLE_CATEGORY_NAMES, SCOPE_LIMITATION_NOTICE
from analysis.deep_read_agentic import run_deep_read_agentic
from analysis.report_health import check_report, estimate_max_tokens, looks_corrupted

load_dotenv()

_client = None

CONSOLIDATE_PROMPT_TEMPLATE = """下面是同一份卷宗材料,用同一套分析方法,独立跑了{n}次得到的{n}份阅卷分析报告
(彼此之间没有互相参考,是完全独立生成的)。

你的任务:把这{n}份报告合并成一份最终报告,不是简单拼接,要做实质性的整合判断:

1. 同一个事实发现,如果在不同报告里用不同的措辞、甚至引用了不同的页码描述,但说的是同一件事,
要合并成一条,不要重复列出。
2. 每一条合并后的发现,必须在开头标注"[复现Х/{n}次]"——统计这条发现实际上在几份报告里独立
出现过。这是给律师的置信度信号,复现次数越高,这条发现越值得优先核实。
3. 保留原始报告里的【卷宗标签 第Y页】和原文摘句,如果同一条发现在不同报告里引用的页码不一样,
都列出来,不要凭自己判断哪个对,交给律师自己核对。
4. 分类结构维持原来的{n_categories}类不变。每一类内部,复现次数高的排在前面。
5. 不要因为整合报告就自己新增分析或者删减内容,你的任务是合并去重、标注复现次数,不是重新分析。

{n}份独立报告:

{reports}

请输出合并后的最终报告,格式跟原报告一致(按{n_categories}类分组,每条发现带页码+原文摘句+复现次数标注)。"""


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com", timeout=600.0)
    return _client


def _strip_notice(report: str) -> str:
    return report.replace(SCOPE_LIMITATION_NOTICE, "").strip()


def run_consensus(ledger_paths: list[str], n_runs: int = 3, max_tokens: int = 8000) -> tuple[str, dict, bool]:
    """独立跑 n_runs 次 + 合并成一份带复现次数标注的最终报告。返回 (报告文本, usage统计, 是否健康)。
    healthy 汇总两层:任意一次独立运行本身不健康(比如核验不完整退回了初稿),或者合并步骤本身
    不健康,只要有一层出问题,整体就判定为不健康——不能因为合并步骤顺利就掩盖了某次独立运行
    其实没跑好这件事。"""
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0}
    reports: list[str] = [None] * n_runs  # type: ignore[list-item]
    any_run_unhealthy = False

    print(f"独立跑{n_runs}次分析(并发)...")
    with ThreadPoolExecutor(max_workers=n_runs) as pool:
        futures = {pool.submit(run_deep_read_agentic, ledger_paths, max_tokens): i for i in range(n_runs)}
        for future in as_completed(futures):
            i = futures[future]
            report, usage, run_healthy = future.result()
            reports[i] = _strip_notice(report)
            total_usage["prompt_tokens"] += usage["prompt_tokens"]
            total_usage["completion_tokens"] += usage["completion_tokens"]
            if not run_healthy:
                any_run_unhealthy = True
            print(f"  第{i + 1}次运行完成(healthy={run_healthy})")

    print("合并{}份报告、标注复现次数...".format(n_runs))
    reports_block = "\n\n".join(f"===== 第{i + 1}次运行的报告 =====\n{r}" for i, r in enumerate(reports))
    prompt = CONSOLIDATE_PROMPT_TEMPLATE.format(n=n_runs, reports=reports_block, n_categories=len(PRINCIPLE_CATEGORY_NAMES))

    # 合并步骤要把 n_runs 份报告的发现原样保留、只是去重加标注,输出长度跟 n_runs 份报告的
    # 总长同一量级,固定给的 max_tokens 在报告条数多的时候可能不够——按参照文本长度动态估算。
    consolidate_max_tokens = estimate_max_tokens(reports_block, floor=max_tokens)
    client = _get_client()
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=consolidate_max_tokens,
    )
    total_usage["prompt_tokens"] += resp.usage.prompt_tokens
    total_usage["completion_tokens"] += resp.usage.completion_tokens

    consolidated, consolidate_healthy = check_report(resp.choices[0].message.content, resp.choices[0].finish_reason)
    if not consolidate_healthy and looks_corrupted(consolidated):
        # 损坏是模型采样偶发的噪声,换一次采样很可能就不再出现,不该一遇到就直接放弃"复现
        # 次数"这个置信度标注——重试一次(参数不变),还不行才真的退回未合并的拼接。
        print("  合并输出里混入了格式错乱的工具调用文本,重试一次(换一次采样)...")
        resp = client.chat.completions.create(
            model="deepseek-chat", messages=[{"role": "user", "content": prompt}], max_tokens=consolidate_max_tokens
        )
        total_usage["prompt_tokens"] += resp.usage.prompt_tokens
        total_usage["completion_tokens"] += resp.usage.completion_tokens
        consolidated, consolidate_healthy = check_report(resp.choices[0].message.content, resp.choices[0].finish_reason)

    if not consolidate_healthy:
        # 合并这一步没有真实工具可调用,如果输出夹带了工具调用格式的痕迹,说明这段输出不是
        # 真的合并报告,不能交付。跟"生成修正版"那一步不一样,这里没有单份"初稿"可以退回——
        # 退而求其次,直接把 n_runs 份原始报告未合并地拼接起来交付,不做"复现次数"标注,
        # 但至少每一条真实发现都还在,好过交付一份损坏的合并结果。
        print("  合并输出里混入了格式错乱的工具调用文本,重试后依然如此,判定为损坏,返回未合并的原始报告拼接")
        consolidated = (
            f"[系统提示:{n_runs}次独立运行结果的合并步骤失败,以下是未经去重/复现次数标注的原始拼接,"
            f"同一发现可能在多份报告中重复出现。]\n\n" + reports_block
        )
    return consolidated + SCOPE_LIMITATION_NOTICE, total_usage, consolidate_healthy and not any_run_unhealthy


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("ledger_paths", nargs="+")
    parser.add_argument("--n-runs", type=int, default=3)
    parser.add_argument("--out", default="analysis_consensus_result.txt")
    parser.add_argument("--max-tokens", type=int, default=8000)
    args = parser.parse_args()

    text, usage, healthy = run_consensus(args.ledger_paths, args.n_runs, args.max_tokens)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(text)
    print(f"[tokens] prompt={usage['prompt_tokens']} completion={usage['completion_tokens']}")
    print(f"结果写入 {args.out}(healthy={healthy})")
