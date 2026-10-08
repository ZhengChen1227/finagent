"""大模型客户端（OpenAI 兼容协议，支持 Function Calling）。

竞赛要求"至少使用一个大语言模型作为核心推理引擎，鼓励采用国产模型"。
本客户端不绑定厂商，通过 base_url 即可对接 DeepSeek、通义千问、智谱 GLM、
Kimi 等国产模型的 OpenAI 兼容接口。

未配置 API Key 时，上层编排循环会自动切换为确定性离线模式，
保证评审在无密钥环境下仍能复现完整任务流程与输出结果。
"""

from __future__ import annotations

import json
import re

import httpx


class LLMClient:
    def __init__(self, cfg: dict) -> None:
        conf = cfg.get("llm", {})
        self.base_url = (conf.get("base_url") or "").rstrip("/")
        self.api_key = conf.get("api_key") or ""
        self.model = conf.get("model") or ""
        self.temperature = float(conf.get("temperature", 0.2))
        self.timeout = float(conf.get("timeout", 120))
        self._calls = 0
        self._usage = {"prompt_tokens": 0, "completion_tokens": 0}

    @property
    def available(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)

    @property
    def calls(self) -> int:
        return self._calls

    @property
    def usage(self) -> dict:
        return dict(self._usage)

    # ---------------- 底层调用 ----------------

    def chat_messages(self, messages: list, tools: list | None = None,
                      json_mode: bool = False) -> dict:
        """发起一次对话，返回原始 message 对象（可能含 tool_calls）。"""
        if not self.available:
            raise RuntimeError("未配置大模型（缺少 base_url / api_key / model）")
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        with httpx.Client(timeout=self.timeout) as client:
            resp = client.post(f"{self.base_url}/chat/completions",
                               json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
        self._calls += 1
        usage = data.get("usage") or {}
        for key in ("prompt_tokens", "completion_tokens"):
            self._usage[key] = self._usage.get(key, 0) + int(usage.get(key, 0) or 0)
        return data["choices"][0]["message"]

    # ---------------- 便捷接口 ----------------

    def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        msg = self.chat_messages(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            json_mode=json_mode,
        )
        return msg.get("content") or ""

    def chat_json(self, system: str, user: str) -> dict:
        return parse_json_loose(self.chat(system, user, json_mode=True))


def parse_json_loose(text: str) -> dict:
    """宽松 JSON 解析：兼容模型输出被 ``` 包裹或夹带说明文字的情况。"""
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            pass
    return {}
