from __future__ import annotations

import json
import time
from typing import Any

import requests

from services.ai_runtime import (
    DEFAULT_MAX_ATTEMPTS,
    DEFAULT_TIMEOUT_SECONDS,
    RetryableAIError,
    execute_with_retry,
)


class AIClientError(RuntimeError):
    pass


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



    def create_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: dict[str, Any],
    ) -> dict[str, Any]:


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
            }
        )



        start_time = time.time()

        def _request_once():
            try:
                response = requests.post(
                    "https://api.openai.com/v1/responses",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                    timeout=self.timeout,
                )
            except requests.exceptions.Timeout as exc:
                raise RetryableAIError(
                    f"OpenAI请求超时 ({self.timeout}s)"
                ) from exc
            except requests.exceptions.ConnectionError as exc:
                raise RetryableAIError(
                    f"OpenAI网络连接失败: {exc}"
                ) from exc
            except requests.exceptions.RequestException as exc:
                raise AIClientError(
                    f"OpenAI网络请求失败: {exc}"
                ) from exc

            if response.status_code >= 400:
                try:
                    detail = response.json().get("error", {}).get(
                        "message", response.text
                    )
                except Exception:
                    detail = response.text

                message = f"OpenAI 请求失败（{response.status_code}）：{detail}"
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

                f"OpenAI返回非JSON数据: {response.text[:200]}"

            ) from exc



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

                "OpenAI 未返回可解析的商品画像"

            )



        try:


            return json.loads(
                text
            )


        except json.JSONDecodeError as exc:


            raise AIClientError(

                f"OpenAI 返回的 JSON 无法解析：{exc}"

            ) from exc
