# modules/analysis_layer.py
# 分析层：接收规则层JSON，逐条分析合规体系缺失点
# 目录型模块（复用gap-analysis模式），只对规则层查表得到的真实义务清单做差距分析——
# 不再是固定的Art.9-19，而是按角色/风险等级动态确定的清单。

import json
from harness import Ledger, Observer, Brain, Executor


class AnalysisObserver(Observer):
    REQUIRED_FIELDS = ["article", "title", "gap", "recommendation", "priority", "law_basis"]

    def __init__(self, rule_output: dict):
        self.required_articles = [o.get("article") for o in rule_output.get("obligations", [])]

    def observe(self, outputs: dict):
        recommendations = outputs.get("recommendations", [])
        covered_articles = [r.get("article") for r in recommendations]
        missing = [a for a in self.required_articles if a not in covered_articles]

        issues = []
        for idx, rec in enumerate(recommendations):
            for field in self.REQUIRED_FIELDS:
                if not rec.get(field):
                    issues.append(f"第 {idx+1} 条缺少字段: {field}")
                    break

        return {
            "status": "complete" if not missing and not issues else "partial",
            "covered": len(covered_articles),
            "total": len(self.required_articles),
            "missing": missing,
            "issues": issues
        }


def run_analysis_layer(rule_output: dict, human_input: dict, law_texts: dict) -> dict:
    print("=" * 60)
    print("🔍 分析层：识别合规体系缺失点")
    print("=" * 60)

    obligations = rule_output.get("obligations", [])

    if not obligations:
        print(f"   ℹ️ 无适用义务清单（风险等级：{rule_output.get('risk_level', '未知')}），跳过差距分析")
        return {
            "system_name": human_input.get('system_name', '未命名'),
            "recommendations": [],
            "existing_docs": rule_output.get("existing_docs", []),
            "missing_docs": [],
        }

    ledger = Ledger("analysis_layer", max_rounds=3)
    observer = AnalysisObserver(rule_output)
    brain = Brain(max_rounds=3)
    executor = Executor()

    # 只注入这次真正用得上的条款原文，不是整份语料
    relevant_articles = [o.get("article") for o in obligations]
    law_text_block = "\n\n".join(
        law_texts.get(article, f"{article}（暂无原文语料）") for article in relevant_articles
    )

    system_prompt = f"""
你是一位EU AI Act合规体系建设专家。

【用户系统信息】
- 系统名称：{human_input.get('system_name', '未提供')}
- 核心功能：{human_input.get('core_function', '未提供')}
- 角色：{rule_output.get('role', '未提供')}
- 风险等级：{rule_output.get('risk_level', '未提供')}（依据：{rule_output.get('risk_basis', '')}）

【法条原文（仅本次适用的条款）】
{law_text_block}

【输出格式】
{{
  "system_name": "系统名称",
  "recommendations": [
    {{
      "article": "Art.9",
      "title": "风险管理体系",
      "gap": "已建立初步风险评估，但未覆盖上市后监测阶段",
      "recommendation": "补充上市后监测计划",
      "priority": "P1",
      "law_basis": "Art.9(2)"
    }}
  ],
  "existing_docs": ["已有文档"],
  "missing_docs": ["缺失文档"]
}}

注意：recommendations 必须覆盖【法条原文】里列出的每一条，不能遗漏，也不能编造原文里没有的条款。
"""

    user_prompt = f"请分析以下义务清单：\n{json.dumps(obligations, indent=2, ensure_ascii=False)}"

    current_round = 0
    final_output = None

    while current_round < brain.max_rounds:
        current_round += 1
        print(f"\n📍 第 {current_round}/{brain.max_rounds} 轮")

        output = executor.execute(system_prompt, user_prompt)
        if not output:
            continue

        # 代码强制过滤：只保留请求范围内的条款，防止模型凭常识加料
        # （之前实测过：模型给部署者只请求了Art.26/27，却自己额外加了5条，
        # 只检查"有没有漏"的Observer会放过这种"多加"，必须在这里兜底裁掉）
        recommendations = output.get("recommendations", [])
        filtered = [r for r in recommendations if r.get("article") in relevant_articles]
        dropped = len(recommendations) - len(filtered)
        if dropped > 0:
            print(f"   ⚠️ 过滤掉 {dropped} 条清单外的条款（模型自行添加，已丢弃）")

        # 按条款号合并去重：有些条款内部有多个子款（如Art.26有6款），
        # 模型有时会把每个子款拆成单独一条、都标同一个article号，导致报告里同一条款重复出现好几次。
        # 这里按article分组，同一条款的gap/recommendation/law_basis合并成一条，而不是丢弃信息。
        merged: dict = {}
        order = []
        for r in filtered:
            article = r.get("article")
            if article not in merged:
                merged[article] = dict(r)
                order.append(article)
            else:
                for field in ("gap", "recommendation", "law_basis"):
                    existing = merged[article].get(field, "") or ""
                    new_val = r.get(field, "") or ""
                    if new_val and new_val not in existing:
                        merged[article][field] = f"{existing}；{new_val}" if existing else new_val
        output["recommendations"] = [merged[a] for a in order]

        observation = observer.observe(output)
        print(f"   📊 已分析: {observation['covered']}/{observation['total']} 条")

        if observation['status'] == "complete":
            final_output = output
            break
        else:
            final_output = output

    if final_output:
        print(f"\n✅ 分析层完成")
        return final_output
    return None
