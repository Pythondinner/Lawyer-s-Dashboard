# modules/gdpr_hubs.py
# GDPR 决策枢纽：DPIA门槛、Art.22自动化决策、Art.6/9合法性基础、Ch.V跨境传输机制、Art.28处理者关系
# 设计依据：REBUILD_DESIGN.md 第4.1节
#
# 规则型枢纽（DPIA门槛、Art.22、跨境传输、Art.28处理者关系）：结论由代码从已知事实机械推导。
# 混合型枢纽（Art.6/9合法性基础）：前五项规则型，第六项（正当利益）权衡型，需人工复核。

from typing import Dict, Any, List, Optional


# ---------------------------------------------------------------------------
# 枢纽1：Art.35(1) + WP248 九因素 DPIA 门槛
# ---------------------------------------------------------------------------

DPIA_NINE_FACTORS = [
    ("scoring_profiling", "评分/画像"),
    ("automated_decision_legal_effect", "自动化决策产生法律效果"),
    ("systematic_monitoring", "系统性监控"),
    ("sensitive_data", "敏感数据（Art.9/犯罪定罪数据）"),
    ("large_scale", "大规模处理"),
    ("data_matching", "数据匹配或合并"),
    ("vulnerable_subjects", "涉及弱势群体（如儿童）"),
    ("innovative_technology", "使用新技术"),
    ("prevents_right_or_service", "阻碍数据主体行使权利或获得服务"),
]


def dpia_threshold(factors: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
    """WP248指引：命中≥2项九因素，通常即应做DPIA。代码机械计数，不靠LLM判断。"""
    factors = factors or {}
    hit = [label for key, label in DPIA_NINE_FACTORS if factors.get(key)]
    required = len(hit) >= 2
    return {
        "dpia_required": required,
        "hit_factors": hit,
        "hit_count": len(hit),
        "basis": f"命中{len(hit)}/9项WP248因素：{'、'.join(hit) if hit else '无'}" + ("（≥2项，需做DPIA）" if required else "（<2项，非强制，但可自愿评估）"),
    }


# ---------------------------------------------------------------------------
# 枢纽2：Art.22 自动化决策（三要件 + 例外 + 保障措施）
# ---------------------------------------------------------------------------

SIGNIFICANT_EFFECTS = {"财务影响", "就业教育机会", "服务资格", "法律地位变化", "有实质影响（未细分类别）"}


def automated_decision_test(
    decision_exists: bool,
    human_involvement_level: str,  # "无" / "形式性" / "实质性"
    effect_significance: str,  # "无实质影响" / 财务影响 / 就业教育机会 / 服务资格 / 法律地位变化 / 有实质影响（未细分类别）
    exception_basis: Optional[str] = None,  # None / "合同履行必需" / "法律授权" / "数据主体明确同意"
    safeguards_in_place: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    Art.22 三要件：决定存在性、人工介入程度（仅"无"或"形式性"才算solely automated）、影响重大性。
    三者同时成立 → 适用，除非存在例外（合同必需/法律授权/明确同意）；
    例外成立时仍需检查Art.22(3)保障措施（人工干预权/表达意见权/异议权）。
    结论由代码 if/else 机械推导。
    """
    solely_automated = human_involvement_level in ("无", "形式性")
    significant = effect_significance in SIGNIFICANT_EFFECTS

    if not (decision_exists and solely_automated and significant):
        return {
            "applies": False,
            "conclusion": "Art.22 不适用",
            "basis": f"三要件未同时满足（决定存在={decision_exists}，人工介入={human_involvement_level}，影响重大性={effect_significance}）",
        }

    if not exception_basis:
        return {
            "applies": True,
            "conclusion": "Art.22 适用，数据主体有权拒绝该自动化决策",
            "basis": "三要件同时满足，且无Art.22(2)例外事由",
        }

    if safeguards_in_place:
        return {
            "applies": True,
            "conclusion": f"Art.22 例外适用（{exception_basis}），且已落实Art.22(3)保障措施",
            "basis": f"三要件满足但存在例外事由「{exception_basis}」，保障措施已到位",
        }

    return {
        "applies": True,
        "conclusion": f"Art.22 例外适用（{exception_basis}），但尚未确认落实Art.22(3)保障措施（人工干预权/异议权/表达意见权），存在合规缺口",
        "basis": f"三要件满足，例外事由「{exception_basis}」成立，但保障措施未确认",
    }


# ---------------------------------------------------------------------------
# 枢纽3：Art.6/9 合法性基础（五项规则初筛 + 正当利益权衡）
# ---------------------------------------------------------------------------

def lawful_basis_screen(
    has_contract_with_data_subject: bool = False,
    has_legal_obligation: bool = False,
    is_vital_interest: bool = False,
    is_public_task: bool = False,
    has_explicit_consent: bool = False,
) -> Dict[str, Any]:
    """
    规则型初筛：Art.6(1)(a)-(e) 五项按顺序检查，命中第一个成立的即为结论（代码机械判定）。
    全部不成立 → 落到 (f) 正当利益，这部分是权衡型，需要另外调用 legitimate_interest_balancing。
    """
    if has_contract_with_data_subject:
        return {"basis_found": True, "basis": "Art.6(1)(b) 合同履行必需", "type": "规则型"}
    if has_legal_obligation:
        return {"basis_found": True, "basis": "Art.6(1)(c) 法律义务", "type": "规则型"}
    if is_vital_interest:
        return {"basis_found": True, "basis": "Art.6(1)(d) 重大利益（生命安全）", "type": "规则型"}
    if is_public_task:
        return {"basis_found": True, "basis": "Art.6(1)(e) 公共任务/公权力", "type": "规则型"}
    if has_explicit_consent:
        return {"basis_found": True, "basis": "Art.6(1)(a) 明确同意", "type": "规则型",
                "note": "需另外核实同意是否满足Art.7有效性四要件：自由做出、具体、知情、明确"}
    return {"basis_found": False, "basis": None, "type": "需权衡", "next": "落入Art.6(1)(f)正当利益，需三步平衡测试，权衡型，结论需标注「专业判断，建议律师复核」"}


def legitimate_interest_balancing(legitimate_interest_description: str, necessity_argument: str, balancing_factors: str) -> Dict[str, Any]:
    """
    Art.6(1)(f) 正当利益三步平衡测试：权衡型，不由代码下确定性结论，只做"论证是否有实质内容"的完整性检查，
    真正的判断留给LLM论证 + 标注"专业判断，需人工复核"。
    """
    has_substance = all([
        legitimate_interest_description and len(legitimate_interest_description.strip()) >= 10,
        necessity_argument and len(necessity_argument.strip()) >= 10,
        balancing_factors and len(balancing_factors.strip()) >= 10,
    ])
    return {
        "basis_found": has_substance,
        "basis": "Art.6(1)(f) 正当利益" if has_substance else None,
        "type": "权衡型",
        "requires_human_review": True,
        "note": "三步平衡测试的论证内容是否充分，需人工（律师）复核确认，本工具不下确定性结论",
    }


# ---------------------------------------------------------------------------
# 枢纽4：Ch.V 跨境传输机制（Art.44-49）
# ---------------------------------------------------------------------------

def transfer_mechanism_check(
    cross_border_transfer: bool,
    destination_has_adequacy_decision: Optional[bool] = None,
    has_scc_or_bcr: Optional[bool] = None,
) -> Dict[str, Any]:
    """代码机械判定：不传出欧盟→不适用；有充分性认定→Art.45合法；无认定但有SCC/BCR→Art.46合法；都没有→缺口。"""
    if not cross_border_transfer:
        return {"status": "不适用", "basis": "数据不传出欧盟，Ch.V不适用"}
    if destination_has_adequacy_decision:
        return {"status": "合法", "basis": "目的地已获欧盟委员会充分性认定（Art.45），传输合法，无需额外保障措施"}
    if has_scc_or_bcr:
        return {"status": "合法", "basis": "已采用标准合同条款（SCC）或有约束力公司规则（BCR），符合Art.46附加适当保障要求"}
    return {"status": "缺口", "basis": "数据传出欧盟，目的地无充分性认定，也未采用SCC/BCR等适当保障措施，存在Ch.V合规缺口（需核实是否符合Art.49特定情形例外，否则应尽快签署SCC）"}


# ---------------------------------------------------------------------------
# 枢纽5：Art.28 处理者关系/DPA
# ---------------------------------------------------------------------------

def processor_relationship_check(
    uses_third_party_processing: bool,
    third_party_is_independent_controller: Optional[bool] = None,
    has_dpa_in_place: Optional[bool] = None,
) -> Dict[str, Any]:
    """代码机械判定：是否使用第三方处理个人数据、该方是独立controller还是processor、是否已签DPA。"""
    if not uses_third_party_processing:
        return {"status": "不适用", "basis": "未使用第三方AI/云服务处理个人数据"}
    if third_party_is_independent_controller:
        return {"status": "需另行评估", "basis": "第三方为独立controller而非processor，Art.28不适用，但需评估是否构成联合控制者（Art.26）或需要单独的数据共享协议"}
    if has_dpa_in_place:
        return {"status": "合法", "basis": "第三方为processor，已按Art.28(3)签订数据处理协议（DPA），符合要求"}
    return {"status": "缺口", "basis": "第三方作为processor处理个人数据，但尚未签订Art.28(3)要求的数据处理协议（DPA），存在合规缺口"}
