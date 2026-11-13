# -*- coding: utf-8 -*-
"""运行时配置与路径。所有外部能力都在这里开关，改配置即可接入自有技术。"""
import json
import os
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
UPLOADS = os.path.join(DATA, "uploads")
OUTPUTS = os.path.join(DATA, "outputs")
ASSETS = os.path.join(DATA, "assets")
CONFIG_DIR = os.path.join(ROOT, "config")
DB_PATH = os.path.join(DATA, "studio.db")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")
TEMPLATES_PATH = os.path.join(CONFIG_DIR, "edit_templates.json")
SENSITIVE_PATH = os.path.join(CONFIG_DIR, "sensitive_words.txt")
GENERATE_PROMPT_PATH = os.path.join(CONFIG_DIR, "generate_prompt.txt")

FFMPEG = os.environ.get("FFMPEG_BIN", "ffmpeg")
FFPROBE = os.environ.get("FFPROBE_BIN", "ffprobe")
FONT_BOLD = "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"
FONT_REG = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"

DEFAULT_SETTINGS = {
    "llm": {
        "enabled": False,
        "base_url": "https://api.deepseek.com",
        "api_key": "",
        "model": "deepseek-flash",
        "timeout": 90,
        "_hint": "分析走 DeepSeek（OpenAI 兼容 /chat/completions）。填好 api_key 并把 enabled 设为 true 即切换；模型名以上游实际支持为准，设置页「测试大模型连接」可自检，若报模型不存在改成 deepseek-chat 即可。关闭时用内置规则引擎，全流程仍可跑通。"
    },
    "asr": {
        "enabled": False,
        "base_url": "",
        "api_key": "",
        "model": "whisper-1",
        "_hint": "关闭时可用『手动粘贴文案』或『上传 .srt/.txt』；也可后续接自有转写服务。"
    },
    "fetch": {
        "douyin_cookie": "",
        "prefer_height": 720,
        "_hint": "抖音采集建议填入浏览器 Cookie；也可完全不走采集，直接上传本地视频。"
    },
    "company": {
        "name": "四川合胜宏科技集团有限公司",
        "short": "合胜宏企服",
        "city": "成都"
    },
    "auth": {
        "enabled": False,
        "username": "boss",
        "password": "",
        "_hint": "默认关闭，仅限内网使用；若要放到公网，务必开启并挂 HTTPS 反向代理。"
    },
    "search": {
        "provider": "mock",
        "tikhub": {
            "base_url": "https://api.tikhub.io",
            "api_key": "",
            "timeout": 30
        },
        "_hint": "provider=mock 用内置演示数据跑通全链路；填好 TikHub Key 后改成 tikhub 即切真实数据，代码零改动。"
    },
    "motiongit": {
        "enabled": False,
        "base_url": "https://motiongit.com",
        "api_key": "",
        "default_model": "volc-tts-2.0",
        "default_voice": "zh_male_ruyayichen_saturn_bigtts",
        "prefer_for_voice": True,
        "_hint": "MotionGit Creator API：给自己的 .motion 模板准备素材与配音（成片仍在本地渲染）。在 motiongit.com/settings/api 生成 Key（mgk_ 开头）填这里，充值后即可用高品质配音（5 积分/100 字）与生图（4 积分/张）；素材/贴纸/音乐检索免费（每天 30 次）。"
    },
    "harvest": {
        "default_count": 15,
        "max_calls": 60,
        "comment_pages": 3,
        "use_asr": False,
        "whisper_model": "small",
        "_hint": "单次批量任务的默认报告数与调用预算上限；comment_pages 为每条视频的评论翻页数（官方每页固定 20 条，每翻一页计费一次）；use_asr 开启完整档：用 TikHub play_url 下载视频 → ffmpeg 抽音频 → 本地 faster-whisper 转写带时间戳 SRT（需 pip install faster-whisper，模型约 500MB）；whisper_model 可选 tiny/base/small/medium，越大越准越慢；下载或转写失败自动回落轻量档，不影响整批任务。"
    }
}

DEFAULT_TEMPLATES = [
    {
        "id": "standard",
        "name": "标准口播（推荐）",
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "bg_color": "#12161d",
        "accent_color": "#ff6a3d",
        "title_size": 76,
        "subtitle_size": 58,
        "subtitle_color": "#ffffff",
        "intro_seconds": 1.6,
        "outro_seconds": 1.8,
        "outro_text": "成都办企业，找合胜宏",
        "watermark": "合胜宏企服",
        "trim_head": 0.0,
        "trim_tail": 0.0,
        "speed": 1.0,
        "bgm": "",
        "bgm_volume": 0.12,
        "burn_subtitle": True,
        "subtitle_from_script": True
    },
    {
        "id": "plain",
        "name": "纯净版（无片头片尾）",
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "bg_color": "#0b0d11",
        "accent_color": "#ffffff",
        "title_size": 72,
        "subtitle_size": 56,
        "subtitle_color": "#ffffff",
        "intro_seconds": 0,
        "outro_seconds": 0,
        "outro_text": "",
        "watermark": "",
        "trim_head": 0.0,
        "trim_tail": 0.0,
        "speed": 1.0,
        "bgm": "",
        "bgm_volume": 0.1,
        "burn_subtitle": True,
        "subtitle_from_script": True
    },
    {
        "id": "policy",
        "name": "政策科普（大字标题）",
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "bg_color": "#0e1a24",
        "accent_color": "#3fa9f5",
        "title_size": 88,
        "subtitle_size": 62,
        "subtitle_color": "#ffffff",
        "intro_seconds": 2.0,
        "outro_seconds": 2.0,
        "outro_text": "政策以主管机关现行规定为准",
        "watermark": "合胜宏企服",
        "trim_head": 0.0,
        "trim_tail": 0.0,
        "speed": 1.0,
        "bgm": "",
        "bgm_volume": 0.1,
        "burn_subtitle": True,
        "subtitle_from_script": True
    }
]

_lock = threading.Lock()


def _ensure():
    try:
        for d in (DATA, UPLOADS, OUTPUTS, ASSETS, CONFIG_DIR):
            os.makedirs(d, exist_ok=True)
        if not os.path.exists(SETTINGS_PATH):
            _write(SETTINGS_PATH, DEFAULT_SETTINGS)
        if not os.path.exists(TEMPLATES_PATH):
            _write(TEMPLATES_PATH, DEFAULT_TEMPLATES)
    except PermissionError as exc:
        # 目录权限被改坏时，别只抛一个裸 Errno 13：直接给出可执行的修复命令。
        # get_settings() 每次请求都会走到这里，所以不处理会让整个站点只剩一段 JSON。
        raise RuntimeError(
            "数据/配置目录不可写：%s。请在项目根目录执行："
            "sudo chgrp -R www-data data config && "
            "sudo chmod -R g+rwX data config && "
            "sudo chmod o+rx . && sudo systemctl restart douyin-studio"
            % exc
        ) from exc


def _write(path, obj):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=2)


def _read(path, fallback):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return fallback


def get_settings():
    _ensure()
    s = _read(SETTINGS_PATH, DEFAULT_SETTINGS)
    # 补齐新增键
    for k, v in DEFAULT_SETTINGS.items():
        s.setdefault(k, v)
    return s


def save_settings(data):
    _ensure()
    with _lock:
        cur = _read(SETTINGS_PATH, DEFAULT_SETTINGS)
        for key in ("llm", "asr", "fetch", "company", "auth", "search", "harvest", "motiongit"):
            if key in data and isinstance(data[key], dict):
                cur.setdefault(key, {})
                for k, v in data[key].items():
                    # search.tikhub 这类二级配置做合并，避免只改 api_key 时丢掉 base_url
                    if isinstance(v, dict) and isinstance(cur[key].get(k), dict):
                        cur[key][k].update(v)
                    else:
                        cur[key][k] = v
        _write(SETTINGS_PATH, cur)
    return get_settings()


def get_templates():
    _ensure()
    return _read(TEMPLATES_PATH, DEFAULT_TEMPLATES)


def get_template(tid):
    for t in get_templates():
        if t.get("id") == tid:
            return t
    return get_templates()[0]


def save_templates(items):
    _ensure()
    with _lock:
        _write(TEMPLATES_PATH, items)
    return items


def get_sensitive_words():
    _ensure()
    if not os.path.exists(SENSITIVE_PATH):
        return []
    with open(SENSITIVE_PATH, "r", encoding="utf-8") as fh:
        return [ln.strip() for ln in fh if ln.strip() and not ln.strip().startswith("#")]


def get_generate_prompt():
    _ensure()
    if os.path.exists(GENERATE_PROMPT_PATH):
        with open(GENERATE_PROMPT_PATH, "r", encoding="utf-8") as fh:
            return fh.read()
    return ""
