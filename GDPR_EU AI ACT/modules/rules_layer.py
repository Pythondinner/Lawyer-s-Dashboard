# modules/rules_layer.py
# 规则层：风险分级 + 角色义务查表
# 这一层现在是纯规则型枢纽（Art.5+AnnexIII+Art.50风险分级、角色→义务清单），
# 结论由代码从已知事实机械推导，不再需要LLM"匹配"——LLM的判断留给下一层（分析层）对每条已知义务做差距分析。
# 修复的硬伤：旧版无论输入什么，风险等级和义务清单都是硬编码的"提供者+高风险+Art.9-19"。

from modules.ai_act_hubs import classify_risk_tier, obligations_for, ARTICLE_TITLES


def run_rules_layer(human_input: dict) -> dict:
    print("=" * 60)
    print("⚖️ 规则层：风险分级 + 角色义务查表")
    print("=" * 60)

    use_case_category = human_input.get("scenario", "")
    tier_result = classify_risk_tier(use_case_category=use_case_category)
    role = human_input.get("role", "")

    print(f"   📊 风险分级: {tier_result['tier']}（{tier_result['basis']}）")

    system_context = {
        "name": human_input.get('system_name', '未提供'),
        "function": human_input.get('core_function', '未提供'),
        "stage": human_input.get('stage', '未提供'),
    }
    existing_docs = human_input.get('existing_docs', [])
    if isinstance(existing_docs, str):
        existing_docs = [d.strip() for d in existing_docs.split('、') if d.strip()] if existing_docs else []

    if tier_result["tier"] != "高风险":
        print(f"   ℹ️ 非高风险场景，跳过Art.9-19/26/27这套义务清单匹配")
        return {
            "risk_level": tier_result["tier"],
            "risk_basis": tier_result["basis"],
            "role": role,
            "obligations": [],
            "system_context": system_context,
            "existing_docs": existing_docs,
        }

    required_articles = obligations_for(role, tier_result["tier"])
    print(f"   📊 角色「{role}」→ 适用条款: {required_articles}")

    if not required_articles:
        print(f"   ⚠️ 角色「{role}」不在已知的四类角色（提供者/部署者/进口商/分销商）范围内，无法查表")
        return None

    obligations = [{"article": a, "title": ARTICLE_TITLES.get(a, a)} for a in required_articles]

    print(f"\n✅ 规则层完成，角色={role}，风险等级={tier_result['tier']}，匹配 {len(obligations)} 条义务")

    return {
        "risk_level": tier_result["tier"],
        "risk_basis": tier_result["basis"],
        "role": role,
        "obligations": obligations,
        "system_context": system_context,
        "existing_docs": existing_docs,
    }
