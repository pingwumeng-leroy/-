# -*- coding: utf-8 -*-
"""MotionGit .motion 模板接入 + 无原片 TTS 成片。

两类出片能力，都从「一份定稿文案」出发，不再依赖口播原片：

  A) MotionGit 真对接：用户把官网下载的 .motion 工程包放到服务器（data/motion/），
     本模块解包、读取包内 schema（params/catalog/content），把文案映射成包要的
     user.json 输入，调用包自带渲染管线（adapter.py / render.mjs / headless chrome）
     出片，动效 100% 还原官网模板。
  B) 无原片 TTS 成片（内置兜底）：没有 .motion 包时，用 edge-tts 念文案当配音，
     配 PIL 生成的卡片画面 + ffmpeg 合成竖屏 mp4，零外部依赖、零成本。

设计原则：包无关（package-agnostic）。不写死任何模板的字段，全部从包内
schema 动态发现；映射文案时用启发式 + 明确提示，凡拿不准的槽位留给人工补。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import zipfile
from PIL import Image, ImageDraw

from . import config, db, tts, motiongit
from .editor import _font, _hex, _wrap  # 复用字体/换行/取色工具

MOTION_DIR = os.path.join(config.DATA, "motion")

# ----------------------------------------------------------------------------
# 1. 包管理：解包 + 发现入口与 schema
# ----------------------------------------------------------------------------
class MotionPackage:
    """一个已解包的 .motion 工程。所有字段都是从包内文件动态发现的。"""

    def __init__(self, name, root):
        self.name = name
        self.root = root
        self.meta = self._discover()

    def __repr__(self):
        return "<MotionPackage %s %s>" % (self.name, self.meta.get("profile"))

    @classmethod
    def install(cls, motion_path, name=None):
        """解包一个 .motion 到 data/motion/<name>/。返回 MotionPackage。"""
        os.makedirs(MOTION_DIR, exist_ok=True)
        name = name or re.sub(r"[^\w一-鿿-]+", "_",
                              os.path.splitext(os.path.basename(motion_path))[0]).strip("_") or "package"
        dest = os.path.join(MOTION_DIR, name)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        os.makedirs(dest, exist_ok=True)
        with zipfile.ZipFile(motion_path) as z:
            z.extractall(dest)
        # 有些包首层套一个同名目录，拉平一层，方便找 adapter/params
        entries = [e for e in os.listdir(dest)
                   if os.path.isdir(os.path.join(dest, e))]
        if len(entries) == 1 and not any(
                os.path.exists(os.path.join(dest, f))
                for f in ("adapter.py", "render.mjs", "index.html", "motion.json")):
            sub = os.path.join(dest, entries[0])
            for item in os.listdir(sub):
                shutil.move(os.path.join(sub, item), os.path.join(dest, item))
            os.rmdir(sub)
        return cls(name, dest)

    def _find(self, *names):
        for n in names:
            p = os.path.join(self.root, n)
            if os.path.exists(p):
                return p
            p2 = os.path.join(self.root, "core", n)
            if os.path.exists(p2):
                return p2
        return None

    def _discover(self):
        meta = {"name": self.name, "root": self.root, "profile": "unknown",
                "entry": None, "layouts": ["landscape"], "requires_presenter": False,
                "voice_modes": ["none", "file"], "music_modes": ["none", "file"],
                "scenes": [], "content_fields": [], "basic_keys": [],
                "params_schema": None, "notes": []}
        # ---- 入口：三类运行时，优先级 adapter > render.mjs > index.html ----
        if os.path.exists(os.path.join(self.root, "adapter.py")):
            meta["entry"] = "adapter.py"
            meta["profile"] = "python-adapter"
        elif os.path.exists(os.path.join(self.root, "render.mjs")):
            meta["entry"] = "render.mjs"
            meta["profile"] = "node-render"
        elif os.path.exists(os.path.join(self.root, "index.html")):
            meta["entry"] = "index.html"
            meta["profile"] = "html-canvas"
        else:
            meta["notes"].append("未找到已知入口（adapter.py / render.mjs / index.html）")

        # ---- params.schema.json：输入校验 schema ----
        sp = self._find("params.schema.json")
        if sp:
            try:
                meta["params_schema"] = json.load(open(sp, encoding="utf-8"))
            except Exception as e:
                meta["notes"].append("params.schema.json 解析失败：%s" % e)

        # ---- catalog.json：原生单元（场景）定义 ----
        sc = self._find("catalog.json")
        if sc:
            try:
                cat = json.load(open(sc, encoding="utf-8"))
                groups = cat.get("groups", [])
                meta["scenes"] = [{"id": g.get("id"),
                                  "start": g.get("start"), "end": g.get("end"),
                                  "fields": g.get("fields", []),
                                  "media": g.get("media", [])}
                                 for g in groups]
                if "mediaPorts" in cat:
                    meta["media_ports"] = cat["mediaPorts"]
            except Exception as e:
                meta["notes"].append("catalog.json 解析失败：%s" % e)

        # ---- content.schema.json：可填文字槽位 ----
        cs = self._find("content.schema.json")
        if cs:
            try:
                csc = json.load(open(cs, encoding="utf-8"))
                fields = csc.get("fields", [])
                meta["content_fields"] = [{"id": f.get("id"),
                                          "maxChars": f.get("maxChars", 0),
                                          "desc": f.get("desc", "")}
                                         for f in fields]
            except Exception as e:
                meta["notes"].append("content.schema.json 解析失败：%s" % e)

        # ---- 从 START-HERE / motion.json / guide 推断画幅与是否要真人 ----
        for doc in ("00-START-HERE.md", "motion.json", "guide.json", "CREATION.md",
                    "README.md"):
            p = os.path.join(self.root, doc)
            if not os.path.exists(p):
                continue
            txt = open(p, encoding="utf-8", errors="ignore").read()
            if "竖屏" in txt or "portrait" in txt.lower():
                if "portrait" not in meta["layouts"]:
                    meta["layouts"].append("portrait")
            if "横屏" in txt or "landscape" in txt.lower():
                if "landscape" not in meta["layouts"]:
                    meta["layouts"].append("landscape")
            if "adu-authored-unit" in txt or "真人" in txt or "人物视频" in txt:
                meta["requires_presenter"] = True
            # 从 params.schema 的 layout enum 取精确支持画幅
        ps = meta["params_schema"]
        if ps:
            for prop in ps.get("properties", {}).get("layout", {}).get("enum", []):
                if prop not in meta["layouts"]:
                    meta["layouts"].append(prop)
            vm = ps.get("properties", {}).get("media", {}).get("properties", {}) \
                     .get("voice", {}).get("properties", {}).get("mode", {}).get("enum")
            if vm:
                meta["voice_modes"] = vm
        # basic 键：从场景 fields 里若出现 B1/B2/B3/B4 的字段推断
        bkeys = set()
        for s in meta["scenes"]:
            for f in s.get("fields", []):
                m = re.match(r"^(B[1-4])\.", f)
                if m:
                    bkeys.add(m.group(1))
        meta["basic_keys"] = sorted(bkeys)
        if not meta["basic_keys"]:
            # 没有显式 basic 场景时，content_fields 即全部可填文字槽
            meta["basic_keys"] = [f["id"] for f in meta["content_fields"]]
        return meta

    def has_engine(self):
        return self.meta.get("entry") in ("adapter.py", "render.mjs", "index.html")

    REQUIRED = ("motion.json", "params.schema.json", "integrity.json", "render.mjs")

    def missing_files(self):
        """判断包是否完整（截断下载/解包失败会缺这些关键文件）。

        adapter 链路真正要跑起来缺一不可：integrity.json 供 adapter 自校验、
        params.schema.json 供输入校验、render.mjs 供逐帧渲染。
        index.html 形态的包只要求 index.html。
        """
        if self.meta.get("entry") == "index.html":
            return [] if self._find("index.html") else ["index.html"]
        miss = []
        for f in self.REQUIRED:
            if not self._find(f):
                miss.append(f)
        return miss

    @property
    def complete(self):
        return not self.missing_files()


def list_packages():
    os.makedirs(MOTION_DIR, exist_ok=True)
    out = []
    for n in sorted(os.listdir(MOTION_DIR)):
        root = os.path.join(MOTION_DIR, n)
        if os.path.isdir(root) and not n.startswith("."):
            try:
                pkg = MotionPackage(n, root)
                meta = pkg.meta
                meta["missing"] = pkg.missing_files()
                meta["complete"] = not meta["missing"]
                out.append(meta)
            except Exception as e:
                out.append({"name": n, "error": str(e)})
    return out


def get_package(name):
    root = os.path.join(MOTION_DIR, name)
    if not os.path.isdir(root):
        return None
    return MotionPackage(name, root)


def remove_package(name):
    root = os.path.join(MOTION_DIR, name)
    if os.path.isdir(root):
        shutil.rmtree(root)
        return 1
    return 0


def remove_all_packages():
    if not os.path.isdir(MOTION_DIR):
        return 0
    n = 0
    for nme in os.listdir(MOTION_DIR):
        p = os.path.join(MOTION_DIR, nme)
        if os.path.isdir(p) and not nme.startswith("."):
            shutil.rmtree(p)
            n += 1
    return n


# ----------------------------------------------------------------------------
# 2. 文案 → 包输入（schema 驱动，启发式映射）
# ----------------------------------------------------------------------------
def _sentences(text, maxn=6):
    text = re.sub(r"[ \t　]+", " ", (text or "").strip())
    parts = re.split(r"(?<=[。！？!?；;])", text)
    out = [p.strip() for p in parts if p.strip()]
    return out[:maxn]


def _bullets(text, maxn=5):
    """把正文拆成要点（按换行/句号），用于 B2 points 等。"""
    lines = []
    for ln in (text or "").splitlines():
        ln = ln.strip().lstrip("-•·*0123456789.、").strip()
        if not ln:
            continue
        for seg in re.split(r"[。！？!?；;]", ln):
            seg = seg.strip("，,、 ")
            if seg:
                lines.append(seg)
        if len(lines) >= maxn:
            break
    return lines[:maxn]


def build_input(script, package, opts):
    """根据包 schema，把一份定稿文案映射成 user.json 输入。

    opts 可带：
      selected   : 用户选定的场景 id 列表（来自前端场景选择器）
      sound_effects: 'native' / 'none'
      voice      : tts 音色名（用 edge-tts 生成配音并绑定）或 None（不配音）
      music      : 'none' / 文件路径
    返回 (user_input_dict, voice_audio_path_or_None)
    """
    meta = package.meta
    body = (script.get("body") or "").strip()
    title = (script.get("title") or "").strip() or "企业服务那些事"
    company = config.get_settings().get("company", {}).get("short", "合胜宏企服")

    layout = "portrait" if "portrait" in meta["layouts"] else "landscape"
    ui = {
        "mode": "user-media",
        "language": "zh",
        "layout": layout,
        "title": title[:45],
        "presenterLabel": company[:45],
    }

    # ---- 场景选择 ----
    scenes = meta["scenes"]
    sel = opts.get("selected") or []
    if not sel and scenes:
        # 没选就给个默认：取前 1–4 个（不超过包上限），或所有（若 ≤30）
        sel = [s["id"] for s in scenes][:4]
    if scenes:
        chosen = []
        for sid in sel:
            g = next((s for s in scenes if s["id"] == sid), None)
            if not g:
                continue
            chosen.append({"id": sid, "title": title[:45]})
        ui["scenes"] = chosen
    else:
        ui["scenes"] = sel  # 没有 catalog 时直接透传

    # 包必须真人素材但用户没有：不拦，放进 ui 让渲染阶段给出明确提示
    if bool(meta.get("requires_presenter")) and not opts.get("presenter"):
        ui["_notes"] = ["该模板需要真人出镜素材，本次按无真人方式出片，成片可能留空位"]

    # ---- basic 表达（B1–B4 等）----
    sents = _sentences(body)
    bullets = _bullets(body)
    basic = {}
    bk = set(meta["basic_keys"])
    # 仅在包声明了这些键时才填，避免往无关包塞字段
    if "B1" in bk:
        basic["keyword"] = (sents[0][:8] if sents else title[:8])
        basic["explanation"] = (sents[0] if sents else body[:42])[:42]
    if "B2" in bk:
        basic["heading"] = title[:10]
        basic["points"] = [(b[:18]) for b in bullets] or [body[:18]]
    if "B3" in bk:
        basic["heading"] = title[:10]
        basic["left"] = (bullets[0] if bullets else body[:8])[:8]
        basic["right"] = (bullets[1] if len(bullets) > 1 else "对比项")[:8]
        basic["conclusion"] = (sents[-1] if sents else title)[:48]
    if "B4" in bk:
        basic["summary"] = title[:10]
        basic["action"] = (sents[-1] if sents else "欢迎咨询")[:42]
    # 通用：把可填文字槽也尽量填
    for f in meta["content_fields"]:
        fid = f["id"]
        if fid in basic:
            continue
        limit = f.get("maxChars", 0) or 200
        basic[fid] = (body[:limit] or title[:limit])
    if basic:
        ui["basic"] = basic

    # ---- content：把整段文案放进第一个内容槽，供包内自由使用 ----
    cf = meta["content_fields"]
    if cf and "content" not in ui:
        ui["content"] = {cf[0]["id"]: body[: (cf[0].get("maxChars", 0) or 2000)]}

    # ---- media：默认无真人；配音用 edge-tts；音乐 none ----
    voice_path = None
    media = {"presenter": {"mode": "none"}}
    if "file" in meta["voice_modes"] and opts.get("voice"):
        # 生成配音，prepare 阶段按相对路径绑定
        voice_path = opts.get("_voice_out")
        if voice_path and os.path.exists(voice_path):
            media["voice"] = {"mode": "file", "file": os.path.basename(voice_path)}
        else:
            media["voice"] = {"mode": "none"}
    else:
        media["voice"] = {"mode": "none"}
    if opts.get("music") and "file" in meta["music_modes"]:
        media["music"] = {"mode": "file", "file": opts["music"],
                          "license": "user-provided"}
    else:
        media["music"] = {"mode": "none"}
    ui["media"] = media
    ui["soundEffects"] = opts.get("sound_effects", "native" if meta["entry"] else "none")
    ui["captions"] = []
    return ui, voice_path


# ----------------------------------------------------------------------------
# 3. 渲染：真对接 MotionGit
# ----------------------------------------------------------------------------
_PY_CANDIDATES = ("/usr/bin/python3.12", "/usr/bin/python3.11", "/usr/bin/python3",
                  sys.executable)

# adapter.py 内联声明的依赖（PEP-723 里的 dependencies）
_ADAPTER_DEPS = ("numpy", "scipy", "soundfile", "pyloudnorm", "jsonschema")


def _py_has_deps(py):
    """这个解释器是否已装齐 adapter 依赖。"""
    code = "import " + ", ".join(_ADAPTER_DEPS)
    try:
        p = subprocess.run([py, "-c", code], capture_output=True, timeout=20)
        return p.returncode == 0
    except Exception:
        return False


def _py_exec():
    """返回能直接跑 adapter.py 的解释器路径；都不行则返回 None（改走 uv）。"""
    for py in _PY_CANDIDATES:
        if py and os.path.exists(py) and _py_has_deps(py):
            return py
    return None


def _adapter_cmd(script="adapter.py"):
    """adapter 执行前缀。

    首选「依赖已就绪的解释器」直接跑：`uv run` 在网络差的机器上会长时间卡在
    下载 numpy/scipy（实测 30MB 级 wheel 在慢网下几分钟都不动），而模板包只跑
    一次配色/合成，不需要隔离环境。依赖没装时才回退 uv（--no-project 避免
    uv 在服务器目录里找 pyproject 而挂死）。
    """
    py = _py_exec()
    if py:
        return [py, script]
    base = ["uv", "run", "--no-project"]
    for c in _PY_CANDIDATES:
        if os.path.exists(c):
            base += ["--python", c]
            break
    return base + [script]



def _run(cmd, cwd, log_fn, timeout=1800):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if p.stdout:
        log_fn(p.stdout[-1500:])
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "命令失败")[-1800:])
    return p


def _synthesize(text, opts, out_path, log_fn):
    """统一配音入口：优先 MotionGit 平台高品质 TTS，否则回落 edge-tts。

    opts 可带：
      voice_provider : 'motiongit' / 'edge'（空则按设置自动）
      mg_voice       : MotionGit 音色 id
      mg_model       : 'volc-tts-2.0' / 'volc-tts'
      voice          : edge-tts 音色名（xiaoxiao 等）
    返回 (audio_path, info_dict)。失败会抛错，由调用方兜底。
    """
    mg = config.get_settings().get("motiongit", {}) or {}
    provider = (opts.get("voice_provider") or "").lower()
    use_mg = provider == "motiongit" or (
        not provider and motiongit.available() and mg.get("prefer_for_voice"))
    if use_mg and motiongit.available():
        voice = opts.get("mg_voice") or mg.get("default_voice")
        model = opts.get("mg_model") or mg.get("default_model", "volc-tts-2.0")
        res = motiongit.tts(text, voice=voice, model=model, out_path=out_path,
                            on_progress=lambda st, stage: log_fn("配音任务 %s %s" % (st, stage)))
        log_fn("MotionGit 配音完成：%s（%.1fs · %s 积分）" %
               (os.path.basename(out_path), res.get("duration", 0), res.get("credits", 0)))
        return out_path, res
    # edge-tts 兜底（零成本、需联网）
    voice = opts.get("voice") or "xiaoxiao"
    dur = tts.synthesize(text, out_path, voice=voice)
    log_fn("edge-tts 配音完成：%s（%.1fs）" % (os.path.basename(out_path), dur))
    return out_path, {"duration": dur}


def render_motion(job_id):
    """后台线程：用真实 .motion 包出片。成功写 output_path + status=done。"""
    job = db.get_job(job_id)
    if not job:
        return
    tmp = os.path.join(config.ASSETS, "job_%d" % job_id, "motion")
    os.makedirs(tmp, exist_ok=True)
    try:
        db.update_job(job_id, status="running", progress=5)
        script = db.get_script(job.get("script_id")) or {}
        pkg_name = job.get("motion_package") or ""
        pkg = get_package(pkg_name)
        if not pkg or not pkg.has_engine():
            raise RuntimeError("找不到可用的 MotionGit 包：%s（请先在「设置/模板」上传 .motion）" % pkg_name)
        miss = pkg.missing_files()
        if miss:
            raise RuntimeError(
                "模板包「%s」不完整，缺少：%s。请重新从 MotionGit 官网下载完整 .motion 再上传"
                "（缺文件通常是下载被截断）。" % (pkg.name, "、".join(miss)))
        meta = pkg.meta
        _log(job_id, "已载入模板包 %s（%s，画幅 %s）" %
             (pkg.name, meta.get("profile"), "/".join(meta.get("layouts", []))))

        db.update_job(job_id, progress=12)
        # 生成配音（若需要）
        voice_out = None
        opts = json.loads(job.get("motion_input") or "{}") if isinstance(job.get("motion_input"), str) else (job.get("motion_input") or {})
        if opts.get("voice") or opts.get("voice_provider") or opts.get("mg_voice"):
            voice_out = os.path.join(tmp, "voice.mp3")
            try:
                opts = dict(opts)
                opts["_voice_out"] = voice_out
                _synthesize((script.get("body") or ""), opts, voice_out,
                            lambda m: _log(job_id, m))
            except Exception as e:
                _log(job_id, "配音生成失败，改为无配音出片：%s" % e)
                voice_out = None
                opts.pop("voice", None)

        # 构建 user.json
        ui, _ = build_input(script, pkg, {**opts, "_voice_out": voice_out})
        user_json = os.path.join(tmp, "user.json")
        json.dump(ui, open(user_json, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        _log(job_id, "已生成 user.json（%d 个场景 / %d 个文字槽）" %
             (len(ui.get("scenes", []) or []), len(ui.get("content", {}))))
        db.update_job(job_id, progress=22)

        root = pkg.root
        # 一次性依赖：有 package.json 才 npm ci（带缓存标记）
        if os.path.exists(os.path.join(root, "package.json")):
            if not os.path.exists(os.path.join(root, ".npm_ci_done")):
                _log(job_id, "安装包内 npm 依赖…")
                _run(["npm", "ci"], root, lambda m: _log(job_id, m), timeout=600)
                open(os.path.join(root, ".npm_ci_done"), "w").close()
        db.update_job(job_id, progress=35)

        plan_out = os.path.join(tmp, "plan.json")
        instance = os.path.join(tmp, "instance")
        if os.path.exists(instance):
            shutil.rmtree(instance)

        if meta["entry"] == "adapter.py":
            # 标准 Python adapter：plan → prepare → render（render 内部调 mix）
            uv = _adapter_cmd()
            _log(job_id, "运行模板适配器（引擎：%s）…" % uv[0])
            _run(uv + ["adapter.py", "plan", "--input", user_json,
                       "--out", plan_out], root, lambda m: _log(job_id, m), timeout=600)
            _log(job_id, "plan 校验通过")
            db.update_job(job_id, progress=48)
            _run(uv + ["adapter.py", "prepare", "--input", user_json,
                       "--out", instance], root, lambda m: _log(job_id, m), timeout=900)
            db.update_job(job_id, progress=62)
            silent = os.path.join(tmp, "silent.mp4")
            _run(uv + ["adapter.py", "render", "--instance", instance,
                       "--out", silent], root, lambda m: _log(job_id, m), timeout=1800)
            # adapter 的 render 会再调 mix.py 产出 silent-with-audio.mp4
            produced = os.path.join(tmp, "silent-with-audio.mp4")
            if not os.path.exists(produced):
                produced = silent
        elif meta["entry"] == "render.mjs":
            _run(["node", "render.mjs", "--input", user_json, "--out",
                  os.path.join(tmp, "out.mp4")], root, lambda m: _log(job_id, m),
                 timeout=1800)
            produced = os.path.join(tmp, "out.mp4")
        else:  # index.html：headless chrome 读 window.MP/SEEK/DURATION
            produced = _render_html_canvas(job_id, pkg, ui, tmp)

        if not os.path.exists(produced) or os.path.getsize(produced) < 10000:
            raise RuntimeError("模板未产出有效视频文件")

        out_name = "clip_%d_%s.mp4" % (job_id, meta.get("profile"))
        out_path = os.path.join(config.OUTPUTS, out_name)
        shutil.copy2(produced, out_path)
        odur, _, _ = 0.0, 0, 0
        try:
            from . import ingest
            odur, _, _ = ingest.probe(out_path)
        except Exception:
            pass
        _log(job_id, "完成：%s（%ss，%d KB）" %
             (out_name, odur, os.path.getsize(out_path) // 1024))
        db.update_job(job_id, status="done", progress=100, output_path=out_path,
                      finished_at=db.now())
    except Exception as exc:
        _log(job_id, "失败：%s" % exc)
        db.update_job(job_id, status="failed", error=str(exc)[:1500],
                      finished_at=db.now())


def _render_html_canvas(job_id, pkg, ui, tmp):
    """最简 html-canvas 接口（window.MP / SEEK / DURATION）的通用宿主。

    包自带 index.html，挂载 canvas 并暴露 window.MP（参数）、window.SEEK(t)、
    window.DURATION。逐帧调用 SEEK 后截图，ffmpeg 合成静帧视频，再叠配音。
    仅当包采用此最简接口时启用；带 adapter.py 的包走上面的标准流程。
    """
    from . import ingest
    root = pkg.root
    dur = 10.0
    fps = 30
    W, H = 1080, 1920
    # 先用无头 Chrome 探 DURATION 与画布尺寸
    probe_js = """
      const path=require('path');
      const {chromium}=require('%s');
      (async()=>{
        const b=await chromium.launch();
        const pg=await b.newPage();
        await pg.goto('file://'+process.argv[2]);
        await pg.waitForFunction('typeof window.MP!=="undefined"||typeof window.DURATION!=="undefined"',{timeout:15000}).catch(()=>{});
        const info=await pg.evaluate(()=>{
          window.MP = window.MP||%s;
          if(typeof window.SEEK==='function') window.SEEK(0);
          return {dur: (typeof window.DURATION==='number'?window.DURATION:10),
                  w: (document.querySelector('canvas')||{}).width||1080,
                  h: (document.querySelector('canvas')||{}).height||1920};
        });
        require('fs').writeFileSync(process.argv[3], JSON.stringify(info));
        await b.close();
      })().catch(e=>{require('fs').writeFileSync(process.argv[3],JSON.stringify({err:String(e)}));process.exit(0);});
    """ % (os.path.join(os.path.dirname(__file__), "..", "node_modules", "playwright"),
           json.dumps(ui, ensure_ascii=False))
    pj = os.path.join(tmp, "probe.js")
    open(pj, "w").write(probe_js)
    _run(["node", pj, os.path.join(root, "index.html"), os.path.join(tmp, "info.json")],
         tmp, lambda m: _log(job_id, m), timeout=120)
    info = json.load(open(os.path.join(tmp, "info.json")))
    if info.get("err"):
        raise RuntimeError("html-canvas 探测失败：" + info["err"])
    dur, W, H = info.get("dur", 10), int(info.get("w", 1080)), int(info.get("h", 1920))
    frames = max(1, int(dur * fps))
    frame_tpl = os.path.join(tmp, "f%05d.png")
    open(os.path.join(tmp, "frame.js"), "w").write(probe_js.replace(
        "if(typeof window.SEEK==='function') window.SEEK(0);",
        "for(let i=0;i<%d;i++){window.SEEK(i/%d);await page.screenshot({path:'%s'.replace('%%05d',String(i).padStart(5,'0'))});}"
        % (frames, fps, frame_tpl)))
    _run(["node", os.path.join(tmp, "frame.js"), os.path.join(root, "index.html"),
          os.path.join(tmp, "dummy.json")], tmp, lambda m: _log(job_id, m), timeout=1200)
    silent = os.path.join(tmp, "silent.mp4")
    _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-framerate", str(fps),
          "-i", frame_tpl, "-c:v", "libopenh264", "-b:v", "4M", "-pix_fmt", "yuv420p",
          "-movflags", "+faststart", silent], tmp, lambda m: _log(job_id, m), timeout=600)
    # 叠配音（若 user.json 带了 voice file）
    voice = (ui.get("media", {}).get("voice", {}) or {})
    if voice.get("mode") == "file":
        vp = os.path.join(root, voice["file"])
        if os.path.exists(vp):
            final = os.path.join(tmp, "silent-with-audio.mp4")
            _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", silent,
                  "-i", vp, "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                  "-shortest", final], tmp, lambda m: _log(job_id, m), timeout=300)
            return final
    return silent


# ----------------------------------------------------------------------------
# 4. 内置兜底：无原片 TTS 卡片成片
# ----------------------------------------------------------------------------
def _card_image(path, title, text, idx, total, accent="#ff6a3d", bg="#0e1a24"):
    W, H = 1080, 1920
    img = Image.new("RGB", (W, H), _hex(bg))
    d = ImageDraw.Draw(img)
    # 顶部色条
    d.rectangle([0, 0, W, 16], fill=_hex(accent))
    d.rectangle([0, H - 16, W, H], fill=_hex(accent))
    f_title = _font(96)
    f_body = _font(52, bold=False)
    # 标题（封面卡 idx==0 突出）
    if idx == 0:
        lines = _wrap(d, title, f_title, W - 160)
        y = H // 2 - (len(lines) * int(f_title.size * 1.3)) // 2 - 120
        for ln in lines[:4]:
            tw = d.textlength(ln, font=f_title)
            d.text(((W - tw) / 2, y), ln, font=f_title, fill=(255, 255, 255))
            y += int(f_title.size * 1.3)
        d.rounded_rectangle([W // 2 - 120, y + 30, W // 2 + 120, y + 42], radius=6,
                            fill=_hex(accent))
        cap = "合胜宏企服 · 企业服务"
        tw = d.textlength(cap, font=_font(40, bold=False))
        d.text(((W - tw) / 2, H - 200), cap, font=_font(40, bold=False),
               fill=(200, 206, 214))
    else:
        # 序号圆点
        d.ellipse([80, 150, 200, 270], fill=_hex(accent))
        dn = ImageDraw.Draw(img)
        nn = str(idx)
        nf = _font(70)
        tw = d.textlength(nn, font=nf)
        d.text(((140 - tw / 2), 165), nn, font=nf, fill=(255, 255, 255))
        # 正文
        body_font = _font(58, bold=False)
        lines = _wrap(d, text, body_font, W - 200)
        y = 360
        for ln in lines[:14]:
            d.text((100, y), ln, font=body_font, fill=(235, 240, 246))
            y += int(body_font.size * 1.32)
        comp = config.get_settings().get("company", {}).get("short", "合胜宏企服")
        tw = d.textlength(comp, font=_font(38, bold=False))
        d.text(((W - tw) / 2, H - 180), comp, font=_font(38, bold=False),
               fill=(150, 160, 175))
    img.save(path)
    return path


def render_tts_cards(job_id):
    """后台线程：无 .motion 包时的兜底出片。edge-tts 配音 + PIL 卡片 + ffmpeg。"""
    job = db.get_job(job_id)
    if not job:
        return
    tmp = os.path.join(config.ASSETS, "job_%d" % job_id, "tts")
    os.makedirs(tmp, exist_ok=True)
    try:
        db.update_job(job_id, status="running", progress=8)
        script = db.get_script(job.get("script_id")) or {}
        body = (script.get("body") or "").strip()
        title = (script.get("title") or "").strip() or "企业服务那些事"
        opts = (json.loads(job.get("motion_input") or "{}")
                if isinstance(job.get("motion_input"), str)
                else (job.get("motion_input") or {}))
        voice = opts.get("voice") or "xiaoxiao"
        accent = opts.get("accent") or "#ff6a3d"
        bg = opts.get("bg") or "#0e1a24"
        # 把正文切成若干卡片（优先用文案分段，否则按句）
        segs = []
        raw_segs = json.loads(script.get("segments") or "[]") if isinstance(script.get("segments"), str) else (script.get("segments") or [])
        if isinstance(raw_segs, list) and raw_segs:
            segs = [s.get("text", "") for s in raw_segs if (s.get("text") or "").strip()]
        if not segs:
            segs = _sentences(body, maxn=8) or [body]
        segs = [s.strip() for s in segs if s.strip()][:10]

        # 配音：整段正文（或逐卡拼接）
        audio = os.path.join(tmp, "voice.mp3")
        dur = 0.0
        try:
            _, info = _synthesize(body, opts, audio, lambda m: _log(job_id, m))
            dur = float(info.get("duration") or 0.0)
        except Exception as e:
            _log(job_id, "配音失败（无网络或未配置？改用静音轨）：%s" % e)
            audio = None
        db.update_job(job_id, progress=28)

        # 分配每张卡片时长
        total_chars = sum(len(s) for s in segs) or 1
        if dur > 0:
            durations = [max(2.5, dur * len(s) / total_chars) for s in segs]
        else:
            durations = [max(3.0, len(s) / 4.0) for s in segs]
        # 封面卡 1.5s
        cover_dur = 1.5
        all_dur = [cover_dur] + durations
        db.update_job(job_id, progress=40)

        # 生成卡片图
        cards = [os.path.join(tmp, "card_cover.png")]
        _card_image(cards[0], title, "", 0, len(segs) + 1, accent, bg)
        for i, s in enumerate(segs, 1):
            cp = os.path.join(tmp, "card_%02d.png" % i)
            _card_image(cp, title, s, i, len(segs) + 1, accent, bg)
            cards.append(cp)
        _log(job_id, "已生成 %d 张卡片" % len(cards))
        db.update_job(job_id, progress=55)

        # 合成静帧视频：concat 解复用器 + 每张卡时长（可靠、不会因无限流卡死）
        fps = 30
        silent = os.path.join(tmp, "silent.mp4")
        listf = os.path.join(tmp, "cards.txt")
        with open(listf, "w", encoding="utf-8") as fh:
            for c, dd in zip(cards, all_dur):
                fh.write("file '%s'\n" % os.path.abspath(c).replace("'", "'\\''"))
                fh.write("duration %.3f\n" % dd)
            # 末帧再写一次，保证最后一卡的 duration 生效
            fh.write("file '%s'\n" % os.path.abspath(cards[-1]).replace("'", "'\\''"))
        _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "concat",
              "-safe", "0", "-i", listf, "-vf",
              "scale=1080:1920:force_original_aspect_ratio=decrease,"
              "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,fps=30,format=yuv420p",
              "-c:v", "libopenh264", "-b:v", "5M", "-pix_fmt", "yuv420p",
              "-movflags", "+faststart", silent], tmp, lambda m: _log(job_id, m), timeout=600)
        db.update_job(job_id, progress=75)

        # 叠配音
        out_name = "clip_%d_tts.mp4" % job_id
        out_path = os.path.join(config.OUTPUTS, out_name)
        if audio and os.path.exists(audio):
            _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", silent,
                  "-i", audio, "-c:v", "copy", "-c:a", "aac", "-b:a", "128k",
                  "-ar", "44100", "-shortest", out_path], tmp,
                 lambda m: _log(job_id, m), timeout=300)
        else:
            _run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", silent,
                  "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "-c:v", "copy",
                  "-c:a", "aac", "-b:a", "128k", "-shortest", out_path], tmp,
                 lambda m: _log(job_id, m), timeout=300)
        if not os.path.exists(out_path) or os.path.getsize(out_path) < 10000:
            raise RuntimeError("未生成有效视频文件")
        odur, _, _ = (lambda: (0, 0, 0))()
        try:
            from . import ingest
            odur, _, _ = ingest.probe(out_path)
        except Exception:
            pass
        _log(job_id, "完成：%s（%ss，%d KB）" %
             (out_name, odur, os.path.getsize(out_path) // 1024))
        db.update_job(job_id, status="done", progress=100, output_path=out_path,
                      finished_at=db.now())
    except Exception as exc:
        _log(job_id, "失败：%s" % exc)
        db.update_job(job_id, status="failed", error=str(exc)[:1500],
                      finished_at=db.now())


def _log(jid, msg):
    try:
        db.update_job(jid, log=msg[-4000:])
    except Exception:
        pass
    print("[MOTION]", msg, flush=True)


def start_motion(job_id):
    """后台线程出片（MotionGit 包），立即返回，前端轮询进度。"""
    t = threading.Thread(target=render_motion, args=(job_id,), daemon=True)
    t.start()
    return t


def start_tts(job_id):
    """后台线程出片（无原片 TTS 卡片兜底）。"""
    t = threading.Thread(target=render_tts_cards, args=(job_id,), daemon=True)
    t.start()
    return t
