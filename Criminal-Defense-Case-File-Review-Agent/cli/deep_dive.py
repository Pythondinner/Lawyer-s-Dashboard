"""Agent-2 终端版:人机协同深挖卷宗内容。核心逻辑在 analysis/deep_dive_core.py,
网页版(web/app.py)复用同一份,这里只是一层终端交互的薄封装(读键盘、打印)。

跟 Agent-1(deep_read_agentic.py)是同一个 function calling 基础设施,但交互模式不一样:
Agent-1 是一次性批量生成+自我核验的单向流程;这个是多轮、开放式的对话循环,律师读完 Agent-1
的报告之后,想追问哪条就追问哪条,不追问的可以完全跳过——不是引导式的固定问答流程。"""

import sys

from analysis.deep_dive_core import DeepDiveCase, confirm_finding, process_turn

# Windows 下 stdin/stdout 默认走系统控制台代码页(通常不是 UTF-8),管道输入中文文本时
# 会解码出没法再编码回 UTF-8 的代理字符,导致写日志文件时崩溃。显式强制 UTF-8,不依赖
# 系统区域设置——这不是"管道输入"独有的问题,交互式终端如果代码页不对也会踩到同一个坑。
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def run_deep_dive(case_dir: str) -> None:
    case = DeepDiveCase(case_dir)
    history = case.load_history()

    if history:
        print(f"读取到{len(history)}轮历史对话,继续之前的深挖。")
    print(f"===== 深挖案子:{case.manifest['case_name']} =====")
    print("直接输入问题,输入 exit 或 quit 结束。\n")

    while True:
        try:
            user_input = input("律师> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n结束。")
            break
        if user_input.lower() in ("exit", "quit"):
            print("结束。")
            break
        if not user_input:
            continue

        reply, tool_events, pending_proposals = process_turn(case, history, user_input)
        for event in tool_events:
            print(f"  [{event}]")
        print(f"\n助手> {reply}\n")

        # 硬接口:终端没有按钮,退化成固定指令——"是不是要确认"这件事由精确的字符匹配判断,
        # 不是模型判断,也不是靠解析这一行输入的语气。
        for proposal in pending_proposals:
            print(f"  [待确认] 【{proposal['volume']} 第{proposal['page']}页】{proposal['finding']}")
            if proposal.get("note"):
                print(f"           备注: {proposal['note']}")
            try:
                answer = input("           输入 y 确认 / n 推翻 / 其他任意内容跳过: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = ""
            if answer == "y":
                confirm_finding(case, proposal, "confirmed")
                print("           已记录为:confirmed")
            elif answer == "n":
                confirm_finding(case, proposal, "overturned")
                print("           已记录为:overturned")
            else:
                print("           已跳过,未记录")

        history = history + [{"role": "user", "content": user_input}, {"role": "assistant", "content": reply}]


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Agent-2 终端版:针对一个已跑完 Agent-1 的案子做人机协同深挖")
    parser.add_argument("case_dir", help="案子目录,比如 data/cases/非法采矿案")
    args = parser.parse_args()

    run_deep_dive(args.case_dir)
