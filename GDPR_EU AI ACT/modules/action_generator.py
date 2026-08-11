# modules/action_generator.py
# 合规动作建议生成器 - 基于风险结论生成可执行的动作计划

import os
import json
from datetime import datetime
from harness import Executor


def build_system_prompt() -> str:
    return """
你是一位顶尖的EU AI Act和GDPR合规实战专家，曾为多家跨国企业提供合规整改服务。你的任务是基于已识别的合规风险点，生成**具体、可执行、有优先级**的合规动作建议。

【输入】
一份风险识别报告，包含多个风险点，每个风险点包含：
- risk_id: 风险编号
- risk_domain: 风险领域（data/algorithm/user_rights/operation_governance）
- risk_description: 风险描述
- risk_cause: 风险根本原因
- severity: 严重程度（high/medium/low）
- law_articles: 相关法条（如 GDPR Art.44-46）

【输出要求】
对每个风险点，生成3-5条具体的合规动作建议。每条建议必须包含以下字段：
1. action: 具体动作描述（动词开头，清晰明确）
2. priority: P0（立即启动，1-2周内）、P1（近期启动，1-3个月）、P2（持续优化，3-6个月）
3. owner: 建议负责方（法务部 / 技术部 / 产品部 / 外部顾问 / 联合团队）
4. timeline: 预估完成周期（如"2周"、"1个月"）
5. acceptance_criteria: 验收标准（如何判断该动作已完成）

【参考示例】
风险：数据跨境传输至中国缺乏适当保障措施
动作建议：
- action: 采用欧盟标准合同条款（SCCs）作为跨境传输的合法基础
  priority: P0
  owner: 法务部 + 技术部
  timeline: 4周
  acceptance_criteria: SCCs协议签署完成并归档

【输出格式】
严格JSON，包含以下字段：
{
  "project_name": "项目名称",
  "generated_at": "时间戳",
  "action_plan": [
    {
      "risk_id": "R001",
      "risk_description": "风险描述",
      "actions": [
        {
          "action": "具体动作描述",
          "priority": "P0/P1/P2",
          "owner": "负责方",
          "timeline": "预估周期",
          "acceptance_criteria": "验收标准"
        }
      ]
    }
  ],
  "summary": {
    "total_risks": 8,
    "p0_actions": 5,
    "p1_actions": 2,
    "p2_actions": 1
  }
}
"""


def build_user_prompt(risk_data: dict) -> str:
    risk_points = risk_data.get("risk_points", [])
    project_name = risk_data.get("project_name", "未命名项目")
    return f"""
项目名称：{project_name}
风险点列表（共{len(risk_points)}个）：

{json.dumps(risk_points, indent=2, ensure_ascii=False)}

请为每个风险点生成3-5条具体合规动作建议，按JSON格式输出。
"""


def generate_action_plan(risk_file: str = "outputs/risk_identification.json") -> dict:
    """
    主入口：读取风险文件 → 调用大模型 → 生成动作计划 → 保存JSON
    """
    print("\n" + "=" * 60)
    print("📋 合规动作建议生成器启动")
    print("=" * 60)

    if not os.path.exists(risk_file):
        print(f"❌ 未找到风险文件: {risk_file}")
        return None

    with open(risk_file, 'r', encoding='utf-8') as f:
        risk_data = json.load(f)

    project_name = risk_data.get("project_name", "未命名项目")
    risk_count = len(risk_data.get("risk_points", []))
    print(f"📊 项目: {project_name} | 风险点: {risk_count} 个")

    executor = Executor()
    system_prompt = build_system_prompt()
    user_prompt = build_user_prompt(risk_data)

    print("⏳ 正在生成合规动作建议（调用DeepSeek）...")
    output = executor.execute(system_prompt, user_prompt)

    if not output:
        print("❌ 动作建议生成失败")
        return None

    # 确保输出包含必要字段
    if "action_plan" not in output:
        print("⚠️ 输出格式异常，缺少 action_plan 字段")
        return None

    # 补充项目名称和时间戳
    output["project_name"] = project_name
    output["generated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 保存结果
    output_dir = "outputs"
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, "action_plan.json")
    with open(output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    # 打印摘要
    summary = output.get("summary", {})
    print(f"\n✅ 合规动作建议已生成:")
    print(f"   - 总风险点: {summary.get('total_risks', 0)}")
    print(f"   - P0（立即启动）: {summary.get('p0_actions', 0)} 项")
    print(f"   - P1（近期启动）: {summary.get('p1_actions', 0)} 项")
    print(f"   - P2（持续优化）: {summary.get('p2_actions', 0)} 项")
    print(f"📁 保存路径: {output_path}")
    print("=" * 60)

    return output
