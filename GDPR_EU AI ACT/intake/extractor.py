# intake/extractor.py
# 每轮对话的要件抽取 —— 扫描用户这句话，尝试抽取当前所有"未知"要件的取值
# 设计依据：REBUILD_DESIGN.md 第11.2节 / 第12节
#
# 核心原则：LLM 只负责"从这句话里读出了什么"，代码负责"这个读出的结果算不算数"。
# 闭集字段（bool/enum）抽取到的值会被代码校验，不在合法取值范围内一律视为"未抽取到"，
# 不做模糊匹配，触发追问，而不是让代码去猜用户想表达哪个选项。

import json
from typing import List, Dict, Any


def build_extraction_prompt(open_fields: List[Dict[str, Any]]) -> str:
    field_specs = []
    for f in open_fields:
        spec = f"- {f['key']}（{f['question']}）"
        if f["type"] == "bool":
            spec += "：取值只能是 true / false / null（不确定则为null）"
        elif f["type"] == "enum":
            options = "、".join(f["enum_options"])
            spec += f"：取值必须是以下选项之一：{options}；不属于任何选项则为null"
        elif f["type"] == "text_array":
            spec += "：取值是字符串数组；未提及则为null"
        else:
            spec += "：取值是字符串；未提及则为null"
        field_specs.append(spec)

    fields_block = "\n".join(field_specs)

    return f"""你是一位合规访谈助手，任务是从用户这句话里提取信息，填充以下这些还未知的字段。

【待抽取字段】
{fields_block}

【规则】
1. 只根据用户这句话本身包含的信息抽取，不要编造、不要凭常识猜测
2. 一句话可能同时提到好几个字段的信息，都要抽取，不要只抽一个
3. 用户没提到的字段，不要出现在输出里，不要瞎猜
4. enum类型字段：如果用户的表达不完全匹配某个选项文字，但语义上明显对应其中一个，就映射过去；
   如果实在无法判断对应哪个，不要输出这个字段
5. 每个抽取到的字段额外给一个 confidence（0到1之间的浮点数），表示你对这次抽取结果的把握程度

【输出格式（严格JSON）】
{{
  "extracted": {{
    "字段key": {{"value": 抽取到的值, "confidence": 0.9}}
  }}
}}

只输出 extracted 里确实从这句话抽取到信息的字段。
"""


def validate_extracted_value(field_def: Dict[str, Any], value: Any):
    """按字段类型校验抽取值是否合法，不合法返回 None（视为未抽取到，代码兜底，不信任模型自称的类型）"""
    if value is None:
        return None
    field_type = field_def["type"]

    if field_type == "bool":
        if isinstance(value, bool):
            return value
        return None

    if field_type == "enum":
        if value in field_def.get("enum_options", []):
            return value
        return None

    if field_type == "text_array":
        if isinstance(value, list) and value and all(isinstance(v, str) and v.strip() for v in value):
            return [v.strip() for v in value]
        return None

    # text
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def extract_facts(
    user_input: str,
    open_fields: List[Dict[str, Any]],
    executor,
    confidence_threshold: float = 0.6,
) -> Dict[str, Dict[str, Any]]:
    """
    调用大模型，从 user_input 中抽取 open_fields 里定义的字段。
    返回: {key: {"value": ..., "confidence": ...}}，只包含真正抽取成功、通过类型校验、
    且置信度达标的字段——其余一律视为本轮未获取到，留给后续轮次继续问。
    """
    if not open_fields or not user_input or not user_input.strip():
        return {}

    system_prompt = build_extraction_prompt(open_fields)

    try:
        output = executor.execute(system_prompt, user_input, schema={"type": "object"})
    except Exception:
        return {}

    if not output or "extracted" not in output or not isinstance(output["extracted"], dict):
        return {}

    field_by_key = {f["key"]: f for f in open_fields}
    results: Dict[str, Dict[str, Any]] = {}

    for key, item in output["extracted"].items():
        if key not in field_by_key or not isinstance(item, dict):
            continue

        raw_value = item.get("value")
        try:
            confidence = float(item.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5

        valid_value = validate_extracted_value(field_by_key[key], raw_value)
        if valid_value is None or confidence < confidence_threshold:
            continue

        results[key] = {"value": valid_value, "confidence": confidence}

    return results
