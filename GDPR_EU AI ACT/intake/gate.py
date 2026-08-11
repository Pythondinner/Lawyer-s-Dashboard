# intake/gate.py
# 阶段0：适用性判断网关 —— 判断 GDPR / AI Act 两个引擎是否需要运行
# 设计依据：REBUILD_DESIGN.md 第2节

from typing import Dict, Any, List


GATE_FIELDS: List[Dict[str, Any]] = [
    {
        "key": "serves_eu_individuals",
        "module": "gate",
        "type": "bool",
        "question": "这个系统的输出或服务，是否面向欧盟境内的个人（不管你们公司注册在哪）？",
        "feeds": ["gdpr", "ai_act"],
    },
    {
        "key": "processes_personal_data",
        "module": "gate",
        "type": "bool",
        "question": "这个系统是否处理个人数据？",
        "feeds": ["gdpr"],
    },
    {
        "key": "is_ai_system",
        "module": "gate",
        "type": "bool",
        "question": "这个系统是否基于机器学习、逻辑规则或统计方法等技术自主生成输出（比如预测、内容、建议、决策），并以此影响物理或虚拟环境？",
        "feeds": ["ai_act"],
    },
]


def evaluate_gate(facts: Dict[str, Any]) -> Dict[str, Any]:
    """
    根据阶段0要件的取值，判断GDPR/AI Act是否适用。
    facts: {key: True/False/None}，None 表示尚未知道。
    返回: {"gdpr_applicable": True/False/None, "ai_act_applicable": True/False/None, "reasons": [...]}
    """
    serves_eu = facts.get("serves_eu_individuals")
    processes_pd = facts.get("processes_personal_data")
    is_ai = facts.get("is_ai_system")

    result: Dict[str, Any] = {
        "gdpr_applicable": None,
        "ai_act_applicable": None,
        "reasons": [],
    }

    if serves_eu is False:
        result["gdpr_applicable"] = False
        result["ai_act_applicable"] = False
        result["reasons"].append("不面向欧盟境内个人，GDPR（Art.3）与AI Act（Art.2）域外效力均不触发")
        return result

    if serves_eu is None:
        # 门控要件未知，两个引擎的适用性都还不能下结论
        return result

    # serves_eu is True，继续看各自的适用前提
    if processes_pd is False:
        result["gdpr_applicable"] = False
        result["reasons"].append("不处理个人数据，GDPR不适用")
    elif processes_pd is True:
        result["gdpr_applicable"] = True

    if is_ai is False:
        result["ai_act_applicable"] = False
        result["reasons"].append("不构成AI Act Art.3(1)定义下的AI系统，AI Act不适用")
    elif is_ai is True:
        result["ai_act_applicable"] = True

    return result


def is_gate_resolved(facts: Dict[str, Any]) -> bool:
    """判断阶段0是否已经能确定两个引擎各自跑不跑（不要求所有要件都有值——一旦能短路判定就算resolved）"""
    result = evaluate_gate(facts)
    return result["gdpr_applicable"] is not None and result["ai_act_applicable"] is not None
