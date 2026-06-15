"""Wrapper gọi OpenAI API để phân loại comment → sản phẩm.

API key lấy từ settings['ai_api_key'] (nhập trong dashboard) hoặc biến môi trường
OPENAI_API_KEY. Model mặc định gpt-4o-mini (đổi trong settings['ai_model']).

Dùng JSON mode (response_format=json_object) để model trả JSON parse được.
"""
import json
import os
import re

import live_database as db

DEFAULT_MODEL = "gpt-4o-mini"


def get_api_key():
    return db.get_setting("ai_api_key") or os.environ.get("OPENAI_API_KEY") or ""


def get_model():
    return db.get_setting("ai_model") or DEFAULT_MODEL


def is_configured():
    return bool(get_api_key())


def _client():
    from openai import OpenAI
    key = get_api_key()
    if not key:
        raise RuntimeError("Chưa có OPENAI_API_KEY (nhập trong dashboard hoặc đặt biến môi trường).")
    return OpenAI(api_key=key)


def call_ai_json(prompt, max_tokens=300):
    """Gọi OpenAI, trả dict {product_id, confidence, reason}. Ném lỗi nếu gọi/parse thất bại."""
    client = _client()
    model = get_model()
    kwargs = dict(
        model=model,
        max_tokens=max_tokens,
        messages=[
            {"role": "system", "content": "Bạn trả lời DUY NHẤT bằng một object JSON hợp lệ."},
            {"role": "user", "content": prompt},
        ],
    )
    try:
        resp = client.chat.completions.create(
            **kwargs, response_format={"type": "json_object"})
    except TypeError:
        resp = client.chat.completions.create(**kwargs)
    text = resp.choices[0].message.content
    return _parse_json(text)


def _parse_json(text):
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.S)   # vớt {...} nếu model kèm chữ thừa
        if m:
            return json.loads(m.group(0))
        raise ValueError(f"Không parse được JSON: {text[:200]}")
