# fusion/report_builder.py
# 从同一份结构化数据生成双视图报告（法务视图 + 工程视图），并插入交叉引用提示。
# 设计依据：REBUILD_DESIGN.md 第6/7节

import json
import os
from datetime import datetime
from fusion.cross_reference_map import find_cross_references

GLOSSARY_PATH = "glossary/terms.json"


def _load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _load_glossary():
    return _load_json(GLOSSARY_PATH, {"legal_to_engineering": [], "engineering_to_legal": []})


def _mitigations_by_risk_id(mitigation_data):
    result = {}
    for m in (mitigation_data or {}).get("mitigations", []):
        result[m.get("risk_id")] = m
    return result


def _necessity_by_risk_id(necessity_data):
    result = {}
    for j in (necessity_data or {}).get("justifications", []):
        result[j.get("risk_id")] = j
    return result


def build_fusion_report(output_dir: str = "./outputs") -> str:
    """读取outputs目录下GDPR+AI Act各阶段的结果，生成一份融合双视图报告"""
    risk_data = _load_json(os.path.join(output_dir, "risk_identification.json"), {})
    necessity_data = _load_json(os.path.join(output_dir, "necessity_justification.json"), {})
    mitigation_data = _load_json(os.path.join(output_dir, "mitigation_measures.json"), {})
    gdpr_hubs = _load_json(os.path.join(output_dir, "gdpr_hub_conclusions.json"), {})
    ai_act_report_path = os.path.join(output_dir, "eu_ai_act_report.md")

    mitigations_map = _mitigations_by_risk_id(mitigation_data)
    necessity_map = _necessity_by_risk_id(necessity_data)

    lines = []
    lines.append("# 合规评估融合报告（法务视图 + 工程视图）")
    lines.append("")
    lines.append(f"**生成时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**项目**: {risk_data.get('project_name', '未命名项目')}")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ---- 融合速览：交叉引用 ----
    ai_act_context = {
        "risk_level": None,  # 由main.py单独传入更准确，这里从报告文件里粗略提取不可靠，先留空位
        "use_case_category": None,
    }
    # 尝试从AI Act报告文本里粗提risk_level（仅用于展示，不作为判定依据——判定依据始终是代码枢纽结果）
    if os.path.exists(ai_act_report_path):
        with open(ai_act_report_path, 'r', encoding='utf-8') as f:
            ai_act_text = f.read()
        if "高风险" in ai_act_text:
            ai_act_context["risk_level"] = "高风险"
        elif "有限风险" in ai_act_text:
            ai_act_context["risk_level"] = "有限风险"
        elif "最小风险" in ai_act_text:
            ai_act_context["risk_level"] = "最小风险"
        elif "禁止" in ai_act_text:
            ai_act_context["risk_level"] = "禁止"

    cross_refs = find_cross_references(gdpr_hubs, ai_act_context)
    lines.append("## 一、融合速览：GDPR × AI Act 交叉引用")
    lines.append("")
    if cross_refs:
        for ref in cross_refs:
            lines.append(f"### {ref['gdpr_ref']} ↔ {ref['ai_act_ref']}")
            lines.append("")
            lines.append(f"- **关联性质**: {ref['relationship']}")
            lines.append(f"- **说明**: {ref['explanation']}")
            lines.append("")
    else:
        lines.append("本次评估未命中已知的交叉引用规则（不做泛化联想，只在真正命中法条明文关联或同一事实的两种后果时才提示）。")
        lines.append("")
    lines.append("---")
    lines.append("")

    # ---- GDPR 风险点：双视图 ----
    lines.append("## 二、GDPR 风险点（法务视图 / 工程视图）")
    lines.append("")
    risk_points = risk_data.get("risk_points", [])
    if not risk_points:
        lines.append("（无GDPR风险识别结果，可能尚未运行该引擎）")
        lines.append("")
    for rp in risk_points:
        rid = rp.get("risk_id")
        lines.append(f"### {rid}：{rp.get('risk_description', '')[:40]}")
        lines.append("")
        lines.append("| | 法务视图 | 工程视图 |")
        lines.append("|---|---|---|")

        law_refs = "；".join(
            f"{a.get('law')} {a.get('article')}" for a in rp.get("law_articles", [])
        ) or "（未提供）"
        necessity = necessity_map.get(rid, {})
        necessity_note = necessity.get("proportionality", "") if necessity else ""

        legal_cell = (
            f"风险: {rp.get('risk_description', '')}<br>依据: {law_refs}<br>严重程度: {rp.get('severity', '未知')}"
            + (f"<br>相称性评估: {necessity_note}" if necessity_note else "")
        )

        mitigation = mitigations_map.get(rid, {})
        if mitigation:
            eng_parts = []
            for key, label in [("technical_measures", "技术措施"), ("organizational_measures", "组织措施"), ("governance_measures", "治理措施")]:
                items = mitigation.get(key, [])
                if items:
                    eng_parts.append(f"{label}: " + "；".join(items))
            eng_cell = "<br>".join(eng_parts) if eng_parts else "（缓解方案未生成）"
        else:
            eng_cell = "（缓解方案未生成）"

        lines.append(f"| {rid} | {legal_cell} | {eng_cell} |")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 三、AI Act 义务差距（法务视图 / 工程视图）")
    lines.append("")
    ai_act_analysis = _load_json(os.path.join(output_dir, "eu_ai_act_analysis.json"), {})
    ai_act_recs = ai_act_analysis.get("recommendations", [])
    if ai_act_recs:
        lines.append(f"**角色**: {ai_act_analysis.get('role', '未提供')} ｜ **风险等级**: {ai_act_analysis.get('risk_level', '未知')}（{ai_act_analysis.get('risk_basis', '')}）")
        lines.append("")
        for rec in ai_act_recs:
            lines.append(f"### {rec.get('article')} {rec.get('title', '')}")
            lines.append("")
            lines.append("| | 法务视图 | 工程视图 |")
            lines.append("|---|---|---|")
            legal_cell = f"缺失点: {rec.get('gap', '')}<br>法条依据: {rec.get('law_basis', rec.get('article'))}"
            eng_cell = f"建设建议: {rec.get('recommendation', '')}<br>优先级: {rec.get('priority', '未定')}"
            lines.append(f"| {rec.get('article')} | {legal_cell} | {eng_cell} |")
            lines.append("")
    elif ai_act_analysis.get("risk_level") and ai_act_analysis.get("risk_level") != "高风险":
        lines.append(f"风险等级：{ai_act_analysis.get('risk_level')}（{ai_act_analysis.get('risk_basis', '')}），未触发Art.9-19一整套义务清单，详见 `{ai_act_report_path}`。")
    elif os.path.exists(ai_act_report_path):
        lines.append(f"详见独立报告：`{ai_act_report_path}`。")
    else:
        lines.append("（无AI Act分析结果，可能尚未运行该引擎）")
    lines.append("")
    lines.append("---")
    lines.append("")

    # ---- 术语脚注 ----
    glossary = _load_glossary()
    lines.append("## 四、术语对照（法律 ↔ 工程）")
    lines.append("")
    for entry in glossary.get("legal_to_engineering", []):
        lines.append(f"- **{entry['term']}**：{entry['explanation']}")
    lines.append("")

    report = "\n".join(lines)

    os.makedirs(output_dir, exist_ok=True)
    report_path = os.path.join(output_dir, "fusion_report.md")
    with open(report_path, 'w', encoding='utf-8') as f:
        f.write(report)

    return report_path
