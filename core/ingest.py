# -*- coding: utf-8 -*-
"""素材获取与内容提取。

视频获取：抖音链接（yt-dlp）/ 本地上传。
内容提取：ASR 转写（可选）/ 手动粘贴 / 上传 .srt .txt / 内置规则从标题兜底。
"""
import os
import re
import subprocess
import unicodedata

from . import config, db, llm


def _run(cmd, timeout=300):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "命令失败")[-1200:])
    return p.stdout


def probe(path):
    try:
        out = _run([config.FFPROBE, "-v", "error", "-show_entries",
                    "format=duration:stream=codec_type,width,height",
                    "-of", "json", path], timeout=60)
        import json
        d = json.loads(out)
        dur = float(d.get("format", {}).get("duration", 0) or 0)
        w = h = 0
        for s in d.get("streams", []):
            if s.get("codec_type") == "video" and not w:
                w, h = s.get("width", 0), s.get("height", 0)
        return round(dur, 2), w, h
    except Exception:
        return 0, 0, 0


def safe_name(name):
    name = unicodedata.normalize("NFKC", str(name)).strip()
    name = re.sub(r"[^\w\u4e00-\u9fff.\-]", "_", name)
    return name[:80] or "file"


def save_upload(file_storage, prefix="up"):
    """保存 Flask 上传文件，返回绝对路径。"""
    from datetime import datetime
    fn = safe_name(file_storage.filename or "video.mp4")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(config.UPLOADS, f"{prefix}_{stamp}_{fn}")
    file_storage.save(path)
    return path


def extract_audio(video_path, audio_path):
    _run([config.FFMPEG, "-y", "-i", video_path, "-vn", "-ac", "1",
          "-ar", "16000", "-c:a", "pcm_s16le", audio_path], timeout=600)
    return audio_path


def extract_cover(video_path, cover_path):
    try:
        _run([config.FFMPEG, "-y", "-ss", "1", "-i", video_path, "-vframes", "1",
              "-q:v", "3", cover_path], timeout=90)
        return cover_path
    except Exception:
        return ""


def fetch_douyin(url, cookie=""):
    """用 yt-dlp 下载抖音视频。返回 (file_path, meta)。

    抖音反爬较强，失败时给出明确原因；此时可改用『上传本地视频』，流程不受影响。
    """
    try:
        import yt_dlp
    except ImportError:
        raise RuntimeError("未安装 yt-dlp；可执行 pip install yt-dlp，或改用上传本地视频")

    from datetime import datetime
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    outtmpl = os.path.join(config.UPLOADS, f"dy_{stamp}_%(id)s.%(ext)s")
    opts = {
        "outtmpl": outtmpl,
        "format": "best[height<=?]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "retries": 2,
    }
    fh = config.SETTINGS_PATH.replace("settings.json", "cookies.txt")
    if cookie:
        with open(fh, "w", encoding="utf-8") as f:
            f.write(_to_netscape_cookie(cookie))
        opts["cookiefile"] = fh
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            path = ydl.prepare_filename(info)
            if not os.path.exists(path):
                cand = [p for p in _glob_stamp(stamp) if p.endswith(".mp4")]
                if not cand:
                    raise RuntimeError("下载完成但未找到视频文件")
                path = cand[0]
            meta = {
                "title": info.get("title") or info.get("description") or "抖音视频",
                "author": info.get("uploader") or info.get("channel") or "",
                "likes": info.get("like_count") or 0,
                "duration": info.get("duration") or 0,
                "source_url": url,
            }
            return path, meta
    except Exception as exc:
        raise RuntimeError(f"抖音采集失败：{str(exc)[:400]}。可改用『上传本地视频』继续流程。")


def _glob_stamp(stamp):
    import glob
    return glob.glob(os.path.join(config.UPLOADS, f"dy_{stamp}_*"))


def _to_netscape_cookie(raw):
    """把浏览器复制的 cookie 字符串粗略转成 Netscape 格式（域名 douyin.com）。"""
    raw = raw.strip()
    if raw.startswith("# Netscape") or "\t" in raw:
        return raw + "\n"
    lines = ["# Netscape HTTP Cookie File"]
    for kv in raw.split(";"):
        kv = kv.strip()
        if not kv or "=" not in kv:
            continue
        k, v = kv.split("=", 1)
        lines.append(f".douyin.com\tTRUE\t/\tFALSE\t0\t{k.strip()}\t{v.strip()}")
    return "\n".join(lines) + "\n"


def download_play_url(url, dest, timeout=60, max_bytes=120 * 1024 * 1024):
    """下载 TikHub 搜索结果里的 play_url（视频播放地址）到 dest。

    这是普通 HTTP 下载，不消耗 TikHub API 调用次数；但地址可能防盗链或过期，
    所以带 Referer / UA、限制体积、检查 Content-Type，失败交给调用方回落轻量档。
    """
    import requests
    headers = {
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/124.0 Safari/537.36"),
        "Referer": "https://www.douyin.com/",
    }
    total = 0
    with requests.get(url, stream=True, headers=headers, timeout=timeout) as resp:
        resp.raise_for_status()
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "text/html" in ctype:
            raise RuntimeError("play_url 返回网页而非视频，疑似防盗链或地址过期")
        with open(dest, "wb") as fh:
            for chunk in resp.iter_content(65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise RuntimeError("视频超过 %d MB 上限" % (max_bytes // 1048576))
                fh.write(chunk)
    if total < 10000:
        raise RuntimeError("下载内容过小（%d 字节），疑似被拦截" % total)
    return dest


def parse_srt(text):
    """从 srt/vtt 文本里抽出纯文本行。"""
    lines, out = text.replace("\r", "").split("\n"), []
    for ln in lines:
        s = ln.strip()
        if not s or s.isdigit() or "-->" in s or s.upper() in ("WEBVTT", "NOTE"):
            continue
        s = re.sub(r"<[^>]+>", "", s)
        s = re.sub(r"\{[^}]+\}", "", s)
        if s and (not out or out[-1] != s):
            out.append(s)
    return "\n".join(out)


def extract_transcript(video_id, mode="auto", manual_text="", srt_path=""):
    """返回 (transcript, source)。永不抛错，始终给可用的文字。"""
    v = db.get_video(video_id)
    if not v:
        raise RuntimeError("素材不存在")

    if srt_path and os.path.exists(srt_path):
        with open(srt_path, "r", encoding="utf-8", errors="ignore") as fh:
            txt = parse_srt(fh.read())
        if txt.strip():
            return txt.strip(), "字幕文件"

    if manual_text and manual_text.strip():
        return manual_text.strip(), "手动粘贴"

    if v.get("transcript"):
        return v["transcript"], v.get("transcript_source") or "已存文案"

    if mode in ("auto", "asr"):
        path = v.get("file_path")
        if path and os.path.exists(path):
            audio = os.path.splitext(path)[0] + "_audio.wav"
            try:
                extract_audio(path, audio)
                txt = llm.asr_transcribe(audio)
                if txt and txt.strip():
                    try:
                        os.remove(audio)
                    except OSError:
                        pass
                    return txt.strip(), "ASR 转写"
            except Exception:
                pass
            finally:
                if os.path.exists(audio):
                    try:
                        os.remove(audio)
                    except OSError:
                        pass

    # 兜底：用标题生成一份可分析的占位文案，保证后续链路不中断
    title = v.get("title") or "同行视频"
    fallback = (f"【素材标题】{title}\n"
                f"【说明】未取得口播文字。请点击『补文案』粘贴原视频口播内容或上传字幕文件，"
                f"分析结果会更准确。当前报告基于标题与元数据做结构推断。")
    return fallback, "标题兜底（待补文案）"
