# modules/ai_act_hubs.py
# AI Act 决策枢纽：Art.5+AnnexIII+Art.50 风险分级、角色→义务清单查表
# 设计依据：REBUILD_DESIGN.md 第5.1节
#
# 这是规则型枢纽：结论由代码从已知事实机械推导（短路逻辑），不依赖LLM自由判断。
# 修复的硬伤：旧版 rules_layer.py/main.py 无论输入什么都硬编码"提供者+高风险"。

from typing import Dict, List, Optional, Any
from datetime import date, datetime

PROHIBITED_USES = [
    ("subliminal_manipulation", "潜意识操纵"),
    ("exploit_vulnerable", "利用弱势群体"),
    ("social_scoring", "社会评分"),
    ("realtime_biometric_public", "未授权实时远程生物识别公共场所监控"),
    ("emotion_recognition_workplace_education", "工作场所/教育场所情绪识别"),
    ("biometric_categorization_sensitive", "基于敏感属性的生物特征分类"),
    ("predictive_policing_individual", "预测性警务个人画像"),
    ("untargeted_facial_scraping", "非定向抓取人脸数据库"),
]

ANNEX_III_CATEGORIES = [
    "生物识别", "关键基础设施", "教育职业培训", "就业与工作管理",
    "基本公私服务准入", "执法", "移民庇护边境管理", "司法与民主程序",
]

ROLE_OBLIGATIONS = {
    "提供者": [f"Art.{n}" for n in range(9, 20)],  # Art.9-19
    "部署者": ["Art.26", "Art.27"],
    "进口商": ["Art.23"],
    "分销商": ["Art.24"],
}


def classify_risk_tier(
    use_case_category: Optional[str],
    prohibited_flags: Optional[Dict[str, bool]] = None,
    transparency_trigger: bool = False,
) -> Dict[str, str]:
    """
    风险分级判定：禁止 > 高风险 > 有限风险 > 最小风险，短路逻辑，代码机械推导。

    - use_case_category: 来自统一intake画像的Annex III分类结果（"均不落入"表示不属于任何一类）
    - prohibited_flags: {要件key: 是否命中}，缺省视为均未命中
    - transparency_trigger: 是否命中Art.50透明度义务触发条件（与自然人交互/生成合成内容等）
    """
    prohibited_flags = prohibited_flags or {}
    hit_prohibited = [label for key, label in PROHIBITED_USES if prohibited_flags.get(key)]
    if hit_prohibited:
        return {"tier": "禁止", "basis": f"命中Art.5禁止用途：{'、'.join(hit_prohibited)}"}

    if use_case_category and use_case_category in ANNEX_III_CATEGORIES:
        return {"tier": "高风险", "basis": f"落入Annex III高风险场景：{use_case_category}"}

    if transparency_trigger:
        return {"tier": "有限风险", "basis": "触发Art.50透明度义务（与自然人交互等）"}

    return {"tier": "最小风险", "basis": "未落入禁止清单、Annex III高风险清单，也未触发Art.50透明度义务"}


def obligations_for(role: str, tier: str) -> List[str]:
    """角色→义务清单查表（纯lookup，不是新测试）。只有"高风险"层级才适用这套提供者/部署者/进口商/分销商义务体系。"""
    if tier != "高风险":
        return []
    return ROLE_OBLIGATIONS.get(role, [])


ARTICLE_TITLES = {
    "Art.9": "风险管理体系", "Art.10": "数据与数据治理", "Art.11": "技术文档",
    "Art.12": "记录保存", "Art.13": "透明度与信息提供", "Art.14": "人工监督",
    "Art.15": "准确性、稳健性与网络安全", "Art.16": "提供者义务", "Art.17": "质量管理体系",
    "Art.18": "文件保存", "Art.19": "自动生成日志",
    "Art.23": "进口商义务", "Art.24": "分销商义务",
    "Art.26": "部署者义务", "Art.27": "基本权利影响评估（FRIA）",
    "Art.5": "禁止的人工智能实践", "Art.50": "特定AI系统的透明度义务",
}


# ---------------------------------------------------------------------------
# 生效日期维度：AI Act 分阶段生效，不是一次性全部生效
# 月份级把握，实现/使用时建议对照官方文本核实到日
# ---------------------------------------------------------------------------

ARTICLE_EFFECTIVE_DATES = {
    "Art.5": "2025-02-02",  # 禁止用途
    "Art.50": "2026-08-02",  # 透明度义务（随主体条款一并生效）
    # Art.9-19（高风险提供者义务）、Art.23/24（进口商/分销商）、Art.26/27（部署者义务）均随主体条款生效
    **{f"Art.{n}": "2026-08-02" for n in range(9, 20)},
    "Art.23": "2026-08-02", "Art.24": "2026-08-02",
    "Art.26": "2026-08-02", "Art.27": "2026-08-02",
}


def effective_status(article: str, today: Optional[date] = None) -> Dict[str, Any]:
    """动态计算某条款相对于今天的生效状态——不写死，保证任何时候看这份报告时效性都是对的"""
    today = today or date.today()
    date_str = ARTICLE_EFFECTIVE_DATES.get(article)
    if not date_str:
        return {"effective_date": None, "status": "未知", "note": "该条款生效日期暂未收录"}

    eff_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    if today >= eff_date:
        return {"effective_date": date_str, "status": "已生效", "note": f"自 {date_str} 起已生效"}
    days_left = (eff_date - today).days
    return {"effective_date": date_str, "status": "尚未生效", "note": f"距生效还有 {days_left} 天（{date_str}）"}
