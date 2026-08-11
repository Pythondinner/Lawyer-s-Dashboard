# harness/observer.py
# 观察者：检查当前输出是否满足要求

from typing import Dict


class Observer:
    """检查输出完整性，决定是否需要继续迭代"""

    REQUIRED_DOMAINS = ["data", "algorithm", "user_rights", "operation_governance"]

    def __init__(self):
        pass

    def observe(self, outputs: Dict) -> Dict:
        risk_points = outputs.get("risk_points", [])

        if not risk_points:
            return {
                "status": "empty",
                "coverage": {domain: False for domain in self.REQUIRED_DOMAINS},
                "missing_domains": self.REQUIRED_DOMAINS.copy(),
                "issues": ["未识别出任何风险点"]
            }

        covered_domains = set()
        for point in risk_points:
            domain = point.get("risk_domain")
            if domain:
                covered_domains.add(domain)

        missing_domains = [d for d in self.REQUIRED_DOMAINS if d not in covered_domains]
        status = "complete" if not missing_domains else "partial"

        issues = []
        for idx, point in enumerate(risk_points):
            required_fields = ["risk_id", "risk_domain", "risk_description", "law_articles"]
            for field in required_fields:
                if not point.get(field):
                    issues.append(f"风险点 {idx + 1} 缺少字段: {field}")
                    break

        return {
            "status": status,
            "coverage": {d: d in covered_domains for d in self.REQUIRED_DOMAINS},
            "missing_domains": missing_domains,
            "issues": issues,
            "risk_count": len(risk_points)
        }

    def is_satisfied(self, observation: Dict) -> bool:
        return observation.get("status") == "complete"
