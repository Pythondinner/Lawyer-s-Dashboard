# intake/profile_template.py
# 阶段1：统一系统画像字段定义 —— GDPR 和 AI Act 两个引擎共享同一份画像
# 设计依据：REBUILD_DESIGN.md 第3节。注意：这里只定义两引擎共享的"系统画像"字段，
# 每个枢纽自己需要的细粒度法律要件（如Art.22的三要件）不在这里，由对应枢纽模块单独采集。

from typing import List, Dict, Any


PROFILE_FIELDS: List[Dict[str, Any]] = [
    # ---- 模块A：主体与角色 ----
    {
        "key": "system_name", "module": "A", "type": "text",
        "question": "这个系统叫什么名字，或者有没有一个项目代号？",
        "feeds": ["gdpr", "ai_act"],
    },
    {
        "key": "core_function", "module": "A", "type": "text",
        "question": "这个系统的核心功能是什么？一句话说明它是做什么的。",
        "feeds": ["gdpr", "ai_act"],
    },
    {
        "key": "role", "module": "A", "type": "enum",
        "enum_options": ["提供者", "部署者", "进口商", "分销商"],
        "question": "贵司在这个系统里是什么角色？是自己开发投放市场（提供者），还是采购别人的系统来用（部署者）？",
        "feeds": ["ai_act", "gdpr"],
    },
    {
        "key": "lifecycle_stage", "module": "A", "type": "text",
        "question": "这个系统现在处于什么阶段？（概念、开发中、测试中、已上市、已部署，也可以自由描述）",
        "feeds": ["ai_act"],
    },

    # ---- 模块B：个人数据处理 ----
    {
        "key": "data_types", "module": "B", "type": "text_array",
        "question": "这个系统具体会处理哪些类型的数据？",
        "feeds": ["gdpr", "ai_act"],
    },
    {
        "key": "special_category_data", "module": "B", "type": "bool",
        "question": "处理的数据里是否包含健康、生物特征、种族、宗教信仰、政治观点这类敏感类别数据？",
        "feeds": ["gdpr", "ai_act"],
    },
    {
        "key": "data_subjects", "module": "B", "type": "text",
        "question": "这个系统主要处理的是谁的数据？（公众用户、员工、儿童、患者等）",
        "feeds": ["gdpr", "ai_act"],
    },
    {
        "key": "data_source", "module": "B", "type": "text",
        "question": "这些数据是怎么来的？直接向用户收集、公开网络抓取，还是从第三方购买？",
        "feeds": ["gdpr"],
    },

    # ---- 模块C：AI系统功能与自动化程度 ----
    {
        "key": "use_case_category", "module": "C", "type": "enum",
        "enum_options": [
            "生物识别", "关键基础设施", "教育职业培训", "就业与工作管理",
            "基本公私服务准入", "执法", "移民庇护边境管理", "司法与民主程序", "均不落入",
        ],
        "question": "这个系统的用途最接近下面哪一类？生物识别、关键基础设施、教育职业培训、就业与工作管理、"
                     "基本公私服务准入、执法、移民庇护边境管理、司法与民主程序，还是都不属于？",
        "feeds": ["ai_act"],
    },
    {
        "key": "automated_decision_exists", "module": "C", "type": "bool",
        "question": "这个系统的输出会不会在没有人工复核的情况下，直接触发一个对个人有实质影响的业务动作"
                     "（比如拒绝一笔申请、下调额度、录用与否）？",
        "feeds": ["gdpr", "ai_act"],
    },

    # ---- 模块D：数据流与地域 ----
    {
        "key": "data_location", "module": "D", "type": "text",
        "question": "数据存储和处理主要在哪里？（欧盟境内、境外，还是两者都有）",
        "feeds": ["gdpr", "ai_act"],
    },
    {
        "key": "cross_border_transfer", "module": "D", "type": "bool",
        "question": "数据是否会从欧盟传输到欧盟以外的国家？",
        "feeds": ["gdpr"],
    },

    # ---- 模块E：已有保障与文档 ----
    {
        "key": "existing_protections", "module": "E", "type": "text",
        "question": "目前已经实施了哪些数据保护/安全措施？如果没有可以直接说'无'。",
        "feeds": ["gdpr"],
    },
    {
        "key": "existing_docs", "module": "E", "type": "text",
        "question": "目前已经有哪些合规文档？（隐私政策、已做过的DPIA、技术文档等，没有可以说'无'）",
        "feeds": ["gdpr", "ai_act"],
    },
    {
        "key": "has_dpia_done", "module": "E", "type": "bool",
        "question": "是否已经做过正式的数据保护影响评估（DPIA）？",
        "feeds": ["gdpr", "ai_act"],
    },
]


def fields_for_engine(engine: str) -> List[Dict[str, Any]]:
    """返回喂给指定引擎（'gdpr' / 'ai_act'）的画像字段"""
    return [f for f in PROFILE_FIELDS if engine in f.get("feeds", [])]
