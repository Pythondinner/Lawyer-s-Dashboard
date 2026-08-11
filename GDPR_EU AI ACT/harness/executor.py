# harness/executor.py
# 执行者：调用外部大模型

import os
import json
import yaml
from openai import OpenAI
from typing import Dict, Any, Optional


class Executor:
    """调用大模型执行推理"""

    def __init__(self, config_path: str = "config.yaml"):
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f)
        else:
            self.config = {}

        llm_config = self.config.get("llm", {})
        api_key = os.environ.get("DEEPSEEK_API_KEY") or llm_config.get("api_key")
        if not api_key:
            raise ValueError("请设置环境变量 DEEPSEEK_API_KEY")

        self.client = OpenAI(
            api_key=api_key,
            base_url=llm_config.get("base_url", "https://api.deepseek.com")
        )
        self.model = llm_config.get("model", "deepseek-v4-pro")
        self.temperature = llm_config.get("temperature", 0.3)
        self.max_tokens = llm_config.get("max_tokens", 6000)

    def execute(self, system_prompt: str, user_prompt: str, schema: Optional[Dict] = None) -> Dict[str, Any]:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]

        params = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

        if schema:
            params["response_format"] = {"type": "json_object"}

        try:
            response = self.client.chat.completions.create(**params)
            content = response.choices[0].message.content

            if content:
                content = content.strip()
                if content.startswith("```json"):
                    content = content[7:]
                if content.startswith("```"):
                    content = content[3:]
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()
                return json.loads(content)

            return {}

        except json.JSONDecodeError as e:
            print(f"⚠️ JSON解析失败: {e}")
            print(f"   原始内容: {content[:200]}...")
            return {}
        except Exception as e:
            print(f"⚠️ API调用失败: {e}")
            return {}
