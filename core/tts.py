# -*- coding: utf-8 -*-
"""edge-tts 中文配音封装。

零成本、无需密钥的微软在线 TTS。用于两类场景：
  1) 无原片出片（TTS 卡片模式）：把文案整段念出来当配音轨。
  2) MotionGit 模板：把生成的旁白绑定为 voice: {mode:'file'} 喂给模板。

edge-tts 需要联网；离线或失败时本模块会抛错，由调用方决定兜底（静音轨 / 提示）。
"""
import asyncio
import os
import re
import subprocess

import edge_tts

# 中文常用音色（微软在线，零成本）。Xiaoxiao 自然、Yunxi 偏男声解说。
VOICES = {
    "xiaoxiao": "zh-CN-XiaoxiaoNeural",   # 女声，自然流畅（默认）
    "yunxi": "zh-CN-YunxiNeural",         # 男声，清爽
    "xiaoyi": "zh-CN-XiaoyiNeural",       # 女声，温柔
    "yunyang": "zh-CN-YunyangNeural",     # 男声，新闻播报感
    "xiaobei": "zh-CN-XiaobeiNeural",     # 女声，活泼
}

DEFAULT_VOICE = "xiaoxiao"


def voice_id(name):
    return VOICES.get(name, VOICES.get(DEFAULT_VOICE))


def _split_sentences(text, max_chars=120):
    """按标点断句，避免一次性合成过长导致超时或截断。"""
    text = re.sub(r"[ \t　]+", " ", (text or "").strip())
    if not text:
        return []
    raw = re.split(r"(?<=[。！？!?；;])", text)
    out = []
    for p in raw:
        p = p.strip()
        if not p:
            continue
        while len(p) > max_chars:
            out.append(p[:max_chars])
            p = p[max_chars:]
        out.append(p)
    return out


async def _run(text, voice, out_path, rate):
    communicate = edge_tts.Communicate(text, voice, rate=rate)
    await communicate.save(out_path)


def synthesize(text, out_path, voice=DEFAULT_VOICE, rate="+0%", retries=3):
    """把 text 合成为 mp3，写到 out_path。返回时长（秒）或 0。

    rate 形如 '+0%' / '-10%' / '+15%'，微软 TTS 调速语法。
    """
    vid = voice_id(voice)
    sents = _split_sentences(text)
    if not sents:
        raise ValueError("空文本，无可合成内容")
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    # 分句合成再拼接：长文本更稳，且分句可并行。
    chunk_paths = []
    tmpdir = os.path.dirname(os.path.abspath(out_path))
    for i, s in enumerate(sents):
        cp = os.path.join(tmpdir, f".tts_chunk_{os.getpid()}_{i}.mp3")
        last = None
        for attempt in range(retries):
            try:
                asyncio.run(_run(s, vid, cp, rate))
                last = None
                break
            except Exception as exc:  # 网络抖动，重试
                last = exc
                continue
        if last:
            raise RuntimeError("edge-tts 合成失败：%s" % last)
        chunk_paths.append(cp)

    if len(chunk_paths) == 1:
        os.replace(chunk_paths[0], out_path)
    else:
        # 用 ffmpeg 拼接（文件级 concat，避免重编码劣化）
        listf = os.path.join(tmpdir, f".tts_list_{os.getpid()}.txt")
        with open(listf, "w", encoding="utf-8") as fh:
            for cp in chunk_paths:
                fh.write("file '%s'\n" % os.path.abspath(cp).replace("'", "'\\''"))
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
             "-f", "concat", "-safe", "0", "-i", listf, "-c", "copy", out_path],
            check=True, timeout=300)
        for cp in chunk_paths:
            try:
                os.remove(cp)
            except OSError:
                pass
        try:
            os.remove(listf)
        except OSError:
            pass
    return probe_duration(out_path)


def probe_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=60).stdout.strip()
        return float(out) if out else 0.0
    except Exception:
        return 0.0
