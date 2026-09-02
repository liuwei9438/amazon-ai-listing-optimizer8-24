from __future__ import annotations

import json
import os
import time
from typing import Any

import requests
import streamlit as st

from services.ai_runtime import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    RetryableAIError,
    execute_with_retry,
)


class AIClientError(RuntimeError):
    pass


# 默认端点是 OpenAI 官方。配置 OPENAI_BASE_URL（Streamlit Secrets 或环境变量）
# 后切换为任意 OpenAI 协议兼容服务，例如 DeepSeek：
#     OPENAI_BASE_URL = "https://api.deepseek.com"
DEFAULT_OPENAI_BASE_URL = "https://api.openai.com/v1"


def get_openai_base_url() -> str:
    """读取自定义 API 端点；空字符串表示使用 OpenAI 官方。

    优先级：环境变量 > Streamlit Secrets。
    页面上的「AI 服务商」选择器通过环境变量注入，因此可以覆盖
    Secrets 里的静态配置。
    """
    url = str(os.getenv("OPENAI_BASE_URL", "") or "").strip()

    if not url:
        try:
            url = str(st.secrets.get("OPENAI_BASE_URL", "") or "").strip()
        except Exception:
            url = ""

    return url.rstrip("/")


class OpenAIResponsesClient:

    def __init__(
        self,
        api_key: str,
        model: str = "gpt-4.1-mini",
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        stage: str = "responses",
    ):

        if not api_key or not api_key.strip():

            raise AIClientError(
                "缺少 OpenAI API Key"
            )


        self.api_key = api_key.strip()

        self.model = (
            model.strip()
            or "gpt-4.1-mini"
        )

        self.timeout = timeout
        self.max_attempts = max(1, int(max_attempts))
        self.stage = stage
        self.base_url = (
            get_openai_base_url()
            or DEFAULT_OPENAI_BASE_URL
        ).rstrip("/")

        # OpenAI 官方端点继续使用 Responses API（严格 JSON Schema，行为与
        # 旧版完全一致）；自定义端点（DeepSeek 等）大多只支持
        # chat/completions + json_object，走兼容路径。
        self.use_chat_completions = (
            self.base_url != DEFAULT_OPENAI_BASE_URL
        )



    def create_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:


        if self.use_chat_completions:

            schema_note = (
                "\n\n输出要求：只输出一个 JSON 对象，"
                "不要输出任何解释文字或 Markdown 代码块标记。"
                "JSON 必须严格符合以下 JSON Schema：\n"
                + json.dumps(
                    schema,
                    ensure_ascii=False,
                )
            )

            payload = {

                "model":
                    self.model,

                "messages": [
                    {
                        "role": "system",
                        "content": system_prompt + schema_note,
                    },
                    {
                        "role": "user",
                        "content": user_prompt,
                    },
                ],

                "temperature": 0.1,

                "response_format": {
                    "type": "json_object",
                },

            }

            endpoint = (
                f"{self.base_url}/chat/completions"
            )

        else:

            payload = {

                "model":
                    self.model,

                "input": [

                    {
                        "role": "system",
                        "content": [
                            {
                                "type": "input_text",
                                "text": system_prompt,
                            }
                        ],
                    },

                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": user_prompt,
                            }
                        ],
                    },

                ],

                "temperature":
                    0.1,

                "text": {

                    "format": {

                        "type":
                            "json_schema",

                        "name":
                            "product_profile",

                        "strict":
                            True,

                        "schema":
                            schema,

                    }

                },

            }

            endpoint = "https://api.openai.com/v1/responses"



        # ==========================
        # Debug Request
        # ==========================


        prompt_length = (
            len(system_prompt)
            +
            len(user_prompt)
        )

        print(
            "OPENAI REQUEST START",
            {
                "model": self.model,
                "prompt_length": prompt_length,
                "timeout": self.timeout,
                "endpoint": endpoint,
            }
        )



        start_time = time.time()

        def _request_once():
            try:
                response = requests.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
            except requests.exceptions.Timeout as exc:
                raise RetryableAIError(
                    f"AI请求超时 ({self.timeout}s)"
                ) from exc
            except requests.exceptions.ConnectionError as exc:
                raise RetryableAIError(
                    f"AI网络连接失败: {exc}"
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise AIClientError(
                    f"AI网络请求失败: {exc}"
                ) from exc

            if response.status_code >= 400:
                try:
                    detail = response.json().get("error", {}).get(
                        "message", response.text
                    )
                except Exception:
                    detail = response.text

                message = f"AI 请求失败（{response.status_code}）：{detail}"
                if response.status_code in {408, 409, 429} or response.status_code >= 500:
                    raise RetryableAIError(message, response.status_code)
                raise AIClientError(message)

            return response

        response = execute_with_retry(
            _request_once,
            stage=self.stage,
            max_attempts=self.max_attempts,
        )

        elapsed = round(time.time() - start_time, 2)
        print(
            "OPENAI RESPONSE",
            {"status_code": response.status_code, "elapsed": elapsed},
        )

        try:

            data = response.json()


        except Exception as exc:


            raise AIClientError(
                f"AI返回非JSON数据: {response.text[:200]}"
            ) from exc


        if self.use_chat_completions:

            text = ""

            choices = data.get(
                "choices",
                [],
            )

            if choices:

                message = (
                    choices[0].get(
                        "message",
                        {},
                    )
                    or {}
                )

                text = (
                    message.get("content")
                    or ""
                )

        else:

            text = data.get(
                "output_text",
                "",
            )

            if not text:

                parts = []

                for item in data.get(
                    "output",
                    [],
                ):

                    for content in item.get(
                        "content",
                        [],
                    ):

                        if content.get(
                            "type"
                        ) in (
                            "output_text",
                            "text",
                        ):

                            if content.get(
                                "text"
                            ):

                                parts.append(
                                    content["text"]
                                )

                text = "".join(parts)




        if not text:



            raise AIClientError(
                "AI 未返回可解析的商品画像"
            )



        try:

            return json.loads(
                text
            )

        except json.JSONDecodeError as exc:

            # 兼容个别服务把 JSON 包在 ```json 代码块里的情况：
            # 截取第一个 "{" 到最后一个 "}" 之间再解析一次。
            cleaned = text.strip()

            brace_start = cleaned.find("{")
            brace_end = cleaned.rfind("}")

            if brace_start >= 0 and brace_end > brace_start:

                try:

                    return json.loads(
                        cleaned[
                            brace_start:brace_end + 1
                        ]
                    )

                except json.JSONDecodeError:
                    pass


            raise AIClientError(
                f"AI 返回的 JSON 无法解析：{exc}"
            ) from exc
