"""v2 入口:跟 run_v1.py 的区别只有一处,但这处是质变——
要不要再搜、什么时候算够,不再是我们写的代码决定,是 DeepSeek 自己决定的。

[历史版本,留作演进记录,不是当前系统入口] 当前系统请跑项目根目录的 run_research.py 或 app.py。
这个版本只有 Tool Use 循环本身,还没接 Planner/Synthesizer/Multi-Agent。
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.stdout.reconfigure(encoding="utf-8")
load_dotenv(PROJECT_ROOT / ".env")

from agent import run_research_loop
from storage import init_db

DEMO_QUESTION = "今日国际原油价格是多少"


def run():
    init_db()
    print(f"研究问题: {DEMO_QUESTION}\n")

    result = run_research_loop(DEMO_QUESTION)

    print(f"\n本次 session_id: {result['session_id']}")
    print(f"共调用 search {result['tool_call_count']} 次")
    print(f"停止原因: {result['stop_reason']}")
    if result["stop_note"]:
        print(f"模型说明: {result['stop_note']}")

    print("\n=== 全部证据记录 ===")
    for e in result["evidence"]:
        print(e)


if __name__ == "__main__":
    run()
