"""起诉书/起诉意见书候选定位——扫描已经摄取好的台账文本,凭标题特征(独占页面开头的
"起诉书"/"起诉意见书"字样)找候选页码范围。

纯规则匹配,不调用大模型:这两类文书的标题格式在公检法系统里高度标准化(单独一行的
"起诉书"/"起诉意见书"字样,紧跟着落款检察院/公安机关名称),正则就能可靠识别,没必要
为这么确定的模式再搭一层AI判断,徒增成本和不确定性。

找到的只是**候选**,不是最终认定——真正要不要采用、页码范围对不对,交给律师在界面上
确认/调整,系统自己不会替律师做这个决定(跟起诉书对照功能本身"不依赖模型识别"这条原则
是同一件事,只是这次"模型"换成了正则,判断权归属没有变)。"""

import re

from ingestion.ledger import LedgerEntry

_TITLE_PATTERNS = {
    "起诉书": re.compile(r"起\s*诉\s*书"),
    "起诉意见书": re.compile(r"起\s*诉\s*意\s*见\s*书"),
}

# 只看页面开头这几行非空内容,而且要求命中的那一行本身很短——真正的标题页,"起诉书"/
# "起诉意见书"几个字几乎总是独占一短行(前面顶多带发文机关名),不会是"...已经写了起诉书..."
# 这种嵌在长句子中间的叙述性提及。两个条件缺一个都会漏判成叙述性提及,不能只看"是否在开头附近"。
_TITLE_SEARCH_LINES = 3
_TITLE_LINE_MAX_LEN = 20


def _is_title_page(text: str, pattern: re.Pattern) -> bool:
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    for line in lines[:_TITLE_SEARCH_LINES]:
        compact = line.replace(" ", "").replace("　", "")
        if len(compact) <= _TITLE_LINE_MAX_LEN and pattern.search(compact):
            return True
    return False

# 找到标题页之后,如果找不到更明确的终止信号(比如下一份候选文书的标题页),候选范围默认
# 往后延伸这么多页——起诉书/起诉意见书通常不长,这只是给律师看的一个起点,律师在确认界面上
# 可以随时把结束页往前或往后调。
_DEFAULT_SPAN = 6


def locate_candidates(volume_label: str, entries: list[LedgerEntry]) -> list[dict]:
    """entries 是某一卷的台账条目(不要求已排序)。返回候选列表,每条:
    {"type", "volume_label", "start_page", "end_page", "preview"}。

    连续多页匹配到同一种标题(常见于文书每页都重复页眉"起诉书"三个字)只算一个候选,
    从第一页开始算,不会因为页眉重复被拆成好几个候选。"""
    entries_sorted = sorted(entries, key=lambda e: e.page)
    text_by_page = {e.page: e.text for e in entries_sorted}

    page_to_type: dict[int, str] = {}
    for page, text in text_by_page.items():
        for doc_type, pattern in _TITLE_PATTERNS.items():
            if _is_title_page(text, pattern):
                page_to_type[page] = doc_type
                break

    starts: list[tuple[int, str]] = []
    prev_page = None
    for page in sorted(page_to_type):
        doc_type = page_to_type[page]
        is_new_start = prev_page is None or page != prev_page + 1 or page_to_type.get(prev_page) != doc_type
        if is_new_start:
            starts.append((page, doc_type))
        prev_page = page

    if not starts:
        return []

    all_pages = sorted(text_by_page)
    last_page = all_pages[-1]

    candidates = []
    for i, (start_page, doc_type) in enumerate(starts):
        next_start = starts[i + 1][0] if i + 1 < len(starts) else None
        end_page = start_page + _DEFAULT_SPAN - 1
        if next_start is not None:
            end_page = min(end_page, next_start - 1)
        end_page = min(end_page, last_page)
        candidates.append(
            {
                "type": doc_type,
                "volume_label": volume_label,
                "start_page": start_page,
                "end_page": max(end_page, start_page),
                "preview": text_by_page[start_page][:120],
            }
        )
    return candidates
