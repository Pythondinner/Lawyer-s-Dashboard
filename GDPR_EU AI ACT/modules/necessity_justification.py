# modules/necessity_justification.py
# 必要性论证模块 - 自循环Harness

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


class NecessityObserver(Observer):
    def __init__(self, risk_ids):
        super().__init__()
        self.risk_ids = risk_ids

    def observe(self, outputs):
        if isinstance(outputs, list):
            justifications = outputs
        elif isinstance(outputs, dict) and "justifications" in outputs:
            justifications = outputs["justifications"]
        else:
            justifications = outputs if isinstance(outputs, list) else []

        covered_ids = [j.get("risk_id") for j in justifications if j.get("risk_id")]
        missing_ids = [rid for rid in self.risk_ids if rid not in covered_ids]

        issues = []
        for j in justifications:
            if not j.get("reasoning") or len(j.get("reasoning", "")) < 10:
                issues.append(f"风险 {j.get('risk_id')} 的论证理由不充分")

        return {
            "status": "complete" if not missing_ids and not issues else "partial",
            "covered_ids": covered_ids,
            "missing_ids": missing_ids,
            "issues": issues,
            "total": len(self.risk_ids),
            "covered": len(covered_ids)
        }


def build_system_prompt() -> str:
    return """你是一位AI合规风险评估专家。你的任务是针对已识别的合规风险点，论证其处理活动的必要性。

【输出格式（严格JSON，字段类型必须完全匹配）】
{
  "project_name": "项目名称",
  "justifications": [
    {
      "risk_id": "R001",
      "is_necessary": true,
      "reasoning": "详细理由，不少于10字",
      "alternatives_considered": ["替代方案1", "替代方案2"],
      "proportionality": "相称性评估文字说明"
    }
  ]
}

注意：
- risk_id 必须与传入的风险点ID完全一致（格式 R001, R002...）
- is_necessary 必须是布尔值 true/false，不能是字符串
- alternatives_considered 必须是**字符串数组**，即使只有1个替代方案也要写成 ["方案内容"]，不能直接写成字符串
- 每个传入的风险点都必须有对应的 justification，不能遗漏
"""


def run_necessity_harness(risk_file="outputs/risk_identification.json", max_rounds=None):
    print("\n" + "=" * 60)
    print("🔍 启动必要性论证模块")
    print("=" * 60)

    with open(risk_file, 'r', encoding='utf-8') as f:
        risk_data = json.load(f)

    risk_points = risk_data.get("risk_points", [])
    risk_ids = [r.get("risk_id") for r in risk_points]
    project_name = risk_data.get("project_name", "未命名项目")

    if not risk_ids:
        print("❌ 风险列表为空")
        return None

    print(f"📋 收到 {len(risk_ids)} 个风险点")

    with open("config.yaml", 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    if max_rounds is None:
        max_rounds = config.get("harness", {}).get("max_rounds", 3)

    executor = Executor()
    ledger = Ledger("necessity_justification", max_rounds=max_rounds, model=executor.model)
    observer = NecessityObserver(risk_ids)
    brain = Brain(max_rounds=max_rounds)

    try:
        with open("schemas/necessity_schema.json", 'r', encoding='utf-8') as f:
            necessity_schema = json.load(f)
    except FileNotFoundError:
        necessity_schema = None

    system_prompt = build_system_prompt()

    current_round = 0
    final_output = None

    while current_round < max_rounds:
        current_round += 1
        print(f"\n📍 第 {current_round}/{max_rounds} 轮")

        user_prompt = f"""请对以下风险点进行必要性论证：

{json.dumps(risk_points, indent=2, ensure_ascii=False)}

请返回包含 "justifications" 键的JSON对象。
"""

        print("   ⏳ 调用大模型...")
        output = executor.execute(system_prompt, user_prompt, necessity_schema)

        if not output:
            print("   ⚠️ 输出为空，重试...")
            continue

        output["project_name"] = project_name  # 代码强制写入，不依赖模型复述

        if necessity_schema:
            is_valid, schema_error = validate_schema(output, necessity_schema)
            if not is_valid:
                print(f"   ⚠️ Schema校验失败，重试: {schema_error}")
                continue

        ledger.log_round(current_round, {"risk_ids": risk_ids}, output, {}, {})
        observation = observer.observe(output)
        print(f"   📊 已论证: {observation['covered']}/{observation['total']} 个")

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
        output_path = os.path.join(output_dir, "necessity_justification.json")
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(final_output, f, indent=2, ensure_ascii=False)
        print(f"\n📁 必要性论证已保存: {output_path}")
        ledger.save_to_file(os.path.join(output_dir, "ledger_necessity.json"))
        return final_output
    else:
        print("❌ 未能完成必要性论证")
        return None
