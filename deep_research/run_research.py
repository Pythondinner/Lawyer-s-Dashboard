"""完整链路入口:Planner → 并行Researcher(Multi-Agent收集) → 记者核实分类 → 并行笔杆子撰写 → 报告。

run_with_subqueries() 是"给定子问题列表,跑完整条后半段流程"的核心编排逻辑,被 run()(终端版,
反问走真实 input())和 app.py(网页版,反问走网页表单)共用。on_event 是可选的进度回调——
终端版不传,只走 print;网页版传一个把消息塞进队列的函数,驱动实时更新的状态面板。
"""

import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from dotenv import load_dotenv

sys.stdout.reconfigure(encoding="utf-8")
load_dotenv()

from agent import run_research_loop
from events import emit
from planner import plan
from storage import init_db
from synthesizer import synthesize_report

DEMO_QUESTION = "今日国际原油价格是多少"
MAX_CLARIFY_ROUNDS = 3  # 规则型硬上限:防止"回答完还是不清楚"反复循环,跟MAX_TOOL_CALLS等是同一种兜底


def resolve_sub_queries(question: str) -> list[str]:
    """终端专用:反问走真实的 input()。网页版走 app.py 里自己的表单逻辑,不走这个函数。
    之前这里假设"回答一次反问,Planner 就一定给出 sub_queries"——但用户回答本身也可能还是
    不够清楚,Planner 完全可能再次返回 needs_clarification,这时候直接取 result["sub_queries"]
    会 KeyError 崩掉。改成循环,最多问 MAX_CLARIFY_ROUNDS 轮,超过还没搞清楚就用原始问题兜底,
    不再假设"问一次就够"。
    """
    result = plan(question)
    rounds = 0

    while result.get("status") == "needs_clarification" and rounds < MAX_CLARIFY_ROUNDS:
        print(f"\n[Planner] 问题不够清楚,反问: {result['clarification_question']}")
        print("参考选项(不必照抄):")
        for opt in result.get("clarification_options", []):
            print(f"  - {opt}")
        answer = input("\n请输入你的回答: ")
        result = plan(question, clarification_answer=answer)
        rounds += 1

    return result.get("sub_queries") or [question]


def run_with_subqueries(question: str, sub_queries: list[str], on_event=None) -> dict:
    """Multi-Agent 检索 + 记者/笔杆子整合,不管子问题是终端反问出来的还是网页表单反问出来的,
    到这一步都一样处理。"""
    all_evidence = []
    per_researcher_seconds = []

    emit(f"[Researcher] {len(sub_queries)} 个 Researcher 并行检索中...", on_event)

    wall_clock_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=len(sub_queries)) as executor:
        future_to_meta = {
            executor.submit(
                run_research_loop,
                sq,
                memory_topic=question,
                log_prefix=f"[Researcher{i}/{len(sub_queries)}]",
            ): (i, sq)
            for i, sq in enumerate(sub_queries, start=1)
        }
        for future in as_completed(future_to_meta):
            i, sq = future_to_meta[future]
            loop_result = future.result()
            per_researcher_seconds.append(loop_result["elapsed_seconds"])
            emit(
                f"[Researcher] ✓ Researcher{i} 完成: {sq}  "
                f"(搜索{loop_result['tool_call_count']}轮,{loop_result['elapsed_seconds']}秒,"
                f"停止原因: {loop_result['stop_reason']})",
                on_event,
            )
            all_evidence.extend(loop_result["evidence"])
    wall_clock_seconds = round(time.monotonic() - wall_clock_start, 1)

    sequential_estimate = round(sum(per_researcher_seconds), 1)
    print(
        f"\n[并行效果] 实际耗时 {wall_clock_seconds} 秒;"
        f"如果依次顺序执行,预计要 {sequential_estimate} 秒(每个Researcher耗时相加)\n"
    )
    print(f"[汇总] 共 {len(all_evidence)} 条证据,交给记者+笔杆子\n")

    synth_result = synthesize_report(question, all_evidence, on_event=on_event)
    report = synth_result["report"]

    print(f"\n=== 校验(共尝试 {synth_result['attempts']} 轮) ===")
    if synth_result["unresolved_problems"]:
        print("  重试用尽,仍有以下问题未解决(如实展示,不悄悄放行):")
        for p in synth_result["unresolved_problems"]:
            print(f"  x {p}")
    else:
        print("  全部引用都能追溯到真实来源")

    print("\n=== 最终报告 ===")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    return {
        "report": report,
        "unresolved_problems": synth_result["unresolved_problems"],
        "attempts": synth_result["attempts"],
        "wall_clock_seconds": wall_clock_seconds,
        "sequential_estimate_seconds": sequential_estimate,
        "evidence_count": len(all_evidence),
    }


def run(question: str = DEMO_QUESTION) -> dict:
    init_db()
    print(f"研究问题: {question}\n")

    sub_queries = resolve_sub_queries(question)
    print(f"[Planner] 拆解出 {len(sub_queries)} 个子问题: {sub_queries}\n")

    return run_with_subqueries(question, sub_queries)


if __name__ == "__main__":
    run()
