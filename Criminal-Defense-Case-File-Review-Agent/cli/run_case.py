"""三层(摄取/分析/核验)整合成一条命令。给一份案子配置(JSON,列出要处理的 PDF+卷标签+
可选密码,以及/或者已经摄取好的台账路径),从摄取到分析(自动按规模选单次深度阅卷还是
分批分析)到核验(两者都自带自我核验循环)一路跑完,产出最终报告。

之前是分别手动跑 `python -m ingestion.pipeline ...`(每卷一次)、再手动跑
`python -m analysis.deep_read_agentic ...` 或 `analysis.batch_analysis`,案子一多容易
记混"这个案子该用哪个入口、卷宗都处理完了没、报告和台账是不是对应得上"。

案子配置格式示例:
{
    "case_name": "示例案",
    "volumes": [
        {"pdf": "示例卷2.pdf", "label": "卷2"},
        {"pdf": "示例卷3.pdf", "label": "卷3", "password": "如果加密"}
    ],
    "existing_ledgers": ["data/ledger/已经摄取过的卷.json"],
    "indictment_ledger": "data/ledger/起诉书.json"
}

起诉书/起诉意见书这两类文书不要求律师上传时提前单独分开——现实中卷宗材料经常是文书和
证据混在一起、或者按"文书卷"和"证据卷"分开但不会精确到"这几页专门是起诉书"。摄取完每一卷
之后,系统会用标题特征(独占一行的"起诉书"/"起诉意见书"字样)扫一遍候选页码范围,写进
manifest.json 的 indictment_candidates 里——这只是候选,不会自动当成正式的起诉书内容去跑
对照,必须律师在界面上确认(可以调整页码范围)之后,调用 confirm_indictment() 才会真正生成
对照表。indictment_ledger 这个配置字段仍然保留,是给已经明确知道自己那份就是干净起诉书台账
的场景(比如脚本化批量跑案子)走的旧路径,跳过候选确认直接生成对照。
"""

import argparse
import json
import os
import time
from datetime import datetime, timezone

from analysis.batch_analysis import estimate_tokens, run_batch_analysis
from analysis.consensus import run_consensus
from analysis.deep_read import PRINCIPLE_CATEGORY_NAMES
from analysis.deep_read_agentic import run_deep_read_agentic
from analysis.indictment_check import run_indictment_comparison
from analysis.indictment_docx import build_indictment_docx
from analysis.report_docx import build_report_docx
from analysis.timeline import run_timeline
from ingestion.indictment_locator import locate_candidates
from ingestion.ledger import load_ledger, save_ledger
from ingestion.pipeline import EncryptedPDFError, process_pdf

# 决定"整案走单次深度阅卷,还是走分批分析"的阈值——不能直接借用 batch_analysis.py 的
# DEFAULT_TOKEN_BUDGET(150_000),那个数字是"单批最大能装多少"的调优值,跟"整案多大就该
# 分批"是两回事。原来定的300_000偏保守了:后来在非法采矿案上实测到,视觉识别转录更完整后
# 台账涨到约55万token,被这个门槛错误地推去分批分析,而分批分析有已知的结构性代价——
# "跨批整合"这一步不够可靠,真的漏掉过一条案子里分量很重的独立验算(炸药量反推矿石量、
# 质疑起诉书鉴定量),不是无关紧要的小发现。而 deepseek-chat 单次prompt早就实测过能稳定
# 跑到62万token量级,55万本来就在能力范围内,不该被这道门槛拦下去分批。600_000 留了一点
# 余量在已验证的62万上限之下,不是又拍了一个新的经验值。
SINGLE_SHOT_TOKEN_BUDGET = 600_000

# 单次分析里,规模超过这个量级的案子改用"多次独立跑+合并"(analysis/consensus.py)而不是
# 单跑一次——实测过55万token量级的案子,"生成修正版"这一步撞上损坏(模型采样噪声导致输出
# 混入工具调用格式痕迹)的概率不低,而且具体哪条深度关联能不能被模型发现,同一个输入不同次
# 采样结果本身就有差异,不是"跑对了就一定复现"。跑2次独立分析、合并时保留两次都找到的高
# 置信度内容、也保留只出现一次但真实存在的发现,比赌单次采样能不能踩中更稳。这个门槛沿用
# 旧的 SINGLE_SHOT_TOKEN_BUDGET 经验值(30万)——这正是之前唯一观察到过可靠性问题的规模
# 区间,不是随便定的。
CONSENSUS_TOKEN_THRESHOLD = 300_000
CONSENSUS_RUNS = 2


def ingest_volumes(volumes: list[dict], ledger_dir: str, on_progress=None) -> tuple[list[str], list[str], list[dict]]:
    """处理配置里列出的每一份 PDF,返回 (成功生成的台账路径列表, 跳过的卷说明列表,
    起诉书/起诉意见书候选列表)。单份 PDF 加密打不开不中断整个案子——跳过它、记下原因,
    其余卷继续处理,这正是 EncryptedPDFError 这个专门异常设计的用途。

    on_progress 是可选的阶段提示回调(比如网页版拿来更新"处理中"页面显示的文字),不传
    就什么都不做——不想强迫 CLI 场景也要传一个回调。"""
    on_progress = on_progress or (lambda msg: None)
    os.makedirs(ledger_dir, exist_ok=True)
    ledger_paths = []
    skipped = []
    indictment_candidates = []
    for i, v in enumerate(volumes):
        label = v["label"]
        pdf_path = v["pdf"]
        password = v.get("password")
        print(f"[摄取] {pdf_path} -> {label}")
        on_progress(f"摄取中({i + 1}/{len(volumes)}):{label}")
        try:
            entries = process_pdf(pdf_path, label, password=password)
        except EncryptedPDFError as e:
            print(f"  跳过: {e}")
            skipped.append(f"{label}({pdf_path}): {e}")
            continue
        out_path = os.path.join(ledger_dir, f"{label}.json")
        save_ledger(entries, out_path)
        print(f"  {len(entries)}条记录 -> {out_path}")
        ledger_paths.append(out_path)

        candidates = locate_candidates(label, entries)
        for c in candidates:
            c["ledger_path"] = out_path
        if candidates:
            print(f"  发现{len(candidates)}处疑似起诉书/起诉意见书候选,待律师确认")
        indictment_candidates.extend(candidates)
    return ledger_paths, skipped, indictment_candidates


def confirm_indictment(
    case_dir: str,
    doc_type: str,
    ledger_path: str,
    start_page: int,
    end_page: int,
    candidate_start_page: int | None = None,
    candidate_end_page: int | None = None,
) -> dict:
    """律师在界面上确认某个候选(可能已经手动调整过页码范围)之后才真正执行:从候选来源卷的
    台账里按(调整后的)页码范围切一段出来当成这份文书的台账,跑对照、生成 docx,追加进
    manifest.json 的 indictments 列表。不需要重新摄取 PDF(候选内容本来就来自已经摄取好的
    台账),也不需要重新生成主报告——复用已经做过的工作,只多花对照这一步的开销。

    candidate_start_page/candidate_end_page 是"系统最初找到的候选范围"(不是律师确认时用的
    range),专门用来在候选列表里精确定位、摘掉这一条——律师很可能手动调整过页码范围,如果
    直接拿调整后的 start_page/end_page 去匹配候选列表,只要调整过就会匹配不上,候选会一直
    留着不消失。没传就假设律师没有调整,退化成直接用 start_page/end_page 去匹配。"""
    if candidate_start_page is None:
        candidate_start_page = start_page
    if candidate_end_page is None:
        candidate_end_page = end_page
    manifest_path = os.path.join(case_dir, "manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    with open(manifest["report_path"], "r", encoding="utf-8") as f:
        report = f.read()

    entries = load_ledger(ledger_path)
    subset = [e for e in entries if start_page <= e.page <= end_page]
    if not subset:
        raise ValueError(f"页码范围 {start_page}-{end_page} 在台账 {ledger_path} 里没有对应内容")

    indictment_ledger_dir = os.path.join(case_dir, "ledger")
    os.makedirs(indictment_ledger_dir, exist_ok=True)
    existing_of_type = [item for item in manifest.get("indictments", []) if item["type"] == doc_type]
    suffix = f"_{len(existing_of_type) + 1}" if existing_of_type else ""
    out_ledger_path = os.path.join(indictment_ledger_dir, f"{doc_type}{suffix}.json")
    save_ledger(subset, out_ledger_path)

    comparison, usage, healthy = run_indictment_comparison(out_ledger_path, report, doc_type=doc_type)
    comparison_path = os.path.join(case_dir, f"{doc_type}{suffix}_事实核对.txt")
    with open(comparison_path, "w", encoding="utf-8") as f:
        f.write(comparison)

    comparison_docx_path = os.path.join(case_dir, f"{doc_type}{suffix}_事实核对.docx")
    group_count = build_indictment_docx(comparison, manifest["case_name"], comparison_docx_path, doc_type=doc_type)
    print(f"[{doc_type}核对] {group_count}组对照,写入 {comparison_path} / {comparison_docx_path}")

    entry = {
        "type": doc_type,
        "ledger_path": out_ledger_path,
        "source_ledger_path": ledger_path,
        "start_page": start_page,
        "end_page": end_page,
        "comparison_path": comparison_path,
        "comparison_docx_path": comparison_docx_path,
        "healthy": healthy,
        "group_count": group_count,
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest.setdefault("indictments", []).append(entry)
    manifest.setdefault("indictment_candidates", [])
    manifest["indictment_candidates"] = [
        c
        for c in manifest["indictment_candidates"]
        if not (
            c["ledger_path"] == ledger_path
            and c["start_page"] == candidate_start_page
            and c["end_page"] == candidate_end_page
        )
    ]
    manifest["usage"]["prompt_tokens"] += usage["prompt_tokens"]
    manifest["usage"]["completion_tokens"] += usage["completion_tokens"]
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)

    return entry


def dismiss_indictment_candidate(case_dir: str, ledger_path: str, start_page: int, end_page: int) -> None:
    """律师确认某个候选"不是"起诉书/起诉意见书,不用真的处理它,只是从候选列表里摘掉,
    避免案子页面一直挂着一条永远不会被确认的候选提示。"""
    manifest_path = os.path.join(case_dir, "manifest.json")
    with open(manifest_path, "r", encoding="utf-8") as f:
        manifest = json.load(f)
    manifest["indictment_candidates"] = [
        c
        for c in manifest.get("indictment_candidates", [])
        if not (c["ledger_path"] == ledger_path and c["start_page"] == start_page and c["end_page"] == end_page)
    ]
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)


def run_case(config_path: str, out_dir: str = "data/cases", on_progress=None) -> dict:
    """跑完整个案子,返回运行摘要字典,并把同一份摘要落成 `manifest.json`——这是 Agent-1 交给
    下一步(不管是律师直接看,还是以后的 Agent-2)的正式产出物清单,不用再靠人记文件路径、
    也不用把"这次分析健不健康"这件事藏在报告正文里的一句提示文字里才能知道。

    on_progress(阶段说明字符串)在每个大阶段开始时调用一次——不是精确的百分比进度(每一步
    实际要跑多久,取决于模型生成长度、要不要重试,提前算不准,做一个假的百分比反而会让人
    误判"卡在87%是不是卡住了"),只回答"现在在干哪一步、还在动"这个更朴素也更诚实的问题。"""
    on_progress = on_progress or (lambda msg: None)
    with open(config_path, "r", encoding="utf-8") as f:
        config = json.load(f)

    case_name = config["case_name"]
    case_dir = os.path.join(out_dir, case_name)
    ledger_dir = os.path.join(case_dir, "ledger")
    os.makedirs(case_dir, exist_ok=True)

    print(f"===== 案子:{case_name} =====")
    t0 = time.time()

    new_ledger_paths, skipped, indictment_candidates = ingest_volumes(
        config.get("volumes", []), ledger_dir, on_progress=on_progress
    )
    existing_ledgers = config.get("existing_ledgers", [])
    for p in existing_ledgers:
        entries = load_ledger(p)
        label = os.path.splitext(os.path.basename(p))[0]
        candidates = locate_candidates(label, entries)
        for c in candidates:
            c["ledger_path"] = p
        indictment_candidates.extend(candidates)
    ledger_paths = new_ledger_paths + existing_ledgers

    manifest_path = os.path.join(case_dir, "manifest.json")

    if not ledger_paths:
        print("没有任何可用台账,终止")
        manifest = {
            "case_name": case_name,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "ok": False,
            "reason": "no_ledger",
            "skipped_volumes": skipped,
        }
        with open(manifest_path, "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        return manifest

    total_tokens = sum(estimate_tokens(p) for p in ledger_paths)
    print(f"共{len(ledger_paths)}份台账,估算约{total_tokens}token")

    debug_verify_path = os.path.join(case_dir, "verify_debug.json")
    if total_tokens <= SINGLE_SHOT_TOKEN_BUDGET:
        if total_tokens > CONSENSUS_TOKEN_THRESHOLD:
            print(f"规模在单次分析预算内,但已经到了容易撞上采样噪声的量级,跑{CONSENSUS_RUNS}次独立分析+合并")
            on_progress(f"分析中(单次深度阅卷,{CONSENSUS_RUNS}次独立跑+合并,大案子这一步更慢)")
            report, usage, healthy = run_consensus(ledger_paths, n_runs=CONSENSUS_RUNS)
            debug_verify_path = None  # 多次独立跑各自的核验记录分散在每次运行里,不是单一文件,这里不适用
            analysis_mode = "single_shot_consensus"
        else:
            print("规模在单次分析预算内,走单次深度阅卷(带自我核验)")
            on_progress("分析中(单次深度阅卷,带自我核验)")
            report, usage, healthy = run_deep_read_agentic(ledger_paths, debug_verify_path=debug_verify_path)
            analysis_mode = "single_shot"
        batch_count = 1
    else:
        print("规模超出单次分析预算,走分批分析")
        on_progress("分析中(分批分析,大案子这一步最慢)")
        report, usage, batches, healthy = run_batch_analysis(ledger_paths, cache_dir=os.path.join(case_dir, "batch_cache"))
        analysis_mode = "batch"
        batch_count = len(batches)
        debug_verify_path = None  # 分批路径每一批各自有自己的核验记录,不是单一文件,这里不适用

    on_progress("生成报告 Word 文档中")
    report_path = os.path.join(case_dir, "report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    report_docx_path = os.path.join(case_dir, "阅卷分析报告.docx")
    build_report_docx(report, case_name, report_docx_path)
    print(f"报告 Word 文档写入 {report_docx_path}")

    on_progress("提取时间线中")
    timeline_text, timeline_usage, timeline_healthy = run_timeline(ledger_paths)
    timeline_path = os.path.join(case_dir, "timeline.txt")
    with open(timeline_path, "w", encoding="utf-8") as f:
        f.write(timeline_text)
    timeline_docx_path = os.path.join(case_dir, "卷宗时间线.docx")
    build_report_docx(timeline_text, f"{case_name}（时间线）", timeline_docx_path)
    usage["prompt_tokens"] += timeline_usage["prompt_tokens"]
    usage["completion_tokens"] += timeline_usage["completion_tokens"]
    print(f"时间线写入 {timeline_path}(healthy={timeline_healthy})")

    if indictment_candidates:
        print(f"[起诉书候选] 共发现{len(indictment_candidates)}处候选,待律师在界面上确认后才会生成对照表")

    indictments = []
    legacy_indictment_ledger = config.get("indictment_ledger")
    if legacy_indictment_ledger:
        # 旧路径:配置里直接给了一份已经确认是干净起诉书内容的台账,跳过候选确认,
        # 立即生成对照——给已经明确知道范围的场景(比如脚本化批量跑案子)用。
        on_progress("起诉书事实核对中")
        print("[起诉书核对] 检测到 indictment_ledger,开始核对...")
        comparison, indictment_usage, indictment_healthy = run_indictment_comparison(
            legacy_indictment_ledger, report, doc_type="起诉书"
        )
        comparison_path = os.path.join(case_dir, "起诉书_事实核对.txt")
        with open(comparison_path, "w", encoding="utf-8") as f:
            f.write(comparison)
        usage["prompt_tokens"] += indictment_usage["prompt_tokens"]
        usage["completion_tokens"] += indictment_usage["completion_tokens"]

        comparison_docx_path = os.path.join(case_dir, "起诉书_事实核对.docx")
        group_count = build_indictment_docx(comparison, case_name, comparison_docx_path, doc_type="起诉书")
        print(f"[起诉书核对] {group_count}组对照,写入 {comparison_path}")

        indictments.append(
            {
                "type": "起诉书",
                "ledger_path": legacy_indictment_ledger,
                "source_ledger_path": legacy_indictment_ledger,
                "start_page": None,
                "end_page": None,
                "comparison_path": comparison_path,
                "comparison_docx_path": comparison_docx_path,
                "healthy": indictment_healthy,
                "group_count": group_count,
                "confirmed_at": datetime.now(timezone.utc).isoformat(),
            }
        )

    on_progress("收尾中(写 manifest)")
    elapsed = time.time() - t0
    print(f"===== 完成,用时{elapsed:.0f}秒,healthy={healthy} =====")
    print(f"[tokens] prompt={usage['prompt_tokens']} completion={usage['completion_tokens']}")
    print(f"报告写入 {report_path}")
    if skipped:
        print(f"跳过了{len(skipped)}份加密文件: {skipped}")
    if not healthy:
        print("警告:本次分析未完全健康(核验不完整/输出损坏/被截断/疑似法条引用之一),详见报告正文中的系统提示")

    manifest = {
        "case_name": case_name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ok": True,
        "healthy": healthy,
        "ledger_paths": ledger_paths,
        "report_path": report_path,
        "report_docx_path": report_docx_path,
        "timeline_path": timeline_path,
        "timeline_docx_path": timeline_docx_path,
        "timeline_healthy": timeline_healthy,
        "debug_verify_path": debug_verify_path,
        "indictments": indictments,
        "indictment_candidates": indictment_candidates,
        "analysis_mode": analysis_mode,
        "batch_count": batch_count,
        "skipped_volumes": skipped,
        "usage": usage,
        "elapsed_seconds": elapsed,
        "principle_categories": PRINCIPLE_CATEGORY_NAMES,
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    print(f"manifest写入 {manifest_path}")

    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="三层整合:给一份案子配置,从摄取跑到最终报告")
    parser.add_argument("config", help="案子配置 JSON 文件路径")
    parser.add_argument("--out-dir", default="data/cases")
    args = parser.parse_args()

    run_case(args.config, args.out_dir)
