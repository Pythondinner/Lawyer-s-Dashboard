"""演示 Planner 的两种情况:问题够清楚直接拆解 / 问题模糊触发反问。

[历史版本,留作演进记录,不是当前系统入口] 当前系统请跑项目根目录的 run_research.py 或 app.py,
反问机制现在已经接进主链路,不需要单独跑这个文件看效果——app.py 网页版里反问走的是网页表单,
run_research.py 终端版走的是真实 input(),都比这里更完整。

第二个案例里的"用户回答"先写死模拟,方便非交互环境下跑通。
"""

import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.stdout.reconfigure(encoding="utf-8")
load_dotenv(PROJECT_ROOT / ".env")

from planner import plan

SIMULATED_ANSWER = "皇马最近的比赛战绩"

print("=== 案例1: 问题够清楚,直接拆解 ===\n")
result = plan("今日国际原油价格是多少")
print(result)

print("\n\n=== 案例2: 问题模糊,触发反问 ===\n")
result = plan("帮我看看球队数据")
print(result)

if result.get("status") == "needs_clarification":
    print(f"\n模型反问: {result['clarification_question']}")
    print("给出的参考选项:")
    for opt in result.get("clarification_options", []):
        print(f"  - {opt}")

    print(f"\n(模拟用户回答,不按选项来): {SIMULATED_ANSWER}")

    print("\n=== 拿到回答后,重新规划 ===\n")
    result2 = plan("帮我看看球队数据", clarification_answer=SIMULATED_ANSWER)
    print(result2)
