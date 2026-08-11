# modules/risk_identification.py
# 风险识别模块 - 自循环Harness

import os
import json
import yaml
import jsonschema
from harness import Ledger, Observer, Brain, Executor
from modules.gdpr_hub_analysis import analyze_gdpr_hubs, summarize_for_prompt


def load_schema(schema_path: str):
    with open(schema_path, 'r', encoding='utf-8') as f:
        return json.load(f)


def validate_schema(output: dict, schema: dict):
    try:
        jsonschema.validate(instance=output, schema=schema)
        return True, None
    except jsonschema.ValidationError as e:
        return False, str(e)


def _load_known_articles():
    """加载已有真实原文的条款语料（GDPR+AI Act），用于抓幻觉引用。
    只对有语料的法律做校验；个保法/暂行办法/GB·T目前没有语料，不校验（没有依据就不该假装能验证）。"""
    known = {}
    try:
        with open("schemas/gdpr_articles.json", 'r', encoding='utf-8') as f:
            known["GDPR"] = set(json.load(f).keys())
    except FileNotFoundError:
        known["GDPR"] = set()
    try:
        with open("schemas/law_texts.json", 'r', encoding='utf-8') as f:
            known["EU AI Act"] = set(json.load(f).keys())
    except FileNotFoundError:
        known["EU AI Act"] = set()
    return known


def _base_article(article_str: str) -> str:
    """把 'Art.6(1)(f)' 这类带子款的引用归一化成 'Art.6'，避免子款细节导致误判"""
    if not article_str:
        return ""
    base = article_str.split("(")[0].strip()
    return base


def verify_citations(risk_points: list) -> list:
    """检查每个风险点的law_articles引用是否存在于已知语料，返回警告列表（不阻断流程，只做透明提示）"""
    known = _load_known_articles()
    warnings = []
    for rp in risk_points:
        for la in rp.get("law_articles", []):
            law = la.get("law")
            article = la.get("article", "")
            if law not in known or not known[law]:
                continue  # 没有该法律的语料，无法验证，跳过（不假装能验证）
            base = _base_article(article)
            if base not in known[law]:
                warnings.append(f"{rp.get('risk_id')} 引用了 {law} {article}，未在已有语料中找到对应条款，建议人工核实")
    return warnings


def build_system_prompt(hub_summary: str = None) -> str:
    hub_block = ""
    if hub_summary:
        hub_block = f"""
【已确定的枢纽判定结论（代码机械推导，不是猜测——你的风险点描述必须与这些结论一致，不能自相矛盾）】
{hub_summary}

上述结论涉及的风险点，风险描述和law_articles引用要与对应结论对齐（比如DPIA门槛判定"需要做DPIA"，就应该有一个风险点明确指出未做DPIA本身是个缺口）。
"""

    return """你是一位AI合规风险评估专家。你的任务是根据用户提供的业务场景描述，识别可能存在的合规风险点。

你需要从以下四个维度全面识别风险：
1. data: 数据层风险（过度收集、数据来源合法性、敏感数据处理）
2. algorithm: 算法层风险（黑盒决策、算法歧视、缺乏可解释性）
3. user_rights: 用户权利层风险（退出机制、删除权、解释权）
4. operation_governance: 运营治理层风险（持续监控、审计日志、监管对接）
""" + hub_block + """
【反幻觉硬性规定】
1. 禁止编造风险
2. 禁止编造法条
3. 风险必须附理由
4. 置信度诚实

【法律范围】
你只能引用以下法律法规（不得引用范围外的法律）：
- EU AI Act
- GDPR
- 《生成式人工智能服务管理暂行办法》
- 《个人信息保护法》
- GB/T 45654-2025

【输出格式（严格JSON，字段类型必须完全匹配，尤其注意 legal_basis 和 law_articles 必须是数组，不能是字符串）】
{
  "project_name": "项目名称",
  "project_description": "项目描述（10-500字）",
  "legal_basis": ["GDPR", "EU AI Act"],
  "risk_points": [
    {
      "risk_id": "R001",
      "risk_domain": "data",
      "risk_description": "风险点的具体描述",
      "risk_cause": "风险产生的根本原因",
      "law_articles": [
        {"law": "GDPR", "article": "Art.6", "description": "条款要求或内容摘要"}
      ],
      "severity": "high",
      "confidence": 0.8
    }
  ]
}

注意：
- legal_basis 必须是字符串数组，即使只引用一部法律也要写成 ["GDPR"] 而不是 "GDPR"
- law_articles 必须是对象数组，每个对象包含 law/article/description 三个字段
- risk_domain 只能取值：data / algorithm / user_rights / operation_governance
- severity 只能取值：high / medium / low
- confidence 是 0 到 1 之间的数字
"""


def build_user_prompt(scenario_text: str, focus_hint: str = None) -> str:
    prompt = f"""请分析以下业务场景的合规风险：

【业务场景】
{scenario_text}
"""
    if focus_hint:
        prompt += f"\n【重点关注】\n{focus_hint}"
    prompt += "\n\n请按JSON格式输出风险分析结果。"
    return prompt


def run_risk_harness(scenario_text: str, max_rounds: int = 3, known_facts: dict = None):
    """阶段一：风险识别

    known_facts: 阶段1 intake 已经确认过的事实（如 automated_decision_exists），
    作为硬约束传给枢纽抽取，避免枢纽独立重新猜测同一个问题得出不一致的结论。
    """
    print("=" * 60)
    print("🚀 阶段一：风险识别模块启动")
    print("=" * 60)

    config = yaml.safe_load(open("config.yaml", 'r', encoding='utf-8'))
    max_rounds = config.get("harness", {}).get("max_rounds", max_rounds)

    executor = Executor()
    ledger = Ledger("risk_identification", max_rounds=max_rounds, model=executor.model, corpus_version="gdpr_articles.json 2026-08")
    observer = Observer()
    brain = Brain(max_rounds=max_rounds)

    schema = load_schema("schemas/risk_schema.json")

    print("\n🔍 运行GDPR决策枢纽（DPIA门槛/Art.22/合法性基础/跨境传输/处理者关系）...")
    hub_conclusions = analyze_gdpr_hubs(scenario_text, known_facts=known_facts)
    hub_summary = summarize_for_prompt(hub_conclusions)
    print(hub_summary)

    output_dir_for_hubs = config.get("output", {}).get("dir", "./outputs")
    os.makedirs(output_dir_for_hubs, exist_ok=True)
    with open(os.path.join(output_dir_for_hubs, "gdpr_hub_conclusions.json"), 'w', encoding='utf-8') as f:
        json.dump(hub_conclusions, f, indent=2, ensure_ascii=False)

    system_prompt = build_system_prompt(hub_summary)

    print(f"\n📋 场景描述: {scenario_text[:80]}...")
    print(f"🔄 最大迭代轮次: {max_rounds}")
    print("\n" + "-" * 60)

    current_round = 0
    focus_hint = None
    final_output = None

    while current_round < max_rounds:
        current_round += 1
        print(f"\n📍 第 {current_round}/{max_rounds} 轮")

        user_prompt = build_user_prompt(scenario_text, focus_hint)
        print("   ⏳ 调用大模型...")
        output = executor.execute(system_prompt, user_prompt, schema)

        if not output:
            print("   ⚠️ 输出为空，重试...")
            continue

        is_valid, schema_error = validate_schema(output, schema)
        if not is_valid:
            print(f"   ⚠️ Schema校验失败，重试: {schema_error}")
            continue

        citation_warnings = verify_citations(output.get("risk_points", []))
        if citation_warnings:
            output["citation_warnings"] = citation_warnings
            for w in citation_warnings:
                print(f"   ⚠️ 引用校验: {w}")

        ledger.log_round(current_round, {"scenario": scenario_text, "focus": focus_hint}, output, {}, {})
        observation = observer.observe(output)
        print(f"   📊 识别风险: {observation.get('risk_count', 0)} 个")
        print(f"   📊 覆盖维度: {[d for d in observer.REQUIRED_DOMAINS if observation.get('coverage', {}).get(d)]}")

        decision = brain.decide(observation, current_round, output)
        print(f"   🧠 决策: {decision['action']} - {decision['reason']}")

        if observer.is_satisfied(observation):
            final_output = output
            break

        if decision.get("action") == "continue":
            focus_hint = decision.get("hint")
            if output and not final_output:
                final_output = output
        else:
            final_output = output
            break

    print("\n" + "-" * 60)
    if final_output:
        print(f"\n✅ 风险识别完成，共识别 {len(final_output.get('risk_points', []))} 个风险点")
        output_dir = config.get("output", {}).get("dir", "./outputs")
        os.makedirs(output_dir, exist_ok=True)
        final_output["audit"] = ledger.get_audit_record()  # 结论可追溯：哪个模型、哪次调用、依据哪版语料
        output_path = os.path.join(output_dir, "risk_identification.json")
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(final_output, f, indent=2, ensure_ascii=False)
        print(f"📁 风险识别结果已保存: {output_path}")
        ledger.save_to_file(os.path.join(output_dir, "ledger_risk.json"))

        # ---- 【新增】自动调用合规动作建议生成器 ----
        try:
            from modules.action_generator import generate_action_plan
            print("\n➡️  自动生成合规动作建议...")
            action_result = generate_action_plan(output_path)
            if action_result:
                print("✅ 合规动作建议已同步生成")
        except Exception as e:
            print(f"⚠️ 合规动作建议生成失败（不影响主流程）: {e}")

    else:
        print("❌ 未识别出任何风险点")

    print("=" * 60)
    return final_output
