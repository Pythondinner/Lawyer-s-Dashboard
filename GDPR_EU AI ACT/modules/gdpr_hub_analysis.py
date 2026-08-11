# modules/gdpr_hub_analysis.py
# 从 scenario_text 抽取各枢纽所需的细粒度事实，跑一遍枢纽判定逻辑，产出结构化结论。
# 设计依据：REBUILD_DESIGN.md 第4.1节 + 第12节——LLM只负责"从文本里读出了什么"，
# 代码负责"这个读出的结果算不算数、下什么结论"。

from typing import Dict, Any
from harness import Executor
from modules.gdpr_hubs import (
    DPIA_NINE_FACTORS, dpia_threshold,
    automated_decision_test,
    lawful_basis_screen,
    transfer_mechanism_check,
    processor_relationship_check,
)


def build_extraction_prompt() -> str:
    dpia_fields = "\n".join(f"- {key}（{label}）：true/false/null" for key, label in DPIA_NINE_FACTORS)
    return f"""你是一位合规事实抽取助手，任务是从下面的业务场景描述中提取事实，用于后续的确定性法律判定。
只抽取场景描述里明确提到或能直接推断的信息，不要编造、不要凭常识猜测；无法判断的字段填 null。

【DPIA九因素（WP248指引）】
{dpia_fields}

【Art.22自动化决策三要件】
- decision_exists（是否存在针对个人的实质性决定，而非仅提供信息）：true/false/null
- human_involvement_level（人工介入程度）：只能是 "无" / "形式性" / "实质性" / null
- effect_significance（影响重大性）：只能是 "无实质影响" / "财务影响" / "就业教育机会" / "服务资格" / "法律地位变化" / null

【Art.6合法性基础五项初筛】
- has_contract_with_data_subject / has_legal_obligation / is_vital_interest / is_public_task / has_explicit_consent：均为 true/false/null

【跨境传输机制】
- cross_border_transfer：true/false/null
- destination_has_adequacy_decision：true/false/null
- has_scc_or_bcr：true/false/null

【Art.28处理者关系】
- uses_third_party_processing（是否使用第三方AI/云服务处理个人数据）：true/false/null
- third_party_is_independent_controller：true/false/null
- has_dpa_in_place：true/false/null

【输出格式（严格JSON，字段名必须完全一致）】
{{
  "dpia_factors": {{"scoring_profiling": true, "automated_decision_legal_effect": true, "systematic_monitoring": null, "sensitive_data": false, "large_scale": null, "data_matching": null, "vulnerable_subjects": null, "innovative_technology": null, "prevents_right_or_service": null}},
  "automated_decision": {{"decision_exists": true, "human_involvement_level": "无", "effect_significance": "就业教育机会"}},
  "lawful_basis": {{"has_contract_with_data_subject": false, "has_legal_obligation": false, "is_vital_interest": false, "is_public_task": false, "has_explicit_consent": false}},
  "transfer": {{"cross_border_transfer": true, "destination_has_adequacy_decision": false, "has_scc_or_bcr": false}},
  "processor": {{"uses_third_party_processing": true, "third_party_is_independent_controller": false, "has_dpa_in_place": false}}
}}
"""


def _clean_bool(value):
    return value if isinstance(value, bool) else None


def analyze_gdpr_hubs(scenario_text: str, known_facts: Dict[str, Any] = None) -> Dict[str, Any]:
    """从scenario_text抽取事实并跑一遍全部枢纽判定。最终结论全部由代码机械推导，LLM只负责抽取事实。

    known_facts: 阶段1 intake 已经问过、用户直接确认过的事实（目前支持 automated_decision_exists）。
    这些是硬约束，不该再让枢纽抽取重新猜一遍已经问过的问题——之前发现过一次真实bug：
    intake 里用户明确回答"存在无人工介入且有实质影响的自动化决策"=True，但枢纽抽取独立重新读一遍
    scenario_text 时给出了更保守的"无实质影响"，导致 Art.22 被误判为不适用，进而漏掉了本该触发的
    Art.22↔Art.14 交叉引用。已知事实应该覆盖掉这次独立抽取的保守默认值。
    """
    known_facts = known_facts or {}
    executor = Executor()
    system_prompt = build_extraction_prompt()

    try:
        output = executor.execute(system_prompt, scenario_text, schema={"type": "object"}) or {}
    except Exception:
        output = {}

    dpia_raw = output.get("dpia_factors", {})
    dpia_factors = {k: _clean_bool(dpia_raw.get(k) if isinstance(dpia_raw, dict) else None) for k, _ in DPIA_NINE_FACTORS}

    ad_raw = output.get("automated_decision", {})
    ad_raw = ad_raw if isinstance(ad_raw, dict) else {}
    decision_exists = _clean_bool(ad_raw.get("decision_exists")) or False
    human_involvement = ad_raw.get("human_involvement_level")
    if human_involvement not in ("无", "形式性", "实质性"):
        human_involvement = "实质性"  # 不确定时保守判定为"不触发"
    effect_sig = ad_raw.get("effect_significance")
    if effect_sig not in ("无实质影响", "财务影响", "就业教育机会", "服务资格", "法律地位变化", "有实质影响（未细分类别）"):
        effect_sig = "无实质影响"  # 不确定时保守判定为"不触发"

    # 硬约束覆盖：intake 已经明确问过"是否存在无人工介入且有实质影响的自动化决策"，
    # 这个已知事实比枢纽抽取的二次猜测更可信，不允许被保守默认值悄悄推翻
    known_automated_decision = _clean_bool(known_facts.get("automated_decision_exists"))
    if known_automated_decision is True:
        decision_exists = True
        human_involvement = "无"
        if effect_sig == "无实质影响":  # 只在抽取本身没找到更具体类别时兜底，保留LLM抽到的更精确分类
            effect_sig = "有实质影响（未细分类别）"
    elif known_automated_decision is False:
        decision_exists = False

    lb_raw = output.get("lawful_basis", {})
    lb_raw = lb_raw if isinstance(lb_raw, dict) else {}
    lb_kwargs = {
        k: bool(_clean_bool(lb_raw.get(k)))
        for k in ["has_contract_with_data_subject", "has_legal_obligation", "is_vital_interest",
                   "is_public_task", "has_explicit_consent"]
    }

    tr_raw = output.get("transfer", {})
    tr_raw = tr_raw if isinstance(tr_raw, dict) else {}
    cross_border = _clean_bool(tr_raw.get("cross_border_transfer")) or False
    adequacy = _clean_bool(tr_raw.get("destination_has_adequacy_decision"))
    scc = _clean_bool(tr_raw.get("has_scc_or_bcr"))

    pr_raw = output.get("processor", {})
    pr_raw = pr_raw if isinstance(pr_raw, dict) else {}
    uses_tp = _clean_bool(pr_raw.get("uses_third_party_processing")) or False
    tp_controller = _clean_bool(pr_raw.get("third_party_is_independent_controller"))
    has_dpa = _clean_bool(pr_raw.get("has_dpa_in_place"))

    return {
        "dpia_threshold": dpia_threshold(dpia_factors),
        "automated_decision": automated_decision_test(decision_exists, human_involvement, effect_sig),
        "lawful_basis": lawful_basis_screen(**lb_kwargs),
        "transfer_mechanism": transfer_mechanism_check(cross_border, adequacy, scc),
        "processor_relationship": processor_relationship_check(uses_tp, tp_controller, has_dpa),
    }


def summarize_for_prompt(hub_conclusions: Dict[str, Any]) -> str:
    """把枢纽结论整理成一段文字，注入下游LLM的system prompt作为已确定的事实依据，防止LLM自相矛盾"""
    d = hub_conclusions
    lines = [
        f"- DPIA门槛（Art.35(1)+WP248）：{'需要做DPIA' if d['dpia_threshold']['dpia_required'] else '未达到强制门槛'}（{d['dpia_threshold']['basis']}）",
        f"- 自动化决策（Art.22）：{d['automated_decision']['conclusion']}",
        f"- 合法性基础（Art.6）：{d['lawful_basis']['basis'] or '未找到规则型基础，需正当利益权衡（专业判断，需人工复核）'}",
        f"- 跨境传输机制（Ch.V）：{d['transfer_mechanism']['status']}（{d['transfer_mechanism']['basis']}）",
        f"- 处理者关系（Art.28）：{d['processor_relationship']['status']}（{d['processor_relationship']['basis']}）",
    ]
    return "\n".join(lines)
