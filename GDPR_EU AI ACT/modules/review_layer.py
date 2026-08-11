# modules/review_layer.py
# 审核层：校验分析层输出的格式合法性

import json
import jsonschema
import os
from harness import Ledger, Observer, Brain, Executor


class ReviewObserver(Observer):
    def __init__(self, schema: dict):
        self.schema = schema

    def observe(self, outputs: dict):
        try:
            jsonschema.validate(instance=outputs, schema=self.schema)
            return {"status": "complete", "passed": True, "errors": []}
        except jsonschema.ValidationError as e:
            return {"status": "partial", "passed": False, "errors": [str(e)]}


def load_schema() -> dict:
    schema_path = "schemas/review_schema.json"
    if os.path.exists(schema_path):
        with open(schema_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "required": ["system_name", "recommendations"],
        "properties": {
            "system_name": {"type": "string"},
            "recommendations": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["article", "title", "gap", "recommendation", "priority", "law_basis"],
                    "properties": {
                        "article": {"type": "string", "pattern": "^Art\\.\\d+$"},
                        "title": {"type": "string"},
                        "gap": {"type": "string"},
                        "recommendation": {"type": "string"},
                        "priority": {"type": "string", "enum": ["P0", "P1", "P2"]},
                        "law_basis": {"type": "string"}
                    }
                }
            }
        }
    }


def run_review_layer(analysis_output: dict) -> dict:
    print("=" * 60)
    print("✅ 审核层：格式校验")
    print("=" * 60)

    schema = load_schema()
    observer = ReviewObserver(schema)

    observation = observer.observe(analysis_output)
    print(f"   📊 校验结果: {'✅ 通过' if observation['passed'] else '❌ 不通过'}")

    if observation['status'] == "complete":
        return {"status": "approved", "result": analysis_output, "errors": []}
    else:
        return {"status": "rejected", "result": analysis_output, "errors": observation['errors']}
