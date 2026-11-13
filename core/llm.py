# -*- coding: utf-8 -*-
"""LLM / ASR 适配层：OpenAI 兼容协议。未配置时返回 None，由各模块走内置规则兜底。

接入自有技术只需在设置页填 base_url / api_key / model，或直接改这里的实现。
"""
import base64
import json
import os
import re
import mimetypes
import time

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
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        content = msg.get("content") or ""
        if not content.strip():
            # 空回复：多半是内容安全过滤拦截（finish_reason=length/content_filter
            # 或 provider 直接给空串）。带上 finish_reason 方便定位。
            finish = (data.get("choices") or [{}])[0].get("finish_reason")
            raise LLMUnavailable("模型返回空内容（finish_reason=%s）" % finish)
        return content
    except LLMUnavailable:
        raise
    except Exception as exc:
        raise LLMUnavailable(f"LLM 调用失败：{exc}")


def _strip_json(txt):
    """去掉 markdown 包裹、截取首尾大括号、清理尾随逗号。"""
    txt = (txt or "").strip()
    if txt.startswith("```"):
        txt = txt.split("```")[1]
        if txt.lstrip().startswith("json"):
            txt = txt.lstrip()[4:]
        txt = txt.rsplit("```", 1)[0]
    start, end = txt.find("{"), txt.rfind("}")
    if start >= 0 and end > start:
        txt = txt[start:end + 1]
    # 模型偶尔在最后一个数组元素后留逗号：, } / , ]
    return re.sub(r",\s*([}\]])", r"\1", txt).strip()


def _close_json(txt):
    """输出被 max_tokens 截断时，按括号栈补齐闭合符。

    十维 JSON 很长，模型常写到第 7、8 个维度就被切断；此时 rfind('}')
    会把最后一个完整对象的 '}' 当成结尾，缺的只是未闭合的 ] 和 }。
    补齐后 dimensions 可能不足 10 个，交给 deep_analyze() 的补齐逻辑处理，
    总比整条报告回落规则引擎强。
    """
    stack, in_str, esc = [], False, False
    for ch in txt:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]":
            if stack and stack[-1] == ch:
                stack.pop()
    out = txt + ('"' if in_str else "")
    return out + "".join(reversed(stack))


def _dump_llm_error(raw, exc, txt):
    """把解析失败的原始输出落盘，便于事后定位（data/ 已被 gitignore）。"""
    try:
        path = os.path.join(config.DATA, "llm_json_error.txt")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("error: %s\nlength: %d\n\n=== 截取后 ===\n%s\n\n=== 原始输出 ===\n%s"
                     % (exc, len(txt), txt, raw))
    except Exception:
        pass


def chat_json(messages, temperature=0.4, max_tokens=4000):
    """要求返回 JSON 对象；对模型常见的几类破损做容错解析。

    1) markdown 代码块 / 前后解释文字；2) 尾随逗号、字符串内裸换行；
    3) 被 max_tokens 截断导致闭合符缺失（十维长 JSON 最常见）；
    4) 偶发空回复（内容过滤或瞬时故障）—— json_mode 与非 json_mode
       交替最多试 3 次，间隔 2 秒。
    全部失败抛 LLMUnavailable，最后一次原文落盘 data/llm_json_error.txt。
    """
    last_exc = None
    for attempt, json_mode in enumerate((True, False, True)):
        try:
            raw = chat(messages, temperature=temperature, json_mode=json_mode,
                       max_tokens=max_tokens)
        except LLMUnavailable as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(2)
            continue
        txt = _strip_json(raw)
        if not txt:
            last_exc = LLMUnavailable("模型返回空内容（json_mode=%s）" % json_mode)
            if attempt < 2:
                time.sleep(2)
            continue
        try:
            # strict=False 允许字符串里出现裸换行/控制字符
            return json.loads(txt, strict=False)
        except (ValueError, TypeError) as exc:
            last_exc = exc
        fixed = _close_json(txt)
        if fixed != txt:
            try:
                return json.loads(fixed, strict=False)
            except (ValueError, TypeError) as exc:
                last_exc = exc
        _dump_llm_error(raw, last_exc, txt)
    raise LLMUnavailable("模型返回的 JSON 无法解析：%s" % last_exc)


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
