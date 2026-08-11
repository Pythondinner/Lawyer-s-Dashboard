"""引用核验工具:分析报告里每一条【卷宗标签 第Y页】,拿去跟证据台账原文核对,
判断这一页是不是真的支撑了报告里写的那句话——不是只查"这页存不存在",
是查"内容对不对得上"(卷3第69页那次错误就是页码存在、但内容其实在第70页)。"""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

_client = None
MAX_WORKERS = 8

CITATION_RE = re.compile(r"【(.+?)\s*第([\d、，,\-—]+)页】")

VERIFY_PROMPT = """下面是一份刑事案件阅卷分析报告里的一条表述,以及它引用的卷宗原文(引用页,及前后各一页作参照)。

这行表述可能同时引用了好几个页码,你只需要判断"当前这一页"能不能支撑这行表述里跟这一页相关的
那部分内容,不用管其他页码。

判断标准:
- supported: 当前这一页的原文里,能找到支撑这条表述的具体内容(数字、说法、事实)
- not_found: 前后三页原文里都找不到支撑这条表述的内容
- found_on_different_page: 当前这一页原文里没有,但前一页或后一页原文里有,说明引用页码可能错了

分析报告里的表述(整行,只关注跟"当前引用页"相关的部分): {claim}

当前引用页 【{volume} 第{page}页】原文:
{cur_text}

前一页 【{volume} 第{prev_page}页】原文(供参照,判断有没有页码错位):
{prev_text}

后一页 【{volume} 第{next_page}页】原文(供参照,判断有没有页码错位):
{next_text}

只输出如下 JSON,不要输出其他文字:
{{"verdict": "supported" 或 "not_found" 或 "found_on_different_page", "correct_page": 数字或null, "note": "一句话说明"}}"""


@dataclass
class CitationCheckResult:
    volume: str
    page: int
    claim: str
    verdict: str
    correct_page: int | None
    note: str


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=os.environ["DEEPSEEK_API_KEY"], base_url="https://api.deepseek.com", timeout=600.0)
    return _client


def parse_page_numbers(raw: str) -> list[int]:
    pages = []
    for part in re.split(r"[、，,]", raw):
        part = part.strip()
        if not part:
            continue
        m = re.match(r"(\d+)\s*[-—]\s*(\d+)", part)
        if m:
            pages.extend(range(int(m.group(1)), int(m.group(2)) + 1))
        else:
            digits = re.match(r"\d+", part)
            if digits:
                pages.append(int(digits.group()))
    return pages


def extract_citations(report_text: str) -> list[dict]:
    results = []
    for line in report_text.splitlines():
        line = line.strip()
        if not line:
            continue
        for m in CITATION_RE.finditer(line):
            volume = m.group(1).strip()
            for page in parse_page_numbers(m.group(2)):
                results.append({"volume": volume, "page": page, "claim": line})
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


def _verify_one(citation: dict, ledger_index: dict[tuple[str, int], str]) -> CitationCheckResult:
    volume, page, claim = citation["volume"], citation["page"], citation["claim"]
    cur_text = ledger_index.get((volume, page), "(该页在台账里不存在)")
    prev_text = ledger_index.get((volume, page - 1), "(无)")
    next_text = ledger_index.get((volume, page + 1), "(无)")

    if (volume, page) not in ledger_index:
        return CitationCheckResult(volume, page, claim, "page_not_found", None, "台账里没有这一页,可能是卷宗标签或页码写错了")

    client = _get_client()
    prompt = VERIFY_PROMPT.format(
        claim=claim,
        volume=volume,
        page=page,
        cur_text=cur_text[:1500],
        prev_page=page - 1,
        prev_text=prev_text[:800],
        next_page=page + 1,
        next_text=next_text[:800],
    )
    resp = client.chat.completions.create(
        model="deepseek-chat",
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        max_tokens=300,
        temperature=0,
    )
    try:
        parsed = json.loads(resp.choices[0].message.content)
    except json.JSONDecodeError:
        return CitationCheckResult(volume, page, claim, "check_failed", None, "核验本身解析失败")

    return CitationCheckResult(
        volume, page, claim,
        parsed.get("verdict", "unknown"),
        parsed.get("correct_page"),
        parsed.get("note", ""),
    )


def verify_report(report_text: str, ledger_paths: list[str]) -> list[CitationCheckResult]:
    citations = extract_citations(report_text)
    ledger_index = build_ledger_index(ledger_paths)

    results: list[CitationCheckResult] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_verify_one, c, ledger_index): c for c in citations}
        for i, future in enumerate(as_completed(futures), start=1):
            r = future.result()
            print(f"[{i}/{len(citations)}] 【{r.volume} 第{r.page}页】-> {r.verdict}")
            results.append(r)
    return results


def print_summary(results: list[CitationCheckResult]) -> None:
    from collections import Counter

    counts = Counter(r.verdict for r in results)
    total = len(results)
    print(f"\n共核验 {total} 条引用:")
    for verdict, n in counts.most_common():
        print(f"  {verdict}: {n} ({n/total*100:.1f}%)")


VERDICT_LABEL = {
    "supported": "已核实,页码可信",
    "found_on_different_page": "页码可能错了,建议按下面的建议页核对",
    "not_found": "前后页都找不到依据,需要重新核实",
    "page_not_found": "台账里根本没有这一页,标签/页码本身有问题",
    "check_failed": "核验过程本身出错,未判断",
}


def apply_corrections(report_text: str, results: list[CitationCheckResult]) -> tuple[str, list[str]]:
    """把"差一页"这类高把握的错误,直接在报告原文里改过来,但留痕迹,不悄悄改。

    只对单页引用(【卷X 第N页】这种,不含顿号/范围)做原地替换——多页引用(【卷X 第11、114、147页】)
    拆开改容易把格式改乱,风险不划算,这类留给下面的核验清单,不做原地替换。"""
    correction_map: dict[tuple[str, int, str], int] = {}
    for r in results:
        if r.verdict == "found_on_different_page" and r.correct_page and r.correct_page != r.page:
            correction_map[(r.volume, r.page, r.claim)] = r.correct_page

    log: list[str] = []
    out_lines = []
    for raw_line in report_text.splitlines(keepends=True):
        stripped = raw_line.strip()
        matches = list(CITATION_RE.finditer(raw_line))
        new_line = raw_line
        for m in reversed(matches):
            volume = m.group(1).strip()
            pages_str = m.group(2)
            pages = parse_page_numbers(pages_str)
            if len(pages) != 1:
                continue  # 多页引用不做原地替换
            page = pages[0]
            key = (volume, page, stripped)
            if key not in correction_map:
                continue
            correct_page = correction_map[key]
            old_bracket = m.group(0)
            new_bracket = f"【{volume} 第{correct_page}页(原引第{page}页,经核验自动修正)】"
            new_line = new_line[: m.start()] + new_bracket + new_line[m.end() :]
            log.append(f"【{volume} 第{page}页】-> 第{correct_page}页  (出处: {stripped[:60]}…)")
        out_lines.append(new_line)

    return "".join(out_lines), log


def build_annotated_report(report_text: str, results: list[CitationCheckResult]) -> str:
    """原始报告(单页引用里"差一页"的错误已原地修正、留痕迹) + 一份可核对的引用核验清单。"""
    corrected_text, correction_log = apply_corrections(report_text, results)

    lines = ["# 阅卷分析报告(已附引用核验,单页引用的一页之差已自动修正并标注)\n", corrected_text.strip()]

    if correction_log:
        lines.append("\n\n---\n# 自动修正记录\n")
        for entry in correction_log:
            lines.append(f"- {entry}\n")

    lines.append("\n\n---\n# 引用核验清单\n")

    for verdict in ["not_found", "found_on_different_page", "page_not_found", "check_failed", "supported"]:
        group = [r for r in results if r.verdict == verdict]
        if not group:
            continue
        lines.append(f"\n## {VERDICT_LABEL.get(verdict, verdict)}({len(group)}条)\n")
        for r in group:
            claim_preview = r.claim[:80] + ("…" if len(r.claim) > 80 else "")
            suggestion = f" -> 建议核对第{r.correct_page}页" if r.correct_page and r.correct_page != r.page else ""
            lines.append(f"- 【{r.volume} 第{r.page}页】{suggestion}  {r.note}\n  原文表述: {claim_preview}\n")

    return "\n".join(lines)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("report_path")
    parser.add_argument("ledger_paths", nargs="+")
    parser.add_argument("--out", default="citation_check_result.json")
    args = parser.parse_args()

    with open(args.report_path, "r", encoding="utf-8") as f:
        report_text = f.read()

    results = verify_report(report_text, args.ledger_paths)
    print_summary(results)

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in results], f, ensure_ascii=False, indent=2)
    print(f"\n详细结果写入 {args.out}")

    annotated = build_annotated_report(report_text, results)
    annotated_path = args.out.replace(".json", "_annotated.md")
    with open(annotated_path, "w", encoding="utf-8") as f:
        f.write(annotated)
    print(f"给律师看的合并版写入 {annotated_path}")
