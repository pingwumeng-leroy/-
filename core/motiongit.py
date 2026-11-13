# -*- coding: utf-8 -*-
"""MotionGit Creator API 客户端（纯标准库，零依赖）。

职责：为 .motion 成片准备「素材 + 配音 + 编排」，成片仍在本机渲染。
官方契约：https://motiongit.com/developers · OpenAPI: https://motiongit.com/openapi.yaml

鉴权：Authorization: Bearer <mgk_...>（用户在 motiongit.com/settings/api 拿自己的 Key）
- 免费：音色目录、能力清单、素材检索（图/视频/音乐/贴纸，每天 30 次）、贴纸匹配/下载、音乐库
- 计费：TTS（tts.speak，5 积分/100 字）、生图（images.generate，4 积分/张）
         都走 quote → generations → tasks → receipt → download 五步
"""
import json
import os
import time
import urllib.error
import urllib.request
import uuid

from . import config

DEFAULT_BASE = "https://motiongit.com"


class MotionGitError(Exception):
    def __init__(self, status, code="", message="", retry_after=None, balance=None):
        self.status = status
        self.code = code
        self.message = message or code or ("HTTP %s" % status)
        self.retry_after = retry_after
        self.balance = balance
        super().__init__("[%s] %s" % (code or status, self.message))


def _cfg():
    return (config.get_settings().get("motiongit") or {})


def base_url():
    return (_cfg().get("base_url") or DEFAULT_BASE).rstrip("/")


def api_key():
    return (_cfg().get("api_key") or "").strip()


def available():
    return bool(_cfg().get("enabled") and api_key())


def _headers(extra=None):
    h = {"Accept": "application/json", "User-Agent": "douyin-studio/1.0 (+motiongit)"}
    k = api_key()
    if k:
        h["Authorization"] = "Bearer " + k
    if extra:
        h.update(extra)
    return h


def _request(method, path, body=None, headers=None, timeout=60, raw=False):
    url = base_url() + path
    data = None
    h = _headers(headers)
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            payload = r.read()
            resp_headers = {k.lower(): v for k, v in r.headers.items()}
            if raw:
                return r.status, payload, resp_headers
            try:
                return r.status, json.loads(payload.decode("utf-8")), resp_headers
            except Exception:
                return r.status, payload, resp_headers
    except urllib.error.HTTPError as e:
        try:
            j = json.loads(e.read().decode("utf-8"))
        except Exception:
            j = {}
        err = j.get("error", {}) if isinstance(j, dict) else {}
        raise MotionGitError(e.code, err.get("code", ""), err.get("message", ""),
                             err.get("retry_after"), err.get("balance"))
    except urllib.error.URLError as e:
        raise MotionGitError(0, "network_error", str(e.reason))


# ---------------------------------------------------------------------------
# 免费 / 公开
# ---------------------------------------------------------------------------
def capabilities():
    """能力与模型价格（免 Key）。"""
    _, j, _ = _request("GET", "/api/v1/capabilities")
    return j


def voices(model="volc-tts-2.0", q=None, gender=None, language=None, page=1,
           limit=50):
    """音色目录（免 Key）。language 用 'zh-cn' 这类 BCP-47 值；传错会被拒绝。"""
    qs = ["model=%s" % model, "page=%d" % page, "limit=%d" % limit]
    if q:
        qs.append("q=" + urllib.request.quote(q))
    if gender:
        qs.append("gender=" + urllib.request.quote(gender))
    if language:
        qs.append("language=" + urllib.request.quote(language))
    _, j, _ = _request("GET", "/api/v1/tts/voices?" + "&".join(qs))
    return j


def me():
    """账户与积分（需 Key）。"""
    _, j, h = _request("GET", "/api/v1/me")
    return j


def usage():
    """用量：免费素材额度、积分、模型可用性（需 Key）。"""
    _, j, _ = _request("GET", "/api/v1/usage")
    return j


# ---------------------------------------------------------------------------
# 素材（免费 / 每日 30 次）
# ---------------------------------------------------------------------------
def search(kind, q="", provider=None, page=1, limit=12, **extra):
    qs = ["kind=%s" % kind, "page=%d" % page, "limit=%d" % limit]
    if q:
        qs.append("q=" + urllib.request.quote(q))
    if provider:
        qs.append("provider=%s" % provider)
    for k, v in extra.items():
        if v is not None:
            qs.append("%s=%s" % (k, urllib.request.quote(str(v))))
    _, j, _ = _request("GET", "/api/v1/search?" + "&".join(qs))
    return j


def select_asset(source_ref):
    _, j, _ = _request("POST", "/api/v1/assets/select",
                       {"source_ref": source_ref, "purpose": "local_creation"})
    return j


def download_asset(asset_id, out_path):
    st, payload, h = _request("GET", "/api/v1/assets/%s/download" % asset_id, raw=True,
                              timeout=120)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(payload)
    return out_path


def stickers_match(text=None, keywords=None, limit=12):
    body = {"limit": limit}
    if text:
        body["text"] = text
    if keywords:
        body["keywords"] = keywords
    _, j, _ = _request("POST", "/api/v1/stickers/match", body)
    return j


# ---------------------------------------------------------------------------
# 音乐（免费库）
# ---------------------------------------------------------------------------
def music_categories():
    _, j, _ = _request("GET", "/api/v1/music/categories")
    return j


def music_search(q="", type_=None, mood=None, genre=None, limit=12, page=1):
    qs = ["limit=%d" % limit, "page=%d" % page]
    if q:
        qs.append("q=" + urllib.request.quote(q))
    for k, v in (("type", type_), ("mood", mood), ("genre", genre)):
        if v:
            qs.append("%s=%s" % (k, v))
    _, j, _ = _request("GET", "/api/v1/music/search?" + "&".join(qs))
    return j


def music_download(track_id, out_path, mode="download"):
    st, payload, h = _request("GET", "/api/v1/music/%s?mode=%s" % (track_id, mode),
                              raw=True, timeout=120)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(payload)
    return out_path


# ---------------------------------------------------------------------------
# 计费生成：quote → generations → tasks → receipt → download
# ---------------------------------------------------------------------------
def quote(capability, model, input_obj):
    _, j, _ = _request("POST", "/api/v1/quotes",
                       {"capability": capability, "model": model, "input": input_obj})
    return j


def submit(quote_obj, idempotency_key=None):
    """按报价提交生成任务。返回 {task_id, status_url, replayed}。"""
    ikey = idempotency_key or ("studio-" + uuid.uuid4().hex)
    body = {
        "quote_id": quote_obj["quote_id"],
        "capability": quote_obj["capability"],
        "model": quote_obj["model"],
        "input": quote_obj["input"],
        "max_credits": quote_obj["max_credits"],
    }
    _, j, _ = _request("POST", "/api/v1/generations", body,
                       headers={"Idempotency-Key": ikey})
    return j


def task(task_id):
    _, j, _ = _request("GET", "/api/v1/tasks/%s" % task_id)
    return j


def wait_task(task_id, timeout=300, interval=2.0, on_progress=None):
    """轮询任务直到终态。返回 CreatorTask。"""
    terminal = {"succeeded", "partially_succeeded", "failed", "cancelled"}
    start = time.time()
    while True:
        t = task(task_id)
        st = t.get("status", "")
        if on_progress:
            try:
                on_progress(st, t.get("stage", ""))
            except Exception:
                pass
        if st in terminal:
            return t
        if time.time() - start > timeout:
            raise MotionGitError(0, "timeout", "任务超时未完成（%ss）" % timeout)
        time.sleep(interval)


def _pick_audio_asset(t):
    """从任务产物里找音频素材 id。"""
    assets = t.get("result_assets") or []
    for a in assets:
        if (a.get("kind") == "audio") or (str(a.get("mime", "")).startswith("audio/")):
            return a.get("id")
    # 兜底：取第一个有 id 的
    for a in assets:
        if a.get("id"):
            return a["id"]
    return None


def tts(text, voice=None, model="volc-tts-2.0", sample_rate=None,
        delivery_instruction=None, out_path=None, max_credits=None,
        idempotency_key=None, on_progress=None, poll_timeout=300):
    """文本转配音：完整五步流程。返回 {audio_path, duration, narration, task, credits}。

    voice 不传则用平台默认音色。sample_rate：volc-tts 仅 24000；volc-tts-2.0 支持
    24000/48000（默认 48000）。delivery_instruction 仅 2.0 支持。
    """
    text = (text or "").strip()
    if not text:
        raise MotionGitError(0, "empty_text", "配音文本为空")
    if len(text) > 2000:
        raise MotionGitError(0, "text_too_long", "单次配音最多 2000 字，请拆分")
    inp = {"text": text}
    if voice:
        inp["voice"] = voice
    if sample_rate:
        inp["sample_rate"] = int(sample_rate)
    if delivery_instruction and model == "volc-tts-2.0":
        inp["delivery_instruction"] = delivery_instruction[:500]

    q = quote("tts.speak", model, inp)
    if max_credits is not None:
        q["max_credits"] = min(q.get("max_credits", max_credits), max_credits)
    gen = submit(q, idempotency_key)
    tid = gen["task_id"]
    t = wait_task(tid, timeout=poll_timeout, on_progress=on_progress)
    if t.get("status") != "succeeded":
        raise MotionGitError(0, "tts_failed",
                             "配音任务失败：%s" % json.dumps(t.get("error"), ensure_ascii=False))
    aid = _pick_audio_asset(t)
    if not aid:
        raise MotionGitError(0, "no_asset", "任务成功但未找到音频产物")
    if out_path:
        download_asset(aid, out_path)
    narration = t.get("narration") or {}
    dur = narration.get("durationSec") or 0.0
    billing = t.get("billing") or {}
    return {"audio_path": out_path, "asset_id": aid, "duration": dur,
            "narration": narration, "task": t,
            "credits": billing.get("estimated_credits", 0)}


def image_generate(prompt, size="1536x1024", seed=None, out_path=None):
    """按文案生图（4 积分/张）。单步端点，返回 {id,url,...}。"""
    body = {"prompt": prompt, "size": size}
    if seed is not None:
        body["seed"] = int(seed)
    _, j, _ = _request("POST", "/api/v1/images", body, timeout=180)
    if out_path and j.get("url"):
        # url 为带鉴权的相对/绝对地址，需带 Key 下载
        _download_url(j["url"], out_path)
    return j


def _download_url(url, out_path):
    if url.startswith("/"):
        url = base_url() + url
    req = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "wb") as fh:
        fh.write(data)
    return out_path


# ---------------------------------------------------------------------------
# 便捷：给 UI 用的中文音色精选（从目录里筛常见 zh 音色，按需再拉全量）
# ---------------------------------------------------------------------------
CURATED_VOICES = [
    ("zh_male_ruyayichen_saturn_bigtts", "儒雅逸辰 · 磁性男声"),
    ("zh_female_vv_uranus_bigtts", "Vivi · 自然女声"),
    ("zh_female_xiaohe_uranus_bigtts", "小何 · 亲切女声"),
    ("zh_male_m191_uranus_bigtts", "云舟 · 沉稳男声"),
    ("zh_male_taocheng_uranus_bigtts", "小天 · 清爽男声"),
    ("zh_female_qingxinnvsheng_uranus_bigtts", "清新女生 · 明亮"),
    ("zh_male_liufei_uranus_bigtts", "刘飞 · 解说感"),
    ("zh_female_cancan_uranus_bigtts", "灿灿 · 活泼"),
    ("zh_male_sunwukong_uranus_bigtts", "孙悟空 · 戏剧化"),
    ("zh_female_tianmeixiaoyuan_uranus_bigtts", "甜美校园 · 柔和"),
]
