"""统一处理"模型这一步的输出到底能不能当成交付结果"这件事。

背景:deep_read_agentic.py 的"生成修正版"这一步先后暴露过两个问题——模型把工具调用格式的
文本当正文吐出来(乱码),以及固定 max_tokens 不够把报告从中间截断。这两类问题其实不是那一
步独有的:consensus.py 的多次运行合并步骤、batch_analysis.py 的跨批整合步骤,都是同样的
"一次性生成一份长报告"结构,同样会踩到。之前只在一处修,另外两处还是各自的旧写法、各漏各的,
这次把检查逻辑收成一处,三个"最终整合"步骤统一过一遍。

第三类检查(法条引用检测)是不同性质的问题:不是输出机制故障,是这个系统最核心的边界——
"只在卷宗内部做事实推理,不做法律映射"——一旦被突破。这条边界只写在 prompt 里不够可靠
(prompt 能约束大部分情况,但不能保证100%),跟其他两类检查一样,不能只靠模型"自觉遵守",
要有代码去验证边界有没有被实际守住。"""

import re

# 模型在没有真实工具可调用的步骤里,偶尔会自己"模拟"出一段工具调用格式的文本当正文输出——
# 不是真的 API 级工具调用,一旦出现说明这一轮输出本身不可信,要整段判废,不能只清洗掉这几行。
CORRUPTION_MARKERS = ("｜｜DSML｜｜", "<｜tool_calls", "tool_calls>", "invoke name=")

# 只抓"《XX法/解释》第X条"这种规范的法条引用格式——精确度高,不会跟卷宗里的人名地名、
# 案由名称(比如"非法采矿罪")混淆,漏报比误报更能接受(漏掉的还有律师自己审这道关)。
LEGAL_CITATION_RE = re.compile(r"《[^》]{2,30}》\s*第[一二三四五六七八九十百千0-9]+条")

TRUNCATION_NOTE = (
    "\n\n[系统提示:本报告在生成这一步被输出长度上限截断,以上内容不完整,"
    "需要重新以更大的 max_tokens 运行。]"
)

LEGAL_CITATION_NOTE = (
    "\n\n[系统提示:本报告疑似出现了具体法条引用,这超出了本系统「只做事实还原、不做法律推理」"
    "的设计边界——法律条文的引用和适用判断应完全交给律师,不是本系统的职责范围。请核实报告中"
    "是否混入了法条引用,如果有,不应把该处内容当作系统的正式结论看待。]"
)

# 专门给 indictment_check.py(起诉书事实核对)用的检查——这个功能的边界比其他报告更严格:
# 不只是不做法律推理,连"这两条对不对得上"这种事实层面的评价措辞都不允许,只能纯并列。
# 这条边界只写在 prompt 里试过,实测下来模型不是100%守规矩(哪怕明确禁止,还是会漏几处),
# 跟法条引用检测一样,不能只靠 prompt 自觉,需要代码兜底。
VERDICT_WORDS = ("矛盾", "不一致", "建议", "存在问题", "值得注意", "不合理", "有出入")

VERDICT_LANGUAGE_NOTE = (
    "\n\n[系统提示:本对照文本中检测到疑似下结论的措辞(矛盾/不一致/建议等),这类表述超出了"
    "本功能\"只做起诉书与证据并列对照、不做判断\"的设计边界。请自行核对上文中是否有类似措辞,"
    "对应内容是否存在实质差异、差异是否重要,应由律师自己判断,不应受该措辞影响。]"
)


def looks_corrupted(text: str) -> bool:
    return any(marker in text for marker in CORRUPTION_MARKERS)


def cites_law(text: str) -> bool:
    return bool(LEGAL_CITATION_RE.search(text))


def has_verdict_language(text: str) -> bool:
    return any(w in text for w in VERDICT_WORDS)


def estimate_max_tokens(reference_text: str, floor: int = 8000, cap: int = 64000, margin: int = 4000) -> int:
    """按参照文本(比如初稿、待整合的N份报告拼接后的长度)估算这一步输出大概需要多少 token。
    封顶 64000——这是 batch_analysis.py 整合珍惜动物案8批报告时实测过的、deepseek-chat
    单次真的能吐出来的量级,不是凭空定的上限。"""
    return min(max(floor, len(reference_text) + margin), cap)


def check_report(content: str, finish_reason: str) -> tuple[str, bool]:
    """返回 (可能加了提示标注的报告文本, 是否健康)。

    截断、疑似法条引用,都只追加一句提示、不丢内容(至少前半段/其余部分是真的,交给人看好过
    什么都没有);夹带工具调用格式痕迹的判定为损坏,调用方应该整段弃用、退回上一步更早、
    更干净的版本(这个判断由调用方自己再查一次 looks_corrupted() 决定,这里只汇总健康状态)。"""
    healthy = True
    if finish_reason == "length":
        content += TRUNCATION_NOTE
        healthy = False
    if looks_corrupted(content):
        healthy = False
    if cites_law(content):
        content += LEGAL_CITATION_NOTE
        healthy = False
    return content, healthy
