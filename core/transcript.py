# -*- coding: utf-8 -*-
"""逐字稿：带时间戳 SRT 的本地生成与规则分析。

做法移植自用户 dy- 仓库 analyze_douyin.py 的
    transcribe_with_whisper() → load_transcript() → analyze_transcript()
用本地 faster-whisper（int8）转写，不依赖云端 ASR、不花 API 费用。
DeepSeek 只有 /chat/completions，没有 /audio/transcriptions，
所以这条链路不能复用 llm.asr_transcribe()。

关键：逐字稿必须是「带时间戳的 SRT」，不是纯文本 —— 前 3 秒原话、
语速、信息密度曲线全部依赖 start/end，纯文本算不出来。
"""
import os
import re
from pathlib import Path

from . import config

SRT_TIME = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")
CJK = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
WORD = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?|\d+(?:\.\d+)?")

HOOK_TYPES = {
    "提问/悬念": ["为什么", "怎么", "如何", "你知道", "有没有想过", "到底", "？", "?"],
    "反常识": ["其实", "并不是", "恰恰相反", "没想到", "竟然", "居然", "错了"],
    "痛点/共鸣": ["困扰", "很多人", "你是不是", "总是", "一直"],
    "利益承诺": ["教你", "学会", "看完", "只需", "方法", "技巧"],
    "权威/身份": ["我是", "教授", "得主", "研究", "科学家"],
    "数字/具体": [str(i) for i in range(10)],
}

# faster-whisper 模型按规格缓存，批量采集 5 条只加载一次
_MODEL_CACHE = {}


def srt_ts(t):
    ms = int(round(float(t) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def parse_srt_timed(text):
    """把 SRT/VTT 文本解析成带时间戳的段；纯文本则 timed=False。"""
    text = text or ""
    segs = []
    if SRT_TIME.search(text):
        for block in re.split(r"\n\s*\n", text.strip()):
            m = SRT_TIME.search(block)
            if not m:
                continue
            g = [int(x) for x in m.groups()]
            start = g[0] * 3600 + g[1] * 60 + g[2] + g[3] / 1000.0
            end = g[4] * 3600 + g[5] * 60 + g[6] + g[7] / 1000.0
            lines = [ln for ln in block.splitlines()
                     if ln.strip() and not SRT_TIME.search(ln) and not ln.strip().isdigit()]
            body = "".join(ln.strip() for ln in lines).strip()
            if body:
                segs.append({"start": start, "end": end, "text": body})
        if segs:
            return {"timed": True, "segments": segs,
                    "full": "".join(s["text"] for s in segs)}
    plain = re.sub(r"<[^>]+>", "", text)
    plain = re.sub(r"\s+", "", plain)
    return {"timed": False, "segments": [], "full": plain.strip()}


def load_transcript(path):
    if not path or not Path(path).exists():
        return None
    raw = Path(path).read_text(encoding="utf-8", errors="ignore")
    tr = parse_srt_timed(raw)
    return tr if tr["full"] else None


def text_units(s):
    """说了多少「字」：中文按字，英文按词，数字按串（与 dy- 口径一致）。"""
    return len(CJK.findall(s or "")) + len(WORD.findall(s or ""))


def classify_hook(text):
    return [k for k, ws in HOOK_TYPES.items() if any(w in (text or "") for w in ws)]


def analyze_transcript(tr, duration):
    """从带时间戳的逐字稿算第四章的全部量化指标。无稿返回 None。"""
    if not tr or not tr.get("full"):
        return None
    full = tr["full"]
    units = text_units(full)
    r = {"chars": units, "timed": bool(tr.get("timed")),
         "segments_count": len(tr.get("segments") or []),
         "hook_3s": "", "hook_15s": "", "speech_rate_cpm": None,
         "density_curve": [], "long_gaps": 0, "hook_types": []}

    def join(parts):
        out = ""
        for p in parts:
            p = (p or "").strip()
            if out and re.search(r"[A-Za-z0-9.,!?'\"]$", out) and re.match(r"[A-Za-z0-9]", p):
                out += " "
            out += p
        return out.strip()

    segs = tr.get("segments") or []
    if r["timed"] and segs:
        r["hook_3s"] = join(s["text"] for s in segs if s["start"] < 3) or segs[0]["text"]
        r["hook_15s"] = join(s["text"] for s in segs if s["start"] < 15)
        speak_time = sum(s["end"] - s["start"] for s in segs)
        if speak_time > 0:
            r["speech_rate_cpm"] = round(units / (speak_time / 60), 1)
        total = duration or segs[-1]["end"] or 1
        buckets = [0] * 10
        for s in segs:
            idx = min(int(s["start"] / total * 10), 9)
            buckets[idx] += text_units(s["text"])
        r["density_curve"] = buckets
        r["long_gaps"] = sum(
            1 for i in range(len(segs) - 1)
            if segs[i + 1]["start"] - segs[i]["end"] > 3)
        r["duration_used"] = round(total, 1)
    else:
        r["hook_3s"] = full[:15]
        r["hook_15s"] = full[:70]
        if duration:
            r["speech_rate_cpm"] = round(units / (duration / 60), 1)
    r["hook_types"] = classify_hook(r["hook_15s"])
    return r


# ASR 高频误识别的财税/工商术语对照，作为纠错提示词的一部分
_DOMAIN_GLOSSARY = (
    "营业执照、营业值要、个体工商户、个体户、代理记账、报税、纳税申报、汇算清缴、"
    "增值税、企业所得税、个人所得税、附加税、税务筹划、税收洼地、核定征收、"
    "工商注册、工商变更、注销、吊销、注册资本、实缴、认缴、验资、年报、"
    "对公账户、银行开户、基本户、开户许可证、公章、财务章、法人、股东、监事、"
    "股权、代持、经营范围、地址挂靠、异常名录、失信、限高、社保、公积金、"
    "发票、专票、普票、开票、作废、红冲、稽查、滞纳金、罚款"
)

_CORRECT_SYSTEM = (
    "你是中文语音识别（ASR）校对员，专注财税、工商、企业服务领域。"
    "输入是 Whisper 转写的口播稿，存在同音字/谐音字错误"
    "（例如「营业值要」应为「营业执照」）。"
    "请结合上下文和以下领域术语表纠正错别字：" + _DOMAIN_GLOSSARY + "。"
    "硬性规则："
    "1) 只改错别字和明显转写错误，不改动语序、不增删内容、不改口语风格；"
    "2) 术语表的词优先于普通猜测；拿不准的原样保留，不要编造；"
    "3) 数组必须逐条对应，条数、顺序都不能变。"
)


def correct_transcript(tr):
    """用 DeepSeek 纠正逐字稿的 ASR 错别字，返回 (tr, 修正段数)。

    按「段」纠正而不是纠正拼接全文：第四章的前 3 秒 / 前 15 秒原话、
    信息密度曲线都来自 segments 的逐段文本，整段替换才能让全文和
    时间戳指标保持一致。模型不可用或返回条数不符时原样返回，不阻断采集。
    """
    if not tr or not tr.get("full") or not (tr.get("segments") or []):
        return tr, 0
    from . import llm  # 局部导入，避免顶层循环
    if not llm.available():
        return tr, 0
    segs = tr["segments"]
    texts = [str(s.get("text") or "") for s in segs[:80]]
    try:
        data = llm.chat_json([
            {"role": "system", "content": _CORRECT_SYSTEM},
            {"role": "user", "content": "待校对的逐字稿数组：\n" +
             "\n".join("%d|%s" % (i + 1, t) for i, t in enumerate(texts)) +
             "\n\n输出 JSON：{\"segments\": [\"纠正后的第1条\", ...]}，"
             "条数必须与输入一致。"},
        ], temperature=0.1, max_tokens=4000)
        fixed = data.get("segments") if isinstance(data, dict) else None
        if not isinstance(fixed, list) or len(fixed) != len(texts):
            return tr, 0
        changed = 0
        for s, new in zip(segs[:80], fixed):
            new = str(new or "").strip()
            old = str(s.get("text") or "").strip()
            if new and new != old:
                s["text"] = new
                changed += 1
        if changed:
            tr["full"] = "".join(str(s.get("text") or "") for s in segs)
        return tr, changed
    except Exception:
        return tr, 0


def _wav_to_float32(path):
    """把 ffmpeg 抽出的 wav 读成 float32 mono 数组，直接喂给 model.transcribe()。

    不这么做的话 faster-whisper 会走 PyAV 的 decode_audio() 解码文件路径，
    而 faster-whisper 1.2.x 调用的 av.open(..., metadata_errors=...) 参数在
    PyAV 19 里已被移除，一进 transcribe() 就报
    open() got an unexpected keyword argument 'metadata_errors'。
    传 ndarray 完全绕开 PyAV，与 av 版本解耦，模型加载和下载不受影响。
    """
    import wave
    import numpy as np
    with wave.open(path, "rb") as w:
        rate, ch, sw = w.getframerate(), w.getnchannels(), w.getsampwidth()
        raw = w.readframes(w.getnframes())
    if rate != 16000:
        raise RuntimeError("wav 采样率 %dHz ≠ 16000，ndarray 直喂会错位时间戳" % rate)
    if sw == 2:
        a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 4:
        a = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise RuntimeError("不支持的 WAV 位深 %d bit（期望 ffmpeg 产出的 16/32bit PCM）" % (sw * 8))
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if a.size == 0:
        raise RuntimeError("音频为空：视频可能没有音轨，或 ffmpeg 抽取失败")
    return a


def transcribe_video(video_path, out_dir, model_size="small"):
    """本地 faster-whisper 转写，生成带时间戳 transcript.srt，返回路径。

    国内服务器常拉不到 HuggingFace，默认走 hf-mirror；模型缓存放在
    data/hf_cache（已 gitignore），避免 www-data 没有 home 写权限。
    两者都允许用外部环境变量覆盖。
    音频用 ffmpeg 预先抽成 16kHz 单声道 wav 后读成数组直喂模型，
    不把文件路径交给 transcribe()（规避 PyAV 19 的 metadata_errors 崩溃）。
    """
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HOME", os.path.join(config.DATA, "hf_cache"))
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "未安装 faster-whisper：在服务器执行 "
            "sudo /opt/douyin-studio/.venv/bin/pip install faster-whisper") from exc

    from . import ingest  # 局部导入，避免与 ingest 形成顶层循环
    os.makedirs(out_dir, exist_ok=True)
    wav = os.path.join(out_dir, "asr_audio.wav")
    srt = os.path.join(out_dir, "transcript.srt")
    ingest.extract_audio(video_path, wav)
    try:
        audio = _wav_to_float32(wav)
        if model_size not in _MODEL_CACHE:
            _MODEL_CACHE[model_size] = WhisperModel(
                model_size, device="auto", compute_type="int8")
        model = _MODEL_CACHE[model_size]
        segments, _info = model.transcribe(
            audio, language="zh", vad_filter=True,
            initial_prompt="以下是普通话的句子，使用简体中文。")
        with open(srt, "w", encoding="utf-8") as fh:
            for i, seg in enumerate(segments, 1):
                fh.write(f"{i}\n{srt_ts(seg.start)} --> {srt_ts(seg.end)}\n"
                         f"{seg.text.strip()}\n\n")
    finally:
        try:
            os.remove(wav)
        except OSError:
            pass
    return srt
