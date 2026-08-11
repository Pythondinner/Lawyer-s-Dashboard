"""比 citation_check.py 更快更便宜的核验方式:既然分析层现在会在每条发现后面附一句
逐字摘抄的原文,核对"这句话在不在被引用的那一页"就是简单的字符串匹配,不需要再调用模型
去判断"支不支撑"——省一次LLM调用,而且字符串能不能精确匹配上,判断标准比"LLM觉得像不像"
更硬,不会出现模棱两可的情况。"""

import json
import re
import unicodedata
from dataclasses import asdict, dataclass

FINDING_RE = re.compile(r"【([^】]+?)\s*第(\d+)页】.*?原文[：:]\s*[\"“]([^\"”]+)[\"”]", re.DOTALL)

# 摘句和原文之间允许的差异:标点、空格这类排版噪声,不影响"内容对不对得上"这个判断
_NOISE_RE = re.compile(r"[\s,，。.、；;：:\"“”「」『』（）()《》\-—=＝×*]")


def _normalize(s: str) -> str:
    # 先做 NFKC 归一化,把全角数字/字母/标点转成半角——OCR原文和模型摘抄经常一个全角一个半角,
    # 不做这一步,肉眼看着一样的内容会被判定为"没匹配上"
    s = unicodedata.normalize("NFKC", s)
    return _NOISE_RE.sub("", s)


def _split_fragments(raw_quote: str) -> list[str]:
    """摘句里如果有省略号(表格类内容摘抄时常见),说明模型跳过了中间的列,
    要在归一化(会把句号也清掉)之前先按省略号拆开,不然省略号本身会被清洗掉,拆不出片段。"""
    return [_normalize(f) for f in re.split(r"\.{2,}|…+", raw_quote) if _normalize(f)]


def _match_quote(fragments: list[str], haystack: str) -> bool:
    if not fragments:
        return False
    return all(f in haystack for f in fragments)


@dataclass
class QuoteCheckResult:
    volume: str
    page: int
    quote: str
    verdict: str  # matched / found_on_different_page / not_found
    correct_page: int | None
    context: str
    actual_volume: str | None = None  # 只有 verdict=found_on_different_page 且发生跨卷时才有值


def extract_findings(report_text: str) -> list[dict]:
    results = []
    for m in FINDING_RE.finditer(report_text):
        volume, page, quote = m.group(1).strip(), int(m.group(2)), m.group(3).strip()
        context = report_text[max(0, m.start() - 20) : m.start()].splitlines()[-1] if m.start() > 0 else ""
        results.append({"volume": volume, "page": page, "quote": quote, "context": context})
    return results


def build_ledger_index(ledger_paths: list[str]) -> dict[tuple[str, int], str]:
    index: dict[tuple[str, int], list[str]] = {}
    for path in ledger_paths:
        with open(path, "r", encoding="utf-8") as f:
            entries = json.load(f)
        for e in entries:
            key = (e["volume"], e["page"])
            index.setdefault(key, []).append(e["text"])
    return {k: "\n".join(v) for k, v in index.items()}


def build_pages_by_volume(ledger_index: dict[tuple[str, int], str]) -> dict[str, list[int]]:
    pages_by_volume: dict[str, list[int]] = {}
    for volume, page in ledger_index:
        pages_by_volume.setdefault(volume, []).append(page)
    return pages_by_volume


def verify_one(
    quote: str,
    volume: str,
    page: int,
    ledger_index: dict[tuple[str, int], str],
    pages_by_volume: dict[str, list[int]],
    search_radius: int = 3,
    context: str = "",
) -> QuoteCheckResult | None:
    """单条核验,是 verify_quotes 批量核验和 analysis 里工具调用共用的核心逻辑,只写一遍。"""
    fragments = _split_fragments(quote)
    if not fragments:
        return None

    cur_text = _normalize(ledger_index.get((volume, page), ""))
    if _match_quote(fragments, cur_text):
        return QuoteCheckResult(volume, page, quote, "matched", None, context)

    # 当前页找不到,先在附近几页找,再退而求其次搜同一卷,最后搜其他卷(串卷的情况)
    found_volume, found_page = None, None
    for offset in range(1, search_radius + 1):
        for candidate in (page - offset, page + offset):
            text = _normalize(ledger_index.get((volume, candidate), ""))
            if text and _match_quote(fragments, text):
                found_volume, found_page = volume, candidate
                break
        if found_page:
            break
    if found_page is None:
        for candidate in pages_by_volume.get(volume, []):
            if abs(candidate - page) <= search_radius:
                continue
            text = _normalize(ledger_index.get((volume, candidate), ""))
            if _match_quote(fragments, text):
                found_volume, found_page = volume, candidate
                break
    if found_page is None:
        for (other_volume, other_page), text in ledger_index.items():
            if other_volume == volume:
                continue
            if _match_quote(fragments, _normalize(text)):
                found_volume, found_page = other_volume, other_page
                break

    if found_page is not None:
        return QuoteCheckResult(volume, page, quote, "found_on_different_page", found_page, context, found_volume)
    return QuoteCheckResult(volume, page, quote, "not_found", None, context)


def verify_quotes(report_text: str, ledger_paths: list[str], search_radius: int = 3) -> list[QuoteCheckResult]:
    findings = extract_findings(report_text)
    ledger_index = build_ledger_index(ledger_paths)
    pages_by_volume = build_pages_by_volume(ledger_index)

    results = []
    for f in findings:
        r = verify_one(f["quote"], f["volume"], f["page"], ledger_index, pages_by_volume, search_radius, f["context"])
        if r:
            results.append(r)
    return results


def print_summary(results: list[QuoteCheckResult]) -> None:
    from collections import Counter

    counts = Counter(r.verdict for r in results)
    total = len(results)
    print(f"\n共核验 {total} 条摘句:")
    for verdict, n in counts.most_common():
        print(f"  {verdict}: {n} ({n/total*100:.1f}%)")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("report_path")
    parser.add_argument("ledger_paths", nargs="+")
    parser.add_argument("--out", default="quote_check_result.json")
    args = parser.parse_args()

    with open(args.report_path, "r", encoding="utf-8") as f:
        report_text = f.read()

    results = verify_quotes(report_text, args.ledger_paths)
    print_summary(results)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in results], f, ensure_ascii=False, indent=2)
    print(f"详细结果写入 {args.out}")
