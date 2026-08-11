# modules/main.py
# EU AI Act 合规体系建设建议系统 - 主入口

import os
import json
from datetime import datetime
from modules.rules_layer import run_rules_layer
from modules.analysis_layer import run_analysis_layer
from modules.review_layer import run_review_layer
from modules.ai_act_hubs import effective_status


def load_law_texts() -> dict:
    """按条款号索引的法条语料字典，取用哪几条由调用方决定，不再是一整块blob"""
    config_path = "schemas/law_texts.json"
    if os.path.exists(config_path):
        with open(config_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def generate_report(rule_output: dict, analysis_output: dict) -> str:
    role = rule_output.get("role", "未提供")
    risk_level = rule_output.get("risk_level", "未知")
    risk_basis = rule_output.get("risk_basis", "")
    recs = analysis_output.get("recommendations", [])

    lines = []
    lines.append("# EU AI Act 合规体系建设建议书")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("**系统**: " + analysis_output.get("system_name", "未命名"))
    lines.append(f"**角色**: {role}")
    lines.append(f"**风险等级**: {risk_level}（{risk_basis}）")
    lines.append("")
    lines.append("---")
    lines.append("")

    if risk_level != "高风险":
        lines.append("## 一、风险分级结论")
        lines.append("")
        if risk_level == "禁止":
            lines.append(f"⛔ 该场景命中 AI Act Art.5 禁止用途清单：{risk_basis}。**该用例本身不得投入使用，需重新设计或停止，不适用后续的义务清单分析。**")
        elif risk_level == "有限风险":
            lines.append(f"该场景未落入高风险清单，但触发了 Art.50 透明度义务：{risk_basis}。需履行相应的透明度披露要求（如与自然人交互需明确告知、生成内容需标注等），无需承担 Art.9-19 一整套高风险义务。")
        else:
            lines.append(f"该场景未落入禁止清单、Annex III 高风险清单，也未触发 Art.50 透明度义务，目前不承担 AI Act 项下的强制性义务。建议保留本次评估记录，业务发生变化时重新评估。")
        lines.append("")
        lines.append("---")
        lines.append("")
        lines.append("**报告说明**：本报告基于 AI Act 风险分级结果生成，风险等级判定为代码规则推导，非LLM自由判断。")
        return "\n".join(lines)

    has_gap = [r for r in recs if r.get("gap") and r.get("gap") != "无"]
    no_gap = [r for r in recs if not r.get("gap") or r.get("gap") == "无"]
    total = len(recs)

    lines.append("## 一、合规体系成熟度评估")
    lines.append("")
    lines.append(f"- **已覆盖义务**: {len(no_gap)}/{total}")
    lines.append(f"- **需建设义务**: {len(has_gap)}/{total}")
    lines.append("")
    lines.append("---")
    lines.append("")

    if has_gap:
        lines.append("## 二、体系建设建议")
        lines.append("")
        lines.append("排序：先按法律生效日期（客观事实，代码计算），同一生效日期下再按业务优先级P0/P1/P2。")
        lines.append("")

        priority_rank = {"P0": 0, "P1": 1, "P2": 2}
        for r in has_gap:
            r["_effective"] = effective_status(r.get("article", ""))
        has_gap_sorted = sorted(
            has_gap,
            key=lambda r: (r["_effective"].get("effective_date") or "9999-99-99", priority_rank.get(r.get("priority"), 9))
        )

        for r in has_gap_sorted:
            eff = r["_effective"]
            lines.append(f"### {r.get('article')} {r.get('title')}")
            lines.append("")
            lines.append(f"- **缺失点**: {r.get('gap')}")
            lines.append(f"- **建设建议**: {r.get('recommendation')}")
            lines.append(f"- **法条依据**: {r.get('law_basis')}")
            lines.append(f"- **业务优先级**: {r.get('priority', '未定')}")
            lines.append(f"- **法律生效状态**: {eff.get('status')}（{eff.get('note')}）")
            lines.append("")
    else:
        lines.append("## 二、体系建设建议")
        lines.append("")
        lines.append(f"✅ 所有{total}条义务均已覆盖，无缺失项。")

    lines.append("---")
    lines.append("")
    lines.append(f"**报告说明**: 本报告基于 {role} 在「{risk_level}」等级下的义务清单生成。")

    return "\n".join(lines)


def run_eu_pipeline(human_input: dict) -> dict:
    print("=" * 60)
    print("🚀 EU AI Act 合规体系建设建议系统启动")
    print("=" * 60)

    law_texts = load_law_texts()
    print(f"✅ 法条原文加载成功（{len(law_texts)} 条）")

    rule_output = run_rules_layer(human_input)
    if not rule_output:
        print("❌ 规则层执行失败")
        return None

    analysis_output = run_analysis_layer(rule_output, human_input, law_texts)
    if not analysis_output:
        print("❌ 分析层执行失败")
        return None

    if rule_output.get("risk_level") == "高风险":
        review_result = run_review_layer(analysis_output)
        if review_result.get("status") != "approved":
            print("❌ 审核不通过")
            return None

    print("\n📄 生成合规体系建设建议书...")
    report = generate_report(rule_output, analysis_output)
    os.makedirs("outputs", exist_ok=True)
    report_path = "outputs/eu_ai_act_report.md"
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)

    print(f"✅ 报告已生成: {report_path}")

    analysis_output["risk_level"] = rule_output.get("risk_level")
    analysis_output["role"] = rule_output.get("role")
    analysis_output["risk_basis"] = rule_output.get("risk_basis")

    with open("outputs/eu_ai_act_analysis.json", 'w', encoding='utf-8') as f:
        json.dump(analysis_output, f, indent=2, ensure_ascii=False)

    print("=" * 60)
    return analysis_output
