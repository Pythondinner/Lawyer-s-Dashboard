# modules/mitigation_design.py
# 防御方案模块 - 自循环Harness

import os
import json
import yaml
import jsonschema
from harness import Ledger, Observer, Brain, Executor


def validate_schema(output: dict, schema: dict):
    try:
        jsonschema.validate(instance=output, schema=schema)
        return True, None
    except jsonschema.ValidationError as e:
        return False, str(e)


class MitigationObserver(Observer):
    def __init__(self, risk_points):
        super().__init__()
        self.risk_points = risk_points
        self.risk_ids = [r.get("risk_id") for r in risk_points]

    def observe(self, outputs):
        if isinstance(outputs, list):
            mitigations = outputs
        elif isinstance(outputs, dict) and "mitigations" in outputs:
            mitigations = outputs["mitigations"]
        else:
            mitigations = outputs if isinstance(outputs, list) else []

        covered_ids = [m.get("risk_id") for m in mitigations if m.get("risk_id")]
        missing_ids = [rid for rid in self.risk_ids if rid not in covered_ids]

        issues = []
        for m in mitigations:
            risk_id = m.get("risk_id")
            if not risk_id:
                continue
            categories = ["technical_measures", "organizational_measures", "governance_measures"]
            for category in categories:
                items = m.get(category, [])
                if not items or len(items) == 0:
                    issues.append(f"风险 {risk_id} 的 {category} 为空")

        return {
            "status": "complete" if not missing_ids and not issues else "partial",
            "covered_ids": covered_ids,
            "missing_ids": missing_ids,
            "issues": issues,
            "total": len(self.risk_ids),
            "covered": len(covered_ids)
        }


def build_system_prompt() -> str:
    return """你是一位GDPR合规专家。你的任务是针对已识别的合规风险点，依据 GDPR Art.25（设计即隐私/默认隐私）
与 Art.32（处理安全的技术与组织措施）设计具体的缓解方案。

【三类措施框架（GDPR原生分类，不是通用AI安全框架）】
1. 技术措施（technical_measures）—— 对应 Art.32(1)(a)(b)：
   加密、假名化、访问控制（RBAC等）、数据最小化实现、自动化留存期限清除、传输加密（TLS/SCC技术层面）等
2. 组织措施（organizational_measures）—— 对应 Art.24/Art.28：
   数据处理协议（DPA）、DPO审核流程、员工数据保护培训、供应商/处理者合同条款、职责分离等
3. 治理措施（governance_measures）—— 对应 Art.5(2)问责原则、Art.32(1)(d)：
   审计日志、数据泄露事件响应流程、定期复审与测试（Art.32要求的有效性定期评估）、处理活动记录（ROPA）更新等

【约束】
- 每项措施必须具体、可执行，不能是空话（比如"加强安全管理"这种不合格，要写成"对候选人身份证号字段实施AES-256加密存储"这种）
- 措施必须与风险类型直接相关，禁止无关措施
- 严禁使用AI模型安全/红队语境下的措施（如"对抗训练"、"RLHF安全对齐"、"内容分类器拦截输出"），这里是GDPR数据保护语境，不是AI系统本身的安全加固

【输出格式（严格JSON，字段类型必须完全匹配）】
{
  "mitigations": [
    {
      "risk_id": "R001",
      "risk_description": "风险描述",
      "technical_measures": ["对身份证号字段实施加密存储", "访问权限收敛到最小必要角色"],
      "organizational_measures": ["与数据处理方签订Art.28要求的DPA条款"],
      "governance_measures": ["建立数据访问审计日志，留存6个月以上"]
    }
  ]
}

注意：
- technical_measures / organizational_measures / governance_measures 这三个字段必须是**字符串数组**，即使只有一条措施也要写成 ["措施内容"] 而不是直接写成字符串 "措施内容"
- 每类至少1项具体可执行的措施，不能为空数组
- 每个风险点这三个字段都必须齐全，不能省略任何一类
"""


def run_mitigation_harness(risk_file="outputs/risk_identification.json", max_rounds=None):
    print("\n" + "=" * 60)
    print("🛡️ 阶段三：防御方案模块启动")
    print("=" * 60)

    if not os.path.exists(risk_file):
        print(f"❌ 未找到风险识别文件: {risk_file}")
        return None

    with open(risk_file, 'r', encoding='utf-8') as f:
        risk_data = json.load(f)

    risk_points = risk_data.get("risk_points", [])
    project_name = risk_data.get("project_name", "未命名项目")

    if not risk_points:
        print("❌ 风险列表为空")
        return None

    print(f"📋 收到 {len(risk_points)} 个风险点")

    config = yaml.safe_load(open("config.yaml", 'r', encoding='utf-8'))
    if max_rounds is None:
        max_rounds = config.get("harness", {}).get("max_rounds", 3)

    executor = Executor()
    ledger = Ledger("mitigation_design", max_rounds=max_rounds, model=executor.model, corpus_version="Art.25/32 框架")
    brain = Brain(max_rounds=max_rounds)
    observer = MitigationObserver(risk_points)
    system_prompt = build_system_prompt()

    try:
        with open("schemas/mitigation_schema.json", 'r', encoding='utf-8') as f:
            mitigation_schema = json.load(f)
    except FileNotFoundError:
        mitigation_schema = None

    current_round = 0
    final_output = None

    while current_round < max_rounds:
        current_round += 1
        print(f"\n📍 第 {current_round}/{max_rounds} 轮")

        user_prompt = f"""请为以下风险点设计缓解方案（技术措施/组织措施/治理措施三类）：

{json.dumps(risk_points, indent=2, ensure_ascii=False)}

请返回包含 "mitigations" 键的JSON对象。
"""

        print("   ⏳ 调用大模型...")
        output = executor.execute(system_prompt, user_prompt, mitigation_schema)

        if not output:
            print("   ⚠️ 输出为空，重试...")
            continue

        output["project_name"] = project_name  # 代码强制写入，不依赖模型复述

        if mitigation_schema:
            is_valid, schema_error = validate_schema(output, mitigation_schema)
            if not is_valid:
                print(f"   ⚠️ Schema校验失败，重试: {schema_error}")
                continue

        ledger.log_round(current_round, {"risk_ids": [r.get("risk_id") for r in risk_points]}, output, {}, {})
        observation = observer.observe(output)
        print(f"   📊 已覆盖: {observation['covered']}/{observation['total']} 个风险点")

        decision = brain.decide(observation, current_round, output)
        print(f"   🧠 决策: {decision['action']}")

        if observation['status'] == "complete":
            final_output = output
            break
        else:
            final_output = output

    if final_output:
        output_dir = config.get("output", {}).get("dir", "./outputs")
        os.makedirs(output_dir, exist_ok=True)
        output_path = os.path.join(output_dir, "mitigation_measures.json")
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(final_output, f, indent=2, ensure_ascii=False)
        print(f"📁 防御方案已保存: {output_path}")
        ledger.save_to_file(os.path.join(output_dir, "ledger_mitigation.json"))
        return final_output
    else:
        print("❌ 未能完成防御方案设计")
        return None
