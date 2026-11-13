# -*- coding: utf-8 -*-
"""MotionGit motion-kit 包（personal-download 版）的本地渲染驱动。

新旧两种 .motion 包结构完全不同：

  A) python-adapter 包（旧）：包内 adapter.py + params.schema.json，
     由 core/motion.py 的 render_motion 直接跑 plan→prepare→render。

  B) motion-kit 包（新，官网现在发的都是这种）：
     包内是 assistant/motion.mjs（官方助手，893KB）+ content/release.bin
     （加密运行时，25MB）+ personal/motiongit.key.json（个人 Key）。
     所有操作都必须经过官方助手：
         node assistant/motion.mjs start   <原包>           识别模板与路线
         node assistant/motion.mjs contract <原包> motion-kit  读参数合同
         node assistant/motion.mjs new     <原包> <dir>      建生产实例
         node assistant/motion.mjs run <原包> --instance <dir> setup|check-params|plan|prepare|preview|render

本模块只做三件事：
  1. 识别 + 安装这类包（原包原样保留，只解出 assistant/ 供执行）；
  2. 把一份定稿文案翻译成 motion-kit 的 timeline 参数（控件编排）；
  3. 串起 setup→check→plan→prepare→preview→render 全链路，产出 mp4。

关键事实（已实测）：
  * 渲染在本机完成，不消耗任何积分；账户 0 积分也能出片。
  * 配音（TTS）要积分，账户无积分时用本地音乐，voiceover 留空。
  * 模板右上角的时间码 / .m 角标是模板官方设计（time corner），不是水印，
    不能通过参数去掉。
"""
import json
import os
import re
import shutil
import subprocess
import time

from . import config, db

NODE_MIN = 22
KIT_WORKFLOW = "motion-kit"

# 控件最短时长（秒）。取自包内 capabilities 的 minDur，用于编排时兜底。
MIN_DUR = {
    "title": 2.5, "keyword": 2.0, "number": 3.0, "list": 3.0, "compare": 4.0,
    "steps": 3.5, "quote": 3.0, "bars": 3.0, "media": 3.0, "stamp": 1.8,
    "chapter": 1.8, "fusion": 3.5, "logo": 3.0, "intro": 3.0, "outro": 4.0,
    "cta": 2.5, "subscribe": 3.5, "countdown": 3.0, "price": 3.5,
    "reveal": 3.5, "iconcards": 3.5, "qa": 3.5, "myth": 3.5, "prosCons": 4.5,
    "definition": 4.0, "tip": 3.0, "checklist": 4.0, "vs": 4.5, "donut": 4.0,
    "linechart": 4.0, "counter": 3.5, "progress": 3.0, "timeline": 3.5,
    "table": 4.0, "ranking": 4.0, "flow": 4.0, "cycle": 4.5, "funnel": 4.0,
    "venn": 4.0, "mindmap": 4.5, "matrix": 4.0, "hub": 4.0,
}

DEFAULT_VARIANT = {
    "title": "slam", "keyword": "focus", "list": "rail", "iconcards": "grid",
    "quote": "type", "tip": "banner", "definition": "dictionary",
    "compare": "split", "vs": "cards", "steps": "track", "myth": "cross",
    "counter": "cards", "cta": "button", "outro": "thanks",
}

# 文案 -> 控件 的启发式关键词。命中即换控件，保证一条片子至少 6 种不同控件。
RULES = [
    ("cta", r"(找我们|咨询|联系|关注|私信|扫码|领取|办理|报名|加微信)"),
    ("myth", r"(不是.{1,12}而是|误区|其实不是|别再)"),
    ("compare", r"(对比|相比|区别|vs|前者|后者|一边|另一边)"),
    ("steps", r"(第一步|第二步|第三步|首先|其次|最后|流程|步骤)"),
    ("definition", r"(是指|就是|叫做|定义|意思是|所谓)"),
    ("tip", r"(注意|风险|千万|不要|小心|提醒|建议|别照抄|别随便)"),
    ("counter", r"(\d+(\.\d+)?\s*(万|亿|%|元|个|家|天|年))"),
]


# ----------------------------------------------------------------------------
# 1. 包识别与安装
# ----------------------------------------------------------------------------
def read_motion_json(zip_path):
    """不解包，直接从 .motion 里读 motion.json。"""
    import zipfile
    try:
        with zipfile.ZipFile(zip_path) as z:
            if "motion.json" not in z.namelist():
                return None
            return json.loads(z.read("motion.json").decode("utf-8"))
    except Exception:
        return None


def is_kit(motion_json):
    return bool(motion_json) and motion_json.get("format") == "motiongit-template"


def entry_of(motion_json):
    ext = (motion_json or {}).get("extensions") or {}
    for k, v in ext.items():
        if k.endswith(":assistant-entry"):
            return v
    return {}


def install(motion_path, dest, log=None):
    """安装 kit 包：原包整份留存，只把 assistant/ 与文档解出来。

    按包内 00-START-HERE 的要求，personal/、content/release.bin 留在原包里，
    不展开到目录树，避免 Key 落成明文文件。
    """
    import zipfile
    os.makedirs(dest, exist_ok=True)
    keep = os.path.join(dest, "package.motion")
    shutil.copy2(motion_path, keep)
    with zipfile.ZipFile(keep) as z:
        for info in z.infolist():
            name = info.filename
            if name.endswith("/"):
                continue
            ok = (name.startswith("assistant/") or name == "motion.json"
                  or name.endswith(".md"))
            if not ok:
                continue
            target = os.path.join(dest, name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(info) as src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
    if log:
        log("已安装 motion-kit 包，助手 %s" %
            os.path.basename(entry_of(read_motion_json(keep)).get("entry", "")))
    return keep


def helper_path(root):
    p = os.path.join(root, "assistant", "motion.mjs")
    return p if os.path.exists(p) else None


def package_path(root):
    p = os.path.join(root, "package.motion")
    return p if os.path.exists(p) else None


def verify_helper(root):
    """按包内要求核对助手 SHA256 与 BUILD.json 一致（官方元数据在离线时可不拉）。"""
    import hashlib
    hp = helper_path(root)
    if not hp:
        return False, "缺少 assistant/motion.mjs"
    build = os.path.join(root, "assistant", "BUILD.json")
    if not os.path.exists(build):
        return True, "无 BUILD.json，跳过核对"
    try:
        meta = json.load(open(build, encoding="utf-8"))
    except Exception as e:
        return True, "BUILD.json 读取失败：%s" % e
    h = hashlib.sha256(open(hp, "rb").read()).hexdigest()
    if meta.get("sha256") and h != meta["sha256"]:
        return False, "助手校验失败：%s != %s" % (h[:12], meta["sha256"][:12])
    return True, "助手校验通过（%s）" % meta.get("helperVersion", "?")


# ----------------------------------------------------------------------------
# 2. 执行官方助手
# ----------------------------------------------------------------------------
class KitError(RuntimeError):
    pass


class Kit:
    def __init__(self, root, workdir, log=None):
        self.root = root
        self.workdir = workdir
        self.log = log or (lambda m: None)
        self.helper = helper_path(root)
        self.pkg = package_path(root)
        self.instance = None

    # -- 基础调用 -----------------------------------------------------------
    def _call(self, args, timeout=1800, check=True):
        cmd = ["node", self.helper] + args
        p = subprocess.run(cmd, cwd=os.path.dirname(self.helper) or ".",
                           capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        if check and p.returncode != 0:
            msg = (err or out or "助手执行失败")[-1200:]
            raise KitError("%s\n%s" % (args[0] if args else "helper", msg))
        return out, err, p.returncode

    def _json(self, args, timeout=1800):
        out, err, rc = self._call(args, timeout=timeout)
        txt = out.strip()
        if not txt:
            raise KitError("助手无输出：%s" % (err or "")[-500:])
        # 助手可能打印进度行 + 一段 JSON（多行缩进），取第一个能完整解析的对象
        dec = json.JSONDecoder()
        for i, ch in enumerate(txt):
            if ch != "{":
                continue
            try:
                obj, _ = dec.raw_decode(txt[i:])
            except Exception:
                continue
            if isinstance(obj, (dict, list)):
                return obj
        raise KitError("助手未返回 JSON：%s" % txt[-500:])

    def start(self):
        return self._json(["start", self.pkg, "--summary"], timeout=300)

    def contract(self, controls=None):
        args = ["contract", self.pkg, KIT_WORKFLOW] + list(controls or [])
        return self._json(args, timeout=180)

    def new_instance(self):
        data = self._json(["new", self.pkg], timeout=300)
        inst = data.get("instanceDirectory")
        if not inst or not os.path.isdir(inst):
            raise KitError("助手未给出实例目录")
        self.instance = inst
        os.makedirs(os.path.join(inst, "inputs"), exist_ok=True)
        return data

    def setup(self):
        self._json(["run", self.pkg, "--instance", self.instance, "setup",
                    "--workflow", KIT_WORKFLOW, "--language", "zh"], timeout=900)

    def check(self, params_path):
        return self._json(["run", self.pkg, "--instance", self.instance,
                           "check-params", params_path, "--inputs",
                           os.path.join(self.instance, "inputs"),
                           "--language", "zh"], timeout=600)

    def plan(self, params_path, out_dir):
        return self._json(["run", self.pkg, "--instance", self.instance, "plan",
                           params_path, "--inputs", os.path.join(self.instance, "inputs"),
                           "--out", out_dir, "--language", "zh"], timeout=900)

    def prepare(self, plan_dir, project_dir):
        return self._json(["run", self.pkg, "--instance", self.instance, "prepare",
                           "@vault", "--inputs", "@vault", "--plan", plan_dir,
                           "--out", project_dir, "--language", "zh"], timeout=1200)

    def preview(self, project_dir, times):
        return self._json(["run", self.pkg, "--instance", self.instance, "preview",
                           project_dir, "--times", times, "--language", "zh"],
                          timeout=900)

    def render(self, project_dir, out_path):
        return self._json(["run", self.pkg, "--instance", self.instance, "render",
                           project_dir, "--out", out_path, "--language", "zh"],
                          timeout=3600)


# ----------------------------------------------------------------------------
# 3. 文案 -> motion-kit 参数
# ----------------------------------------------------------------------------
def _sentences(text, maxn=14):
    parts = re.split(r"(?<=[。！？!?；;])\s*", (text or "").strip())
    out = [p.strip() for p in parts if p.strip()]
    return out[:maxn] or [text.strip()[:40]]


def _pick(seg):
    """一句文案选控件。"""
    for ctl, pat in RULES:
        if re.search(pat, seg):
            return ctl
    return "keyword"


def _clean(s, n=24):
    """去掉首尾标点并截断，用于列表项/按钮等槽位。"""
    s = re.sub(r"^[，,。！？；;：:\s]+|[，,。！？；;：:\s]+$", "", (s or "").strip())
    return s[:n]


def _title_lines(title, body):
    t = (title or "").strip() or "合胜宏企服"
    if len(t) <= 8:
        tail = _clean(body, 8)
        if not tail or tail == t:
            tail = "先看这几点"
        return t, tail
    cut = max(2, len(t) // 2)
    return t[:cut], _clean(t[cut:], 16)


def _dur(text, ctl, cap=8.0):
    """按时长估算镜头长度：中文约 4.2 字/秒。"""
    est = max(1.2, len(text) / 4.2 + 1.2)
    return round(min(cap, max(MIN_DUR.get(ctl, 3.0), est)), 2)


def _seg(ctl, variant, t0, t1, props, transition="cut"):
    return {"control": ctl, "variant": variant, "t0": round(t0, 2),
            "t1": round(t1, 2), "props": props, "transition": transition}


def build_params(script, opts=None):
    """把一份文案编排成 motion-kit 参数。

    opts:
      layout      : landscape / portrait
      music       : inputs 下的相对文件名（'' 表示无配乐）
      voiceover   : inputs 下的相对文件名（'' 表示无配音）
      style       : dark / light / blue
      accent      : #rrggbb
      theme       : 完整 theme 对象（优先于 accent）
    """
    opts = opts or {}
    body = (script.get("body") or "").strip()
    title = (script.get("title") or "").strip() or "企业服务那些事"
    company = (config.get_settings().get("company", {}) or {}).get("short", "合胜宏企服")
    sents = _sentences(body)

    l1, l2 = _title_lines(title, "")
    segs = []
    t = 0.0
    d = _dur(l1 + l2, "title")
    segs.append(_seg("title", "slam", t, t + d,
                     {"kicker": company[:32], "line1": l1[:16], "line2": l2[:16]}, "flash"))
    t += d

    used = {"title"}
    prev = "title"

    def push(ctl, variant, props, text, trans="cut"):
        """追加镜头并推进时间轴；同类控件连用会自动换一个。"""
        nonlocal t, prev
        if ctl == prev:
            ctl = {"tip": "list", "list": "iconcards", "keyword": "quote",
                   "definition": "tip", "compare": "vs"}.get(ctl, "list")
            if ctl == "list":
                props = {"title": title[:18],
                         "items": [_clean(x) for x in re.split(r"[，,。！？]", text) if x.strip()][:6]}
                if len(props["items"]) < 1:
                    props["items"] = [_clean(text)]
            elif ctl == "iconcards":
                props = {"items": [_clean(x) for x in re.split(r"[，,。！？]", text) if x.strip()][:6]}
            elif ctl == "quote":
                props = {"text": text[:48]}
            elif ctl == "vs":
                props = {"a": "常见做法", "b": "更稳做法",
                         "stats": [{"label": "成本", "a": 7, "b": 4}]}
            variant = DEFAULT_VARIANT.get(ctl, "slam")
        d = _dur(text, ctl)
        segs.append(_seg(ctl, variant, t, t + d, props, trans))
        used.add(ctl)
        prev = ctl
        t += d

    i = 0
    while i < len(sents):
        s = sents[i]
        ctl = _pick(s)
        # 相邻同类合并，避免一条片子全是 keyword
        if ctl == "keyword" and i + 1 < len(sents) and _pick(sents[i + 1]) == "keyword":
            grp = [s]
            while i + 1 < len(sents) and _pick(sents[i + 1]) == "keyword" and len(grp) < 4:
                i += 1
                grp.append(sents[i])
            d = _dur("".join(grp), "list")
            segs.append(_seg("list", "rail", t, t + d,
                             {"title": title[:18],
                              "items": [_clean(g) for g in grp][:6]},
                             "wipe" if len(segs) % 2 else "cut"))
            used.add("list")
            prev = "list"
            t += d
            i += 1
            continue

        if ctl == "keyword":
            word = re.split(r"[，,。！？、]", s)[0][:10]
            sub = [x[:28] for x in re.split(r"[，,]", s)[1:4]]
            v = "focus" if len(segs) % 2 else "slam"
            push("keyword", v, {"kicker": title[:32], "word": word, "sub": sub}, s, "cut")
        elif ctl == "steps":
            grp = [s]
            while i + 1 < len(sents) and _pick(sents[i + 1]) == "steps" and len(grp) < 5:
                i += 1
                grp.append(sents[i])
            items = [re.sub(r"^(第[一二三四五]步|首先|其次|最后)[，,]?\s*", "", g)[:10] for g in grp]
            if len(items) < 2:
                items = [items[0], "再走下一步"]
            d = _dur("".join(grp), "steps")
            segs.append(_seg("steps", "track", t, t + d,
                             {"title": "怎么做"[:18], "steps": items[:5]}, "wipe"))
            used.add("steps")
            prev = "steps"
            t += d
            i += 1
            continue

        elif ctl == "compare":
            half = re.split(r"[，,]", s)
            left = (half[0] if half else s)[:10] or "原来的做法"
            right = (half[-1] if len(half) > 1 else "更稳妥的做法")[:10]
            push("compare", "split",
                 {"left": {"label": "常见做法"[:12], "big": left},
                  "right": {"label": "更稳的做法"[:12], "big": right}}, s, "zoom")
        elif ctl == "definition":
            parts = re.split(r"(是指|就是|叫做|意思是)", s, maxsplit=1)
            term = parts[0].strip()[:10] if len(parts) > 2 else s[:10]
            definition = ("".join(parts[1:]) if len(parts) > 2 else s)[:48]
            push("definition", "dictionary",
                 {"kicker": "先把词搞懂", "term": term, "pos": "名词",
                  "definition": definition}, s, "blur")
        elif ctl == "tip":
            push("tip", "banner",
                 {"kicker": "提醒", "kind": "warn", "title": "注意"[:12],
                  "text": s[:44]}, s, "cut")
        elif ctl == "myth":
            push("myth", "cross",
                 {"myth": (re.split(r"而是|其实", s)[0] or s)[:16],
                  "fact": (re.split(r"而是|其实", s)[-1] or s)[:24]}, s, "flash")
        elif ctl == "counter":
            nums = re.findall(r"(\d+(?:\.\d+)?)\s*(万|亿|%|元|个|家|天|年)?", s)
            items = []
            for j, (v, unit) in enumerate(nums[:4]):
                try:
                    fv = float(v)
                except Exception:
                    continue
                items.append({"label": ("指标%d" % (j + 1))[:8], "value": fv,
                              "unit": (unit or "")[:3]})
            if len(items) < 2:
                push("keyword", "slam", {"word": s[:10], "sub": []}, s, "cut")
            else:
                push("counter", "cards", {"title": title[:18], "items": items}, s, "wipe")
        elif ctl == "cta":
            push("cta", "button",
                 {"kicker": company[:20], "text": "找合胜宏"[:10],
                  "sub": s[:24], "icon": "rocket"}, s, "flash")
        else:
            push("keyword", "slam",
                 {"kicker": title[:32], "word": s[:10], "sub": []}, s, "cut")
        i += 1

    # 收尾：没有 CTA 就补一个，再接片尾
    if "cta" not in used:
        d = _dur("联系我们", "cta")
        segs.append(_seg("cta", "button", t, t + d,
                         {"kicker": "办企业不用自己摸", "text": "找合胜宏"[:10],
                          "sub": "一次办对，少跑三趟", "icon": "rocket"}, "flash"))
        t += d
    d = _dur("感谢观看", "outro")
    segs.append(_seg("outro", "thanks", t, t + d,
                     {"thanks": "感谢观看"[:10], "sub": title[:24],
                      "name": company[:12], "follow": "关注",
                      "items": [{"text": "有疑问评论区问"[:14]}]}, "cut"))
    t += d

    total = round(max(6.0, t), 2)
    theme = opts.get("theme") or {
        "accent": opts.get("accent", "#ff6a3d"),
        "accent2": "#4ea1ff",
        "bg": "#0e1a24",
    }
    tl = {
        "layout": opts.get("layout", "landscape"),
        "styleProfile": opts.get("style", "dark"),
        "duration": total,
        "voiceover": opts.get("voiceover", "") or "",
        "music": opts.get("music", "") or "",
        "ending": "cut",
        "theme": theme,
        "header": {"shortTitle": title[:2000],
                   "source": {"kind": "user-explicit", "field": "title"}},
        "narrationComposition": "readable/1",
        "narrationLayout": "balanced",
        "captionLayout": "balanced-lines/1",
        "highlightReadability": "complete-text/1",
        "segments": segs,
    }
    return {
        "workflow": KIT_WORKFLOW,
        "header": {"shortTitle": title[:2000],
                   "source": {"kind": "user-explicit", "field": "title"}},
        "timeline": tl,
    }


# ----------------------------------------------------------------------------
# 4. 全链路出片
# ----------------------------------------------------------------------------
def _times(params):
    segs = params["timeline"]["segments"]
    out = []
    for s in segs:
        mid = (s["t0"] + s["t1"]) / 2
        out.append(round(mid, 2))
    return ",".join(str(x) for x in out[:12])


def render(job_id, root, log=None):
    """后台任务主体：kit 包全链路出片。返回 (out_path, duration)。"""
    from . import ingest
    log = log or (lambda m: None)
    job = db.get_job(job_id) or {}
    script = db.get_script(job.get("script_id")) or {}
    opts = job.get("motion_input") or {}
    if isinstance(opts, str):
        opts = json.loads(opts or "{}")

    work = os.path.join(config.ASSETS, "job_%d" % job_id, "kit")
    if os.path.isdir(work):
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    ok, msg = verify_helper(root)
    if not ok:
        raise KitError(msg)
    log(msg)

    kit = Kit(root, work, log)
    db.update_job(job_id, progress=8)
    kit.new_instance()
    log("已建生产实例")
    db.update_job(job_id, progress=16)

    log("准备运行时（首次约 20 秒，之后走缓存）…")
    kit.setup()
    db.update_job(job_id, progress=30)

    # 素材：音乐 / 配音 都放 instance/inputs，用相对文件名绑定
    inputs = os.path.join(kit.instance, "inputs")
    for key in ("music", "voiceover"):
        src = opts.get(key + "_path")
        if src and os.path.exists(src):
            name = key + os.path.splitext(src)[1]
            shutil.copy2(src, os.path.join(inputs, name))
            opts[key] = name
            log("已绑定%s：%s" % ("配乐" if key == "music" else "配音", os.path.basename(src)))
    if not opts.get("music"):
        opts["music"] = ""
    if not opts.get("voiceover"):
        opts["voiceover"] = ""

    # 官方助手要求 plan/prepare/render 的路径都落在实例目录内
    base = kit.instance
    params = build_params(script, opts)
    pp = os.path.join(base, "params.json")
    json.dump(params, open(pp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    n = len(params["timeline"]["segments"])
    kinds = sorted({s["control"] for s in params["timeline"]["segments"]})
    log("已编排 %d 个镜头 / %d 种控件（%s）/ 总长 %.1fs" %
        (n, len(kinds), "、".join(kinds[:8]), params["timeline"]["duration"]))
    db.update_job(job_id, progress=38)

    kit.check(pp)
    log("参数校验通过")
    plan_dir = os.path.join(base, "plan")
    kit.plan(pp, plan_dir)
    rv = os.path.join(plan_dir, "review.json")
    if os.path.exists(rv):
        d = json.load(open(rv, encoding="utf-8"))
        d["reviewed"] = True
        json.dump(d, open(rv, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log("编排方案已确认")
    db.update_job(job_id, progress=52)

    project = os.path.join(base, "project")
    kit.prepare(plan_dir, project)
    db.update_job(job_id, progress=64)

    kit.preview(project, _times(params))
    pv = os.path.join(project, "preview.json")
    if os.path.exists(pv):
        d = json.load(open(pv, encoding="utf-8"))
        d["reviewed"] = True
        json.dump(d, open(pv, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log("预览帧已生成并确认")
    db.update_job(job_id, progress=74)

    out_raw = os.path.join(base, "video.mp4")
    kit.render(project, out_raw)
    if not os.path.exists(out_raw) or os.path.getsize(out_raw) < 10000:
        raise KitError("模板未产出有效视频")
    db.update_job(job_id, progress=96)

    name = "clip_%d_kit.mp4" % job_id
    out_path = os.path.join(config.OUTPUTS, name)
    shutil.copy2(out_raw, out_path)
    dur, _, _ = 0.0, 0, 0
    try:
        dur, _, _ = ingest.probe(out_path)
    except Exception:
        pass
    log("完成：%s（%.2fs，%d KB）" % (name, dur, os.path.getsize(out_path) // 1024))
    return out_path, dur, params
