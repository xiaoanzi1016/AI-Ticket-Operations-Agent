# -*- coding: utf-8 -*-
"""多模型接入客户端。

设计：走 OpenAI 兼容接口，模型是"插件"不是绑定。
切换模型只需改 .env（MODEL_NAME / MODEL_BASE_URL），
并可配合 evaluation 做多模型对比评测。

TODO: 对每个模型做超时、重试、token 统计（落 observability.trace）。
"""
from __future__ import annotations

from typing import Optional

from src.config import is_placeholder_key, settings
from src.logger import log


class LLMClient:
    """统一模型客户端封装。"""

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.0,
    ) -> None:
        self.api_key = api_key or settings.model_api_key
        self.base_url = base_url or settings.model_base_url
        self.model = model or settings.model_name
        self.temperature = temperature if temperature != 0 else settings.model_temperature
        # token 用量统计（评测/成本指标用）
        self.total_calls = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        # 最近一次工具调用响应里的思维链内容（DeepSeek 等"思考模式"模型的必需回传字段）。
        # 见 chat_with_tools 的说明 —— 不回传会被服务端判 400。
        self.last_reasoning_content: Optional[str] = None

    def usage_stats(self) -> dict:
        """返回累计 token 用量统计。"""
        return {
            "calls": self.total_calls,
            "prompt_tokens": self.total_prompt_tokens,
            "completion_tokens": self.total_completion_tokens,
            "total_tokens": self.total_prompt_tokens + self.total_completion_tokens,
        }

    def _record_usage(self, resp) -> None:
        """从响应里提取 usage 并累计（平台可能不返回 usage，容错）。"""
        try:
            u = resp.usage
            if u is not None:
                self.total_calls += 1
                self.total_prompt_tokens += u.prompt_tokens or 0
                self.total_completion_tokens += u.completion_tokens or 0
        except Exception:
            self.total_calls += 1  # 无 usage 也计一次调用

    def _client(self):
        """惰性创建 OpenAI 兼容客户端。"""
        try:
            from openai import OpenAI
        except ImportError as e:  # 依赖未安装
            raise RuntimeError(
                "未安装 openai 依赖。请执行: pip install -r requirements.txt"
            ) from e
        # 占位值（.env.example 里的 sk-your-key-here 之类）一律视为"没配密钥"，
        # 避免照抄样例文件后静默进入真实模型模式。见 config.is_placeholder_key。
        if is_placeholder_key(self.api_key):
            raise RuntimeError(
                "未配置有效的 MODEL_API_KEY（当前是空值或 .env.example 里的占位值）。"
                "请在 .env 中填写真实密钥，或设 AGENT_FORCE_MOCK=true 走离线演示模式。"
            )
        return OpenAI(api_key=self.api_key, base_url=self.base_url)

    def chat(self, messages: list[dict], tools: Optional[list[dict]] = None) -> str:
        """基础对话（非工具调用）。"""
        client = self._client()
        kwargs = {"messages": messages, "model": self.model, "temperature": self.temperature}
        if tools:
            kwargs["tools"] = tools
        resp = client.chat.completions.create(**kwargs)
        self._record_usage(resp)
        return resp.choices[0].message.content or ""

    def chat_with_tools(self, messages: list[dict], tools: list[dict]) -> tuple[Optional[str], Optional[dict]]:
        """带工具调用的对话。

        返回 (content, tool_call)。
        - tool_call 非空时，说明模型请求调用某个工具，由 agent 层执行。
        - 两字段通常会有一个为 None。

        关于 `last_reasoning_content`（重要，踩过的坑）：
        思考模式模型（如 deepseek-v4-flash）在返回 tool_calls 时，会同时给出
        `reasoning_content`（思维链）。**下一轮把这个 assistant 消息发回去时，
        必须原样带上 reasoning_content**，否则服务端直接判 400：
            "The `reasoning_content` in the thinking mode must be passed back to the API."
        表现是"第一轮工具调用正常，第二轮必失败"，而 agent 层有 fail-safe 会
        降级成"转人工"，看起来像是模型自己不想干活 —— 很容易被误判。
        这里把思维链暂存到实例属性上，由 agent 层在拼 assistant 消息时取用。
        返回值签名保持 (content, tool_call) 二元组不变，不破坏既有调用方与测试桩。
        """
        client = self._client()
        resp = client.chat.completions.create(
            messages=messages,
            model=self.model,
            temperature=self.temperature,
            tools=tools,
        )
        self._record_usage(resp)
        msg = resp.choices[0].message
        # openai SDK 对非标准字段走 model_extra；两种取法都试一遍，兼容不同 SDK 版本
        reasoning = getattr(msg, "reasoning_content", None)
        if reasoning is None:
            extra = getattr(msg, "model_extra", None) or {}
            reasoning = extra.get("reasoning_content")
        self.last_reasoning_content = reasoning or None
        tool_call = None
        if msg.tool_calls:
            tc = msg.tool_calls[0]
            tool_call = {"id": tc.id, "name": tc.function.name, "arguments": tc.function.arguments}
        return msg.content, tool_call
