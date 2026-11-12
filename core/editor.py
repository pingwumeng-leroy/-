# -*- coding: utf-8 -*-
"""模板化剪辑引擎：口播视频 + 文案 → 竖屏成片。

真实能力（全部本地，无需任何外部服务）：
  统一 9:16 画幅 / 片头标题卡 / 片尾引导卡 / 按文案自动分句烧录中文字幕 /
  水印叠加 / 裁头尾 / 调速 / 可选 BGM / 输出 H.264+AAC mp4
"""
import os
import re
import subprocess
import threading
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from . import config, db, ingest


# ---------------- 素材图生成 ----------------
def _font(size, bold=True):
    path = config.FONT_BOLD if bold else config.FONT_REG
    try:
        return ImageFont.truetype(path, size, index=2)  # SC 字形
    except Exception:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            return ImageFont.load_default()


def _wrap(draw, text, font, max_width):
    lines, cur = [], ""
    for ch in text:
        if ch == "\n":
            lines.append(cur)
            cur = ""
            continue
        t = cur + ch
        if draw.textlength(t, font=font) > max_width and cur:
            lines.append(cur)
            cur = ch
        else:
            cur = t
    if cur:
        lines.append(cur)
    return lines


def _hex(rgb):
    rgb = (rgb or "#12161d").lstrip("#")
    return tuple(int(rgb[i:i + 2], 16) for i in (0, 2, 4))


def make_card(path, tpl, kind, title=""):
    """生成片头/片尾整屏卡片。"""
    W, H = int(tpl.get("width", 1080)), int(tpl.get("height", 1920))
    bg, accent = _hex(tpl.get("bg_color")), _hex(tpl.get("accent_color"))
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, 14], fill=accent)
    d.rectangle([0, H - 14, W, H], fill=accent)

    if kind == "intro":
        f_title = _font(int(tpl.get("title_size", 76)))
        f_small = _font(40, bold=False)
        lines = _wrap(d, title or "企业服务那些事", f_title, W - 160)
        y = H // 2 - (len(lines) * int(f_title.size * 1.35)) // 2 - 60
        for ln in lines[:4]:
            tw = d.textlength(ln, font=f_title)
            d.text(((W - tw) / 2, y), ln, font=f_title, fill=(255, 255, 255))
            y += int(f_title.size * 1.35)
        d.rounded_rectangle([W // 2 - 90, y + 36, W // 2 + 90, y + 46], radius=5, fill=accent)
        company = config.get_settings()["company"].get("short", "")
        tw = d.textlength(company, font=f_small)
        d.text(((W - tw) / 2, H - 220), company, font=f_small, fill=(190, 198, 210))
        city = config.get_settings()["company"].get("city", "")
        tw2 = d.textlength(f"{city} · 企业服务", font=f_small)
        d.text(((W - tw2) / 2, H - 160), f"{city} · 企业服务", font=f_small, fill=(140, 150, 165))
    else:
        f = _font(int(tpl.get("title_size", 76)) - 8)
        f_small = _font(38, bold=False)
        txt = tpl.get("outro_text") or "关注我，少踩坑"
        lines = _wrap(d, txt, f, W - 160)
        y = H // 2 - (len(lines) * int(f.size * 1.4)) // 2
        for ln in lines[:3]:
            tw = d.textlength(ln, font=f)
            d.text(((W - tw) / 2, y), ln, font=f, fill=(255, 255, 255))
            y += int(f.size * 1.4)
        company = config.get_settings()["company"].get("short", "")
        tw = d.textlength(company, font=f_small)
        d.text(((W - tw) / 2, H - 240), company, font=f_small, fill=(190, 198, 210))
    img.save(path)
    return path


# ---------------- 字幕 ----------------
def _ass_time(sec):
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h:d}:{m:02d}:{s:05.2f}"


def split_caption(body, max_chars=22):
    body = re.sub(r"[ \t]+", "", body or "")
    raw = re.split(r"[\n。！？!?；;]+", body)
    segs = []
    for p in raw:
        p = p.strip("，,、 ")
        if not p:
            continue
        while len(p) > max_chars:
            segs.append(p[:max_chars])
            p = p[max_chars:]
        if p:
            segs.append(p)
    return segs


def make_ass(path, body, duration, tpl):
    segs = split_caption(body)
    if not segs or duration <= 0:
        segs = ["（无字幕文案）"]
    weights = [max(4, len(s)) for s in segs]
    total = sum(weights)
    size = int(tpl.get("subtitle_size", 58))
    color = (tpl.get("subtitle_color") or "#ffffff").lstrip("#")
    bgr = f"&H00{color[4:6]}{color[2:4]}{color[0:2]}".upper()
    margin_v = int(int(tpl.get("height", 1920)) * 0.16)

    head = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {int(tpl.get('width',1080))}
PlayResY: {int(tpl.get('height',1920))}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Noto Sans CJK SC,{size},{bgr},&H000000FF,&H00000000,&H90000000,-1,0,0,0,100,100,0,0,1,3,1,2,60,60,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines, t = [], 0.0
    for s, w in zip(segs, weights):
        dur = max(1.2, duration * w / total)
        end = min(duration, t + dur)
        lines.append(f"Dialogue: 0,{_ass_time(t)},{_ass_time(end)},Default,,0,0,0,,{s}")
        t = end
        if t >= duration:
            break
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(head + "\n".join(lines) + "\n")
    return path, len(lines)


# ---------------- 剪辑主流程 ----------------
def _has_audio(path):
    try:
        out = subprocess.run([config.FFPROBE, "-v", "error", "-select_streams", "a",
                              "-show_entries", "stream=index", "-of", "csv=p=0", path],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        return bool(out)
    except Exception:
        return False


def _log(jid, msg):
    try:
        db.update_job(jid, log=msg[-4000:])
    except Exception:
        pass
    print("[EDIT]", msg, flush=True)


def _esc(text):
    """drawtext 文本转义：用 textfile 规避，这里只做基础清理。"""
    return text.replace("\n", " ").strip()


def render(job_id):
    """后台线程执行。成功写 output_path + status=done。"""
    job = db.get_job(job_id)
    if not job:
        return
    tmpdir = os.path.join(config.ASSETS, f"job_{job_id}")
    os.makedirs(tmpdir, exist_ok=True)
    try:
        db.update_job(job_id, status="running", progress=5)
        tpl = config.get_template(job["template_id"])
        src = job["video_path"]
        if not os.path.exists(src):
            raise RuntimeError("口播视频文件不存在，请重新上传")
        script = db.get_script(job["script_id"]) or {}
        body = script.get("body") or ""
        title = script.get("title") or "企业服务那些事"

        dur, w, h = ingest.probe(src)
        if dur <= 0:
            raise RuntimeError("无法读取视频时长，文件可能损坏")
        trim_h = float(tpl.get("trim_head") or 0)
        trim_t = float(tpl.get("trim_tail") or 0)
        src_dur = max(0.6, dur - trim_h - trim_t)
        speed = min(2.0, max(0.5, float(tpl.get("speed") or 1.0)))
        core_dur = src_dur / speed
        intro = float(tpl.get("intro_seconds") or 0)
        outro = float(tpl.get("outro_seconds") or 0)
        W, H = int(tpl.get("width", 1080)), int(tpl.get("height", 1920))
        fps = int(tpl.get("fps", 30))
        bghex = "0x" + (tpl.get("bg_color") or "#12161d").lstrip("#")

        _log(job_id, f"源视频 {dur}s / {w}x{h}；主体 {core_dur:.1f}s；片头 {intro}s；片尾 {outro}s")
        db.update_job(job_id, progress=15)

        # 素材文件
        intro_png = make_card(os.path.join(tmpdir, "intro.png"), tpl, "intro", title)
        outro_png = make_card(os.path.join(tmpdir, "outro.png"), tpl, "outro")
        ass_path, n_sub = make_ass(os.path.join(tmpdir, "sub.ass"), body, core_dur, tpl)
        wm_text_path = os.path.join(tmpdir, "wm.txt")
        with open(wm_text_path, "w", encoding="utf-8") as fh:
            fh.write(_esc(tpl.get("watermark") or ""))
        _log(job_id, f"字幕 {n_sub} 句已生成")
        db.update_job(job_id, progress=30)

        # ---- 固定输入顺序，逐一记录索引 ----
        cmd = [config.FFMPEG, "-y", "-hide_banner", "-loglevel", "error"]
        idx = 0
        i_intro = i_video = i_outro = i_aintro = i_aoutro = i_bgm = None

        if intro > 0:
            cmd += ["-loop", "1", "-t", f"{intro:.2f}", "-i", intro_png]
            i_intro = idx; idx += 1
        cmd += ["-ss", f"{trim_h:.2f}", "-t", f"{src_dur:.2f}", "-i", src]
        i_video = idx; idx += 1
        if outro > 0:
            cmd += ["-loop", "1", "-t", f"{outro:.2f}", "-i", outro_png]
            i_outro = idx; idx += 1
        if intro > 0:
            cmd += ["-f", "lavfi", "-t", f"{intro:.2f}", "-i", "anullsrc=r=44100:cl=stereo"]
            i_aintro = idx; idx += 1
        if outro > 0:
            cmd += ["-f", "lavfi", "-t", f"{outro:.2f}", "-i", "anullsrc=r=44100:cl=stereo"]
            i_aoutro = idx; idx += 1

        bgm = (tpl.get("bgm") or "").strip()
        bgm_ok = bool(bgm and os.path.exists(bgm))
        if bgm_ok:
            cmd += ["-stream_loop", "-1", "-i", bgm]
            i_bgm = idx; idx += 1

        # ---- 滤镜链 ----
        parts = []
        common = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color={bghex},setsar=1,fps={fps},format=yuv420p"

        if intro > 0:
            parts.append(f"[{i_intro}:v]{common},trim=duration={intro:.2f},setpts=PTS-STARTPTS[vintro]")

        core = (f"[{i_video}:v]setpts=(PTS-STARTPTS)/{speed},{common}")
        if tpl.get("burn_subtitle", True):
            core += ",subtitles=sub.ass"
        wm = (tpl.get("watermark") or "").strip()
        if wm:
            core += (f",drawtext=fontfile={config.FONT_REG}:textfile=wm.txt:fontsize=34:"
                     f"fontcolor=white@0.85:x=w-tw-56:y=84:box=1:boxcolor=black@0.35:boxborderw=14")
        core += "[vcore]"
        parts.append(core)

        if outro > 0:
            parts.append(f"[{i_outro}:v]{common},trim=duration={outro:.2f},setpts=PTS-STARTPTS[voutro]")

        if _has_audio(src):
            parts.append(f"[{i_video}:a]asetpts=PTS-STARTPTS,atempo={speed},"
                         f"aresample=44100,aformat=channel_layouts=stereo[acore]")
        else:
            parts.append(f"anullsrc=r=44100:cl=stereo:d={core_dur:.2f},"
                         f"aformat=channel_layouts=stereo[acore]")

        # concat 要求每个片段的 [视频][音频] 成对交替输入
        segments = []
        if intro > 0:
            parts.append(f"[{i_aintro}:a]aformat=channel_layouts=stereo[aintro]")
            segments.append(("[vintro]", "[aintro]"))
        segments.append(("[vcore]", "[acore]"))
        if outro > 0:
            parts.append(f"[{i_aoutro}:a]aformat=channel_layouts=stereo[aoutro]")
            segments.append(("[voutro]", "[aoutro]"))

        n = len(segments)
        chain = "".join(v + a for v, a in segments)
        parts.append(chain + f"concat=n={n}:v=1:a=1[av][aa]")

        if bgm_ok:
            vol = float(tpl.get("bgm_volume") or 0.12)
            parts.append(f"[{i_bgm}:a]volume={vol},aformat=channel_layouts=stereo[abgm]")
            parts.append("[aa][abgm]amix=inputs=2:duration=first:dropout_transition=0[aout]")
            amap = "[aout]"
        else:
            amap = "[aa]"

        filter_complex = ";\n".join(parts)
        with open(os.path.join(tmpdir, "filter.txt"), "w", encoding="utf-8") as fh:
            fh.write(filter_complex)

        out_name = f"clip_{job_id}_{datetime.now().strftime('%H%M%S')}.mp4"
        out_path = os.path.join(config.OUTPUTS, out_name)
        cmd += ["-filter_complex", filter_complex, "-map", "[av]", "-map", amap,
                "-c:v", "libopenh264", "-b:v", "5M", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                "-movflags", "+faststart", out_path]

        _log(job_id, "ffmpeg 开始渲染…")
        db.update_job(job_id, progress=45)
        p = subprocess.run(cmd, cwd=tmpdir, capture_output=True, text=True, timeout=1800)
        if p.returncode != 0:
            raise RuntimeError("ffmpeg 渲染失败：\n" + (p.stderr or "")[-1800:])
        if not os.path.exists(out_path) or os.path.getsize(out_path) < 10000:
            raise RuntimeError("未生成有效视频文件")

        odur, _, _ = ingest.probe(out_path)
        _log(job_id, f"完成：{out_name}（{odur}s，{os.path.getsize(out_path)//1024} KB）")
        db.update_job(job_id, status="done", progress=100, output_path=out_path,
                      finished_at=db.now())
    except Exception as exc:
        _log(job_id, f"失败：{exc}")
        db.update_job(job_id, status="failed", error=str(exc)[:1500],
                      finished_at=db.now())


def start_render(job_id):
    t = threading.Thread(target=render, args=(job_id,), daemon=True)
    t.start()
    return t
