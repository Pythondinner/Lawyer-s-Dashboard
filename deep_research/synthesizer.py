"""Synthesizer:现在是"记者→并行笔杆子"两阶段流水线的编排者,不再自己直接生成报告。

sources 数组依然由代码直接从证据记录生成(规则型,不给LLM编造URL的机会),按URL归一化去重。
记者(reporter.py)负责核实+分类,笔杆子(writer.py)负责逐个主题并行写作——这两个都是新模块。
Synthesizer 自己保留的职责:证据去重、分发给记者/笔杆子、组装最终报告、跑 Reflection Loop
(引用校验,只重试真正有问题的板块,不是整体推倒重来)、检查证据利用率。
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urlparse

import reporter
import writer
from events import emit

_MOBILE_HOST_PREFIXES = ("www.", "m.", "wap.", "mobile.")

MAX_SECTION_RETRIES = 2
UTILIZATION_MIN_EVIDENCE = 4
UTILIZATION_THRESHOLD = 0.4


def normalize_url(url: str | None) -> str | None:
    """粗粒度URL归一化:把同一域名家族下"m."/"www."这类子域名前缀的差异抹平,
    让同一篇文章的移动版/网页版被识别成同一个来源。不追求完美——
    不同域名转载的相同内容(比如新闻联合转发)这种更深的近似重复,这里识别不了,
    需要内容相似度判断,是比这个更大的工程,这里刻意不做。
    """
    if not url:
        return url
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    for prefix in _MOBILE_HOST_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix):]
            break
    return f"{host}{parsed.path.rstrip('/')}"


def build_sources(evidence_records: list[dict]) -> tuple[list[dict], dict[str, str]]:
    """规则型:sources 数组直接从证据记录生成,按归一化URL去重——同一个网页不管被抓了几次
    还是换了个host前缀,只算一个来源、一个 source_id,不然"多来源互相印证"会被同一篇文章
    重复计数,显得比实际更可信。

    返回 (去重后的 sources 列表, source_id 重映射表)。
    """
    sources = []
    url_to_canonical = {}
    remap = {}

    for e in evidence_records:
        key = normalize_url(e.get("url")) or e["source_id"]
        if key in url_to_canonical:
            remap[e["source_id"]] = url_to_canonical[key]
            continue
        url_to_canonical[key] = e["source_id"]
        remap[e["source_id"]] = e["source_id"]
        sources.append(
            {
                "id": e["source_id"],
                "name": e.get("name"),
                "url": e.get("url"),
                "status": e.get("status"),
            }
        )
    return sources, remap


def _build_evidence_for_llm(evidence_records: list[dict], remap: dict[str, str]) -> list[dict]:
    """把证据的 source_id 换成去重后的 canonical id,同一个 canonical id 只出现一次——
    记者和笔杆子看到的输入里,压根没有"同一个来源两个ID"这回事,从源头杜绝重复引用。
    """
    seen = set()
    evidence_for_llm = []
    for e in evidence_records:
        canonical_id = remap[e["source_id"]]
        if canonical_id in seen:
            continue
        seen.add(canonical_id)
        evidence_for_llm.append(
            {
                "source_id": canonical_id,
                "status": e["status"],
                "extracted_claim": e["extracted_claim"],
            }
        )
    return evidence_for_llm


def _collect_section_citations(section: dict) -> set[str]:
    ids = set()
    t = section.get("type")
    if t == "comparison_table":
        for row in section.get("rows", []):
            for v in row.get("values", []):
                if v.get("source_id"):
                    ids.add(v["source_id"])
    elif t == "timeline":
        for ev in section.get("events", []):
            if ev.get("source_id"):
                ids.add(ev["source_id"])
    elif t == "steps":
        for step in section.get("steps", []):
            if step.get("source_id"):
                ids.add(step["source_id"])
    elif t == "item_list":
        for item in section.get("items", []):
            if item.get("source_id"):
                ids.add(item["source_id"])
    elif t == "narrative":
        ids.update(section.get("citations", []))
    return ids


def validate_section_citations(section: dict, valid_ids: set[str]) -> list[str]:
    """Reflection Loop 的校验环节:纯代码检查,一个板块里出现的每个 source_id 是否都能在
    sources 里找到。只校验单个板块,方便只重试出问题的那一块,不用整份报告推倒重来。
    """
    title = section.get("title", "?")
    problems = []
    for sid in _collect_section_citations(section):
        if sid not in valid_ids:
            problems.append(f"「{title}」引用了不存在的 source_id: {sid}")
    return problems


def _check_evidence_utilization(sections: list[dict], evidence_for_llm: list[dict]) -> dict | None:
    """软性检查,不触发重试,只在证据明明够但用得太少时,追加一条如实说明的 caveat。"""
    success_ids = {e["source_id"] for e in evidence_for_llm if e["status"] == "success"}
    if len(success_ids) < UTILIZATION_MIN_EVIDENCE:
        return None  # 证据本来就少,不算"没利用够"

    cited_ids = set()
    for section in sections:
        cited_ids |= _collect_section_citations(section)

    utilization = len(cited_ids & success_ids) / len(success_ids)
    if utilization < UTILIZATION_THRESHOLD:
        return {
            "issue": (
                f"共有 {len(success_ids)} 条可用证据,报告只直接引用了 {len(cited_ids & success_ids)} 条,"
                "其余可能因重复、次要或相关性较低未被采用。"
            ),
            "severity": "missing_data",
        }
    return None


def synthesize_report(question: str, evidence_records: list[dict], on_event=None) -> dict:
    sources, remap = build_sources(evidence_records)
    valid_ids = {s["id"] for s in sources}
    evidence_for_llm = _build_evidence_for_llm(evidence_records, remap)

    # 阶段一:记者核实 + 分类
    reporter_result = reporter.consolidate(question, evidence_for_llm, on_event=on_event)
    themes = reporter_result.get("themes", [])
    caveats = list(reporter_result.get("preliminary_caveats", []))

    # 阶段二:并行笔杆子,每个主题一次调用,复用 Multi-Agent 已验证过的并行调度模式
    emit(f"[笔杆子] {len(themes)} 位撰稿人并行撰写中...", on_event)
    sections: list[dict | None] = [None] * len(themes)
    if themes:
        with ThreadPoolExecutor(max_workers=len(themes)) as executor:
            future_to_idx = {
                executor.submit(writer.write_section, question, theme, None, on_event): i
                for i, theme in enumerate(themes)
            }
            for future in as_completed(future_to_idx):
                sections[future_to_idx[future]] = future.result()

    # 阶段三:逐板块校验,只重试真正有问题的板块
    unresolved_problems: list[str] = []
    total_attempts = 1
    for round_num in range(MAX_SECTION_RETRIES + 1):
        problems_by_idx = {
            i: validate_section_citations(section, valid_ids)
            for i, section in enumerate(sections)
            if validate_section_citations(section, valid_ids)
        }
        if not problems_by_idx:
            break
        if round_num == MAX_SECTION_RETRIES:
            unresolved_problems = [p for ps in problems_by_idx.values() for p in ps]
            break
        total_attempts += 1
        for i, problems in problems_by_idx.items():
            correction_note = (
                "你上一次写的这个板块里,以下引用有问题(引用了不存在的 source_id,"
                "必须严格使用给你的事实列表里的 source_id):\n" + "\n".join(problems)
            )
            sections[i] = writer.write_section(question, themes[i], correction_note=correction_note, on_event=on_event)

    utilization_note = _check_evidence_utilization(sections, evidence_for_llm)
    if utilization_note:
        caveats.append(utilization_note)

    emit("[校验] 引用校验" + ("通过" if not unresolved_problems else "未完全通过"), on_event)

    report = {
        "question": question,
        "headline": reporter_result.get("headline", question),
        "lede": reporter_result.get("lede", ""),
        "sections": sections,
        "caveats": caveats,
        "sources": sources,
    }

    return {
        "report": report,
        "unresolved_problems": unresolved_problems,
        "attempts": total_attempts,
    }
