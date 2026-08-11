# fusion/cross_reference_map.py
# GDPR × AI Act 交叉引用勾连表——硬编码，不是让模型自己联想。
# 设计依据：REBUILD_DESIGN.md 第6节。每一行都对应法条本身写明的关联，或同一事实的两种法律后果，
# 不是泛化的"这两个领域有点像"。

from typing import Dict, Any, List


def _dpia_vs_fria(gdpr_hubs: Dict[str, Any], ai_act: Dict[str, Any]) -> bool:
    dpia_required = gdpr_hubs.get("dpia_threshold", {}).get("dpia_required", False)
    is_high_risk = ai_act.get("risk_level") == "高风险"
    return dpia_required and is_high_risk


def _art22_vs_art14(gdpr_hubs: Dict[str, Any], ai_act: Dict[str, Any]) -> bool:
    return gdpr_hubs.get("automated_decision", {}).get("applies", False)


def _transparency_overlap(gdpr_hubs: Dict[str, Any], ai_act: Dict[str, Any]) -> bool:
    is_high_risk = ai_act.get("risk_level") in ("高风险", "有限风险")
    return is_high_risk


def _sensitive_data_vs_biometric(gdpr_hubs: Dict[str, Any], ai_act: Dict[str, Any]) -> bool:
    return ai_act.get("use_case_category") == "生物识别"


CROSS_REFERENCE_RULES: List[Dict[str, Any]] = [
    {
        "gdpr_ref": "Art.35 DPIA",
        "ai_act_ref": "Art.27 FRIA",
        "relationship": "法条明文规定可合并进行（AI Act Art.27(4)）",
        "explanation": "如果已经做过（或即将做）GDPR Art.35的数据保护影响评估（DPIA），AI Act Art.27(4)明确允许基本权利影响评估（FRIA）结合该DPIA一并进行，不用重复做一遍事实调查。",
        "trigger": _dpia_vs_fria,
    },
    {
        "gdpr_ref": "Art.22 自动化决策",
        "ai_act_ref": "Art.14 人工监督",
        "relationship": "同一事实（无实质人工介入的自动化决策）触发两部法律的不同后果",
        "explanation": "GDPR Art.22赋予数据主体拒绝纯自动化决策的权利，AI Act Art.14则要求高风险系统本身必须具备有效的人工监督机制——两者是同一个「是否有真正人工介入」的事实，分别在个人权利和系统设计两个角度提出要求。",
        "trigger": _art22_vs_art14,
    },
    {
        "gdpr_ref": "Art.13/14 透明度",
        "ai_act_ref": "Art.13/50 透明度",
        "relationship": "部分重叠，各有侧重",
        "explanation": "GDPR的透明度义务聚焦于告知数据主体处理逻辑、目的和后果；AI Act的透明度义务聚焦于系统本身的技术文档、使用说明和（有限风险场景下的）AI身份披露。两者可以共用同一份对外披露文档的基础素材，但侧重点不同，不能互相替代。",
        "trigger": _transparency_overlap,
    },
    {
        "gdpr_ref": "Art.9 敏感数据",
        "ai_act_ref": "Annex III 生物特征分类",
        "relationship": "同一数据类型触发两法不同后果",
        "explanation": "生物特征数据在GDPR下是Art.9特殊类别数据（原则禁止处理，除非符合例外），在AI Act下则可能使系统直接落入Annex III高风险清单（生物识别类）——同一类数据同时触发两部法律里最严格的两档规则。",
        "trigger": _sensitive_data_vs_biometric,
    },
]


def find_cross_references(gdpr_hubs: Dict[str, Any], ai_act: Dict[str, Any]) -> List[Dict[str, Any]]:
    """只返回真正命中触发条件的行，不做泛化联想。"""
    return [
        {k: v for k, v in rule.items() if k != "trigger"}
        for rule in CROSS_REFERENCE_RULES
        if rule["trigger"](gdpr_hubs, ai_act)
    ]
