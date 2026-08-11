# harness/brain.py
# 决策者：根据观察结果决定下一步行动

from typing import Dict


class Brain:
    """根据Observer的反馈做决策"""

    DOMAIN_HINTS = {
        "data": "数据层风险（过度收集、数据来源合法性、敏感数据处理）",
        "algorithm": "算法层风险（黑盒决策、算法歧视、缺乏可解释性）",
        "user_rights": "用户权利层风险（退出机制、删除权、解释权）",
        "operation_governance": "运营治理层风险（持续监控、审计日志、监管对接）"
    }

    def __init__(self, max_rounds: int = 3):
        self.max_rounds = max_rounds

    def decide(self, observation: Dict, current_round: int, outputs: Dict) -> Dict:
        if current_round >= self.max_rounds:
            return {
                "action": "stop",
                "reason": f"达到最大迭代轮次 ({self.max_rounds}轮)"
            }

        status = observation.get("status")

        if status == "empty":
            return {
                "action": "continue",
                "reason": "未识别出任何风险点，需重新生成",
                "next_focus": "all",
                "hint": "请从数据层、算法层、用户权利层、运营治理层四个维度全面识别风险"
            }

        if status == "complete":
            return {
                "action": "stop",
                "reason": "已覆盖全部4个风险维度，识别完整"
            }

        if status == "partial":
            missing = observation.get("missing_domains", [])
            next_focus = missing[0] if missing else "all"
            hint = self.DOMAIN_HINTS.get(next_focus, f"请重点关注{next_focus}领域的风险")

            return {
                "action": "continue",
                "reason": f"当前覆盖不完整，缺失领域: {', '.join(missing)}",
                "next_focus": next_focus,
                "hint": hint
            }

        return {
            "action": "stop",
            "reason": f"未知状态 ({status})，安全停止"
        }
