# -*- coding: utf-8 -*-
"""LLM / ASR 适配层：OpenAI 兼容协议。未配置时返回 None，由各模块走内置规则兜底。

接入自有技术只需在设置页填 base_url / api_key / model，或直接改这里的实现。
"""
import base64
import json
import os
import mimetypes

import requests

from . import config


class LLMUnavailable(Exception):
    pass


def _headers(key):
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def available():
    s = config.get_settings()["llm"]
    return bool(s.get("enabled") and s.get("api_key") and s.get("base_url"))


def chat(messages, temperature=0.7, max_tokens=4000, json_mode=False):
    """返回文本；未启用/失败抛 LLMUnavailable，调用方负责规则兜底。"""
    if not available():
        raise LLMUnavailable("未启用 LLM，使用内置规则引擎")
    s = config.get_settings()["llm"]
    url = s["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": s.get("model", "gpt-4o-mini"),
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        r = requests.post(url, headers=_headers(s["api_key"]), json=payload,
                          timeout=s.get("timeout", 90))
        r.raise_for_status()
        data = r.json()
        return data["choices"][0]["message"]["content"]
    except Exception as exc:
        raise LLMUnavailable(f"LLM 调用失败：{exc}")


def chat_json(messages, temperature=0.4, max_tokens=4000):
    """要求返回 JSON 对象，做容错解析。"""
    txt = chat(messages, temperature=temperature, json_mode=True, max_tokens=max_tokens)
    txt = txt.strip()
    if txt.startswith("```"):
        txt = txt.split("```")[1]
        if txt.lstrip().startswith("json"):
            txt = txt.lstrip()[4:]
        txt = txt.rsplit("```", 1)[0]
    start, end = txt.find("{"), txt.rfind("}")
    if start >= 0 and end > start:
        txt = txt[start:end + 1]
    return json.loads(txt)


def asr_transcribe(audio_path):
    """音频转文字。未启用时抛 LLMUnavailable。"""
    s = config.get_settings()["asr"]
    if not (s.get("enabled") and s.get("api_key") and s.get("base_url")):
        raise LLMUnavailable("未启用转写服务，请手动粘贴文案或上传 .srt/.txt")
    url = s["base_url"].rstrip("/") + "/audio/transcriptions"
    mime = mimetypes.guess_type(audio_path)[0] or "audio/wav"
    with open(audio_path, "rb") as fh:
        r = requests.post(
            url,
            headers={"Authorization": f"Bearer {s['api_key']}"},
            data={"model": s.get("model", "whisper-1"), "language": "zh"},
            files={"file": (os.path.basename(audio_path), fh, mime)},
            timeout=300,
        )
    r.raise_for_status()
    return r.json().get("text", "")


def vision_describe(image_paths, prompt):
    """可选：抽帧让视觉模型补充画面信息。未启用返回 None。"""
    if not available():
        return None
    s = config.get_settings()["llm"]
    url = s["base_url"].rstrip("/") + "/chat/completions"
    content = [{"type": "text", "text": prompt}]
    for p in image_paths[:4]:
        with open(p, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode()
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64:{b64}"}})
    try:
        r = requests.post(url, headers=_headers(s["api_key"]),
                          json={"model": s.get("model"), "messages": [{"role": "user", "content": content}],
                                "max_tokens": 600}, timeout=120)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception:
        return None
