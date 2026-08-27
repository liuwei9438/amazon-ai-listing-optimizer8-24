from __future__ import annotations

import os
import streamlit as st


def get_openai_api_key() -> str:
    """统一读取 OpenAI API Key。"""
    key = ""
    try:
        key = str(st.secrets.get("OPENAI_API_KEY", "") or "").strip()
    except Exception:
        key = ""

    if not key:
        key = str(os.getenv("OPENAI_API_KEY", "") or "").strip()

    return key
