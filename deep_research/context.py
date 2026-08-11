"""共享的运行时上下文——目前只有"今天是哪天"这一件事。

很多个 LLM 调用(Planner 拆子问题、Tool Use 循环判断证据是否够新、extract_claim 判断
时效性、记者标注 stale caveat)都需要一个真实的"现在"作为基准。不注入的话,模型只能
按自己训练数据里的默认年份猜"现在是哪年",猜出来的年份跟实际当前日期对不上,
就会生成过时年份的搜索词、或者误判信息新旧——这不是模型不智能,是压根没给它这个信息。
"""

from datetime import datetime


def today_str() -> str:
    return datetime.now().strftime("%Y年%m月%d日")
