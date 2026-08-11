# adapters.py
# 适配层：把 FactStore 里收集到的要件，拼接成各引擎后端所需的最终格式

from intake.state import FactStore


def build_gdpr_scenario_text(fs: FactStore) -> str:
    """把 FactStore 里的要件拼成 run_risk_harness 所需的 scenario_text"""
    lines = []
    lines.append(f"项目名称：{fs.get_value('system_name', '未命名项目')}")
    lines.append(f"场景描述：{fs.get_value('core_function', '未提供')}")

    data_types = fs.get_value('data_types', [])
    if data_types:
        lines.append("\n【数据处理活动】")
        for item in data_types:
            lines.append(f"- {item}")

    lines.append(f"\n【数据主体】\n{fs.get_value('data_subjects', '未提供')}")
    lines.append(f"\n【数据来源】\n{fs.get_value('data_source', '未提供')}")

    special_category = fs.get_value('special_category_data')
    if special_category is True:
        lines.append("\n【敏感类别数据】\n涉及Art.9敏感类别数据")
    elif special_category is False:
        lines.append("\n【敏感类别数据】\n不涉及Art.9敏感类别数据")

    cross_border = fs.get_value('cross_border_transfer')
    if cross_border is True:
        lines.append(f"\n【跨境传输】\n是，数据存储/处理地点：{fs.get_value('data_location', '未提供')}")
    elif cross_border is False:
        lines.append("\n【跨境传输】\n否，数据不传出欧盟")

    protections = fs.get_value('existing_protections', '')
    if not protections or protections.strip() in ["无", "没有", "暂无"]:
        lines.append("\n【现有保护措施】\n未提供具体保护措施（建议标记为信息缺口）")
    else:
        lines.append(f"\n【现有保护措施】\n{protections}")

    automated_decision = fs.get_value('automated_decision_exists')
    if automated_decision is True:
        lines.append("\n【自动化决策】\n系统输出会在无实质人工介入的情况下直接触发对个人有实质影响的业务动作")

    lines.append("\n【适用法律范围】\nGDPR（欧盟通用数据保护条例）")

    return "\n".join(lines)


def build_ai_act_human_input(fs: FactStore) -> dict:
    """把 FactStore 里的要件映射为 run_eu_pipeline 所需的 human_input 字典"""
    data_types = fs.get_value('data_types', [])
    data_types_text = "、".join(data_types) if isinstance(data_types, list) else (data_types or "")

    return {
        "system_name": fs.get_value('system_name', ''),
        "core_function": fs.get_value('core_function', ''),
        "role": fs.get_value('role', ''),
        "stage": fs.get_value('lifecycle_stage', ''),
        "scenario": fs.get_value('use_case_category', ''),
        "data_types": data_types_text,
        "output_content": fs.get_value('core_function', ''),
        "interacts": "是" if fs.get_value('automated_decision_exists') else "未知",
        "existing_docs": fs.get_value('existing_docs', ''),
    }
