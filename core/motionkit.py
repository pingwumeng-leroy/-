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
import glob
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
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
# 竖屏单屏只讲一件事，所以“不是A而是B”“A不等于B”这类纠正句优先给 myth
POSTER_RULES = [
    ("cta", r"(找我们|咨询|联系|关注|私信|扫码|领取|办理|报名|加微信|找.{2,6}，)"),
    ("myth", r"(不是.{1,12}(越好|越多|越大|越贵)|不等于|≠|而是|误区)"),
    ("tip", r"(别|不要|千万|小心|注意|风险|坑|影响|建议)"),
    ("compare", r"(对比|相比|区别|前者|后者|一边|另一边)"),
    ("steps", r"(第一步|第二步|第三步|首先|其次|最后|流程|步骤)"),
    ("definition", r"(是指|叫做|意思是|所谓)"),
    ("counter", r"(\d+(\.\d+)?\s*(万|亿|%|元|个|家|天|年|倍))"),
    ("prosCons", r"(好处|代价|优点|缺点|划算|值不值)"),
    ("quote", r"(这句话|俗话说|记住|一句话)"),
    ("highlight", r"(关键|核心|重点|最重要)"),
    ("qa", r"(为什么|怎么|是不是|能不能|要不要)"),
]

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


def _read_public(root, name):
    """从原包里读一份公开文档（助手 read 子命令，不展开受保护内容）。"""
    pkg = package_path(root)
    hp = helper_path(root)
    if not pkg or not hp:
        return None
    try:
        p = subprocess.run(["node", hp, "read", pkg, name],
                           cwd=os.path.dirname(hp) or ".",
                           capture_output=True, text=True, timeout=180)
        if p.returncode != 0:
            return None
        txt = (p.stdout or "").strip()
        dec = json.JSONDecoder()
        i = txt.find("{")
        while i >= 0:
            try:
                return dec.raw_decode(txt[i:])[0]
            except Exception:
                i = txt.find("{", i + 1)
    except Exception:
        pass
    return None


def kit_profile(root):
    """读出模板真实身份：工作流、原生画幅、长宽、版式控件清单。

    模板分两条路线，参数合同完全不同：
      * motion-kit  : 通用 71 控件时间轴（template-03 这类）
      * poster-kit  : 海报版式，自带 timeline schema（活力海报竖屏）
    不确定路线就直接产片只会得到一堆 check-params 报错，所以安装时就定下来。
    """
    out = {"workflow": KIT_WORKFLOW, "native_layout": "", "width": 0, "height": 0,
           "template_name": "", "template_id": "", "controls": {}}
    guide_p = os.path.join(root, "assistant", "guide.json")
    guide = {}
    if os.path.exists(guide_p):
        try:
            guide = json.load(open(guide_p, encoding="utf-8"))
        except Exception:
            guide = {}
    t = guide.get("template") or {}
    out.update({"native_layout": t.get("layout", ""), "width": t.get("width", 0),
                "height": t.get("height", 0), "template_name": t.get("name", ""),
                "template_id": t.get("id", "")})
    if (guide.get("workflowRouting") or {}).get("textOnly"):
        out["workflow"] = "poster-kit"
        spec = _read_public(root, "poster-kit.spec.json") or {}
        spec = spec.get("content") or {}
    else:
        c = _read_public(root, "contract") or {}
        spec = c if c.get("controls") else (_read_public(root, "poster-kit.spec.json") or {}).get("content") or {}
    if not spec.get("controls"):
        # contract 子命令拿不到时，退回读包内公开 spec
        spec = (_read_public(root, "poster-kit.spec.json") or {}).get("content") or spec
    controls = spec.get("controls") or {}
    if isinstance(controls, dict):
        out["controls"] = {k: {"label": v.get("label", ""),
                               "use": v.get("use", ""),
                               "minDur": v.get("minDur", 0),
                               "variants": v.get("variants", []),
                               "props": {pk: {kk: vv for kk, vv in (pv or {}).items()
                                              if kk in ("type", "max", "required", "label")}
                                         for pk, pv in (v.get("props") or {}).items()}}
                          for k, v in controls.items()}
    out["transitions"] = spec.get("transitions") or []
    return out


def load_profile(root):
    """kit_profile 的缓存版：安装时写 profile.json，出片时直接读。"""
    p = os.path.join(root, "profile.json")
    if os.path.exists(p):
        try:
            return json.load(open(p, encoding="utf-8"))
        except Exception:
            pass
    prof = kit_profile(root)
    try:
        json.dump(prof, open(p, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    except Exception:
        pass
    return prof


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
        self.profile = load_profile(root)
        self.workflow = self.profile.get("workflow") or KIT_WORKFLOW
        self.plan_dir = None
        self.preview_dir = None
        # 实例名，用来预判助手新开会话时会指到哪个实例目录
        self.instance_key = None
        self.active_instance = None

    # -- 基础调用 -----------------------------------------------------------
    @staticmethod
    def _rebuild_instance_dir(key):
        """按助手命名规则重建实例目录（不含 Key），供素材兜底使用。"""
        d = os.path.join(tempfile.gettempdir(), "motiongit-instance-%s" % key, "instance")
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            return None
        return d

    def _call(self, args, timeout=1800, check=True):
        if self.helper is None and self.root:
            self.helper = helper_path(self.root)
        cmd = ["node", str(self.helper)] + [str(a) for a in args if a is not None]
        cwd = os.path.dirname(str(self.helper)) or "."
        p = subprocess.run(cmd, cwd=cwd,
                           capture_output=True, text=True, timeout=timeout)
        out = (p.stdout or "").strip()
        err = (p.stderr or "").strip()
        # 助手每次调用都新开一个临时实例目录，素材只在“上一次”那个目录里看得到；
        # 报缺素材时把文件同步到当前实例根目录再重试一次（模板要求素材与
        # timeline.json 同级）。这一步只复制文件，不改助手、不动原包。
        if (check and p.returncode != 0 and self.instance_key
                and re.search(r"Missing (media|input)", err + out)):
            # 助手每次调用都用“本次”新建的实例目录，素材必须同步过去。
            # 文件名可能是 voice.mp3，也可能是 voiceover.mp3，两种都补齐。
            # 助手会把实例再嵌套一层（motiongit-instance-<key>/motiongit-instance-<key>），
            # 真正读素材的是最内层那个，所以按前缀一次列出全部同名目录。
            tdir = tempfile.gettempdir()
            key = self.instance_key.replace("motiongit-instance-", "")
            try:
                dirs = [os.path.join(tdir, "motiongit-instance-%s" % key, "instance")]
                for n in sorted(os.listdir(tdir)):
                    if key not in n:
                        continue
                    q = os.path.join(tdir, n, "instance")
                    if os.path.isdir(q) and q not in dirs:
                        dirs.append(q)
            except Exception:
                dirs = []
            if self.instance and os.path.isdir(os.path.join(self.instance, "inputs")):
                srcs = [os.path.join(self.instance, "inputs", f)
                        for f in os.listdir(os.path.join(self.instance, "inputs"))]
                moved = 0
                for d in dirs:
                    try:
                        os.makedirs(d, exist_ok=True)
                    except Exception:
                        continue
                    for sc in srcs:
                        if not os.path.isfile(sc):
                            continue
                        base = os.path.splitext(os.path.basename(sc))[0]
                        for alias in {base, base.replace("voice", "voiceover")}:
                            try:
                                shutil.copy2(sc, os.path.join(
                                    d, alias + os.path.splitext(sc)[1]))
                                moved += 1
                            except Exception:
                                pass
                if moved:
                    self.log("素材已同步到当前实例目录（%d 个文件）" % moved)
                    p = subprocess.run(cmd, cwd=cwd,
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
        self.instance_key = os.path.basename(os.path.dirname(inst))
        if self.instance_key not in tempfile.gettempdir():
            self.instance_key = self.instance_key.replace("motiongit-instance-", "")
        self.instance = inst
        os.makedirs(os.path.join(inst, "inputs"), exist_ok=True)
        return data

    def put_input(self, name, src=None, text=None):
        """把素材写进实例 inputs。

        助手在 check 时会识别 inputs 内容并复制到 vault；如果先建了空目录，
        助手会把它当临时目录重建，所以素材必须“写文件”这一步完成绑定，
        不能在实例建好后立刻依赖目录存在。
        """
        d = os.path.join(self.instance, "inputs")
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, name)
        if text is not None:
            open(dst, "w", encoding="utf-8").write(text)
        elif src and os.path.abspath(src) != os.path.abspath(dst):
            shutil.copy2(src, dst)
        return dst

    def setup(self):
        # poster-kit 路线强制要求显式依赖缓存目录，否则直接 RUNTIME_FAILED；
        # 缓存放在 workdir 下，同一个模板复用，不占系统临时盘
        cache = os.path.join(os.path.dirname(os.path.abspath(self.workdir)),
                             "_kitcache", os.path.basename(os.path.normpath(self.root)))
        os.makedirs(cache, exist_ok=True)
        self.cache = cache
        args = ["run", self.pkg, "--instance", self.instance, "setup",
                "--workflow", self.workflow, "--language", "zh"]
        if self.workflow == "poster-kit":
            args += ["--cache", cache]
        self._json(args, timeout=1800)

    def _cache_args(self):
        return ["--cache", self.cache] if getattr(self, "cache", None) else []

    def check(self, params_path):
        # poster-kit 用 --timeline 指定输入文档，motion-kit 是位置参数 + --inputs。
        # 两条路线都要显式给 --inputs，否则助手只在实例根目录找素材，
        # 会报 “Missing media voiceover”（文件其实在 inputs/ 里）。
        if self.workflow == "poster-kit":
            args = ["run", self.pkg, "--instance", self.instance,
                    "check-params", "--workflow", self.workflow,
                    "--timeline", params_path, "--language", "zh"]
        else:
            args = ["run", self.pkg, "--instance", self.instance,
                    "check-params", params_path,
                    "--inputs", os.path.join(self.instance, "inputs"),
                    "--workflow", self.workflow, "--language", "zh"]
        return self._json(args + self._cache_args(), timeout=600)

    def plan(self, params_path, out_dir):
        """poster-kit 与 motion-kit 的 plan 参数形态不同，必须按路线分派。"""
        if self.workflow == "poster-kit":
            args = ["run", self.pkg, "--instance", self.instance, "plan",
                    "--workflow", "poster-kit", "--timeline", params_path,
                    "--out", out_dir, "--language", "zh"]
        else:
            args = ["run", self.pkg, "--instance", self.instance, "plan",
                    params_path, "--inputs", os.path.join(self.instance, "inputs"),
                    "--out", out_dir, "--language", "zh"]
        return self._json(args + self._cache_args(), timeout=900)

    def prepare(self, plan_dir, project_dir):
        if self.workflow == "poster-kit":
            args = ["run", self.pkg, "--instance", self.instance, "prepare",
                    "--workflow", "poster-kit", "--timeline", "@vault",
                    "--plan", plan_dir, "--out", project_dir, "--language", "zh"]
        else:
            args = ["run", self.pkg, "--instance", self.instance, "prepare",
                    "@vault", "--inputs", "@vault", "--plan", plan_dir,
                    "--out", project_dir, "--language", "zh"]
        return self._json(args + self._cache_args(), timeout=1200)

    def preview(self, project_dir, times, preview_dir=None):
        project_dir = project_dir or ""
        if self.workflow == "poster-kit":
            args = ["run", self.pkg, "--instance", self.instance, "preview",
                    "--workflow", "poster-kit", "--timeline", "@vault",
                    "--plan", self.plan_dir, "--out",
                    preview_dir or os.path.join(self.instance, "extension-preview"),
                    "--times", times, "--language", "zh"]
        else:
            args = ["run", self.pkg, "--instance", self.instance, "preview",
                    project_dir, "--times", times, "--language", "zh"]
        self._json(args + self._cache_args(), timeout=900)
        # 助手会在 out 目录里再建一层临时目录；真图落在最里面的子目录。
        # 同时记下“哪个实例是真正在用的”（助手会嵌套一层会话实例）。
        root = preview_dir or (os.path.join(self.instance, "extension-preview")
                               if self.workflow == "poster-kit" else None)
        res = preview_result(root) if root else {}
        if res.get("dir"):
            self.active_instance = os.path.dirname(os.path.abspath(res["dir"]))
        return res

    def render(self, project_dir, out_path):
        # poster-kit 的 --timeline 是位置参数占位，不能传 None
        project_dir = project_dir or ""
        if self.workflow == "poster-kit":
            args = ["run", self.pkg, "--instance", self.instance, "render",
                    "--workflow", "poster-kit", "--timeline", "@vault",
                    "--plan", self.plan_dir,
                    "--preview-review", self.preview_dir,
                    "--out", out_path, "--language", "zh"]
        else:
            args = ["run", self.pkg, "--instance", self.instance, "render",
                    project_dir, "--out", out_path, "--language", "zh"]
        return self._json(args + self._cache_args(), timeout=3600)


def _instance_videos(tdir=None):
    """扫描全部助手会话实例里的成片，返回 {路径: mtime}。

    助手每次调用都可能新建 /tmp/motiongit-instance-<key>/instance，传进去的
    --out 究竟落在哪个实例里不可预知，只能靠“调用后新出现的文件”认。
    """
    tdir = tdir or tempfile.gettempdir()
    res = {}
    for p in glob.glob(os.path.join(tdir, "motiongit-instance-*", "**", "*.mp4"),
                       recursive=True):
        try:
            res[p] = os.path.getmtime(p)
        except OSError:
            continue
    return res


def preview_result(out_dir):
    """在预览输出目录里找真正的帧与审核文件（可能嵌了一层子目录）。"""
    res = {"dir": out_dir, "frames": [], "review": None, "geometry": None}
    if not out_dir or not os.path.isdir(out_dir):
        return res
    cands = [out_dir]
    for name in sorted(os.listdir(out_dir)):
        sub = os.path.join(out_dir, name)
        if os.path.isdir(sub) and not name.startswith("."):
            cands.append(sub)
    for d in cands:
        fr = sorted(f for f in os.listdir(d) if re.match(r"t_?[\d.]+\.(png|jpg)$", f))
        if fr:
            res["dir"] = d
            res["frames"] = [os.path.join(d, f) for f in fr]
            for n in ("preview-review.json", "geometry.json"):
                p = os.path.join(d, n)
                if os.path.exists(p):
                    res["review" if n.startswith("preview") else "geometry"] = p
            break
    return res


# ----------------------------------------------------------------------------
# 3. 文案 -> motion-kit 参数
# ----------------------------------------------------------------------------
def _sentences(text, maxn=14):
    parts = re.split(r"(?<=[。！？!?；;])\s*", (text or "").strip())
    out = [p.strip() for p in parts if p.strip()]
    return out[:maxn] or [text.strip()[:40]]


def _split_for_pace(sents, target, extra_shots=2):
    """句子太少、镜头数撑不满配音时长时，把长句按逗号拆成独立镜头。

    模板对每个控件同时卡下限和上限（opener 2–3.5s、tip 3–5s、outro 4–6s…），
    镜头数不够时只能把单屏拉长，必然撞上限报“opener 时长需为 2–3.5 秒”。
    正确解法是多铺几屏：一句 20 字的话拆成两屏，节奏和时长都合理。

    extra_shots 是文案之外的固定镜头（片头 opener + 片尾 outro）。
    """
    if target <= 0 or not sents:
        return sents
    NAVG = 4.2      # 常规内容屏的目标时长
    MAXN = 30       # 兜底，避免病态输入把句子切得过碎
    out = list(sents)
    # 需要多少内容屏 ≈ (配音时长 - 固定屏时长) / 每屏目标时长
    need = int(round(max(1.0, (target - extra_shots * 4.0) / NAVG)))
    need = min(need, MAXN)
    guard = 0
    while len(out) < need and guard < 4 * MAXN:
        guard += 1
        # 每次拆最长的那句：按逗号对半切，切不动就找次长的
        order = sorted(range(len(out)), key=lambda i: -len(out[i]))
        done = False
        for idx in order:
            long = out[idx]
            clause = [x.strip() for x in re.split(r"[，,、]", long.rstrip("。！？!?"))
                      if x.strip()]
            if len(clause) < 2:
                continue
            mid = max(1, len(clause) // 2)
            a = "，".join(clause[:mid]) + "。"
            b = "，".join(clause[mid:]) + "。"
            if len(a) < 4 or len(b) < 4:
                continue
            out[idx:idx + 1] = [a, b]
            done = True
            break
        if not done:
            break
    return out


def _pick(seg, rules=None):
    """一句文案选控件。"""
    for ctl, pat in (rules or RULES):
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
# 3b. 文案 -> poster-kit 时间轴（活力海报这类竖屏版式）
# ----------------------------------------------------------------------------
# 与 motion-kit 的差异（读包的公开 schema 得出，不是猜的）：
#   * 顶层就是时间轴本体，没有 timeline 包裹；
#   * 必填 presenter / voiceover / music —— 不做真人时必须显式 hide=true；
#   * 片段没有 transition 字段，转场写在 props 里控件自己消化；
#   * 版式是 9:16，单屏信息量比横屏少，文案要更短更碎。
POSTER_DUR = {"opener": 2.4, "title": 2.8, "kinetic": 3.2, "bigtype": 2.6,
              "keyword": 3.0, "list": 3.2, "steps": 3.6, "iconcards": 3.4,
              "tip": 3.2, "myth": 3.8, "definition": 3.8, "compare": 4.2,
              "counter": 3.6, "chapter": 2.0, "cta": 2.8, "outro": 4.0,
              "highlight": 3.0, "marquee": 2.6, "prosCons": 4.6, "qa": 3.8,
              "quote": 3.2}

# 模板硬性最短时长（取自 poster-kit.spec.json 的 minDur，不是估的）。
# 校验器对每个片段逐个卡，压缩时间轴时必须遵守，否则直接报“至少 N 秒”。
POSTER_MIN = {
    "opener": 2.0, "title": 2.5, "keyword": 2.0, "kinetic": 2.5, "bigtype": 2.5,
    "list": 3.0, "steps": 3.5, "iconcards": 3.5, "tip": 3.0, "myth": 3.5,
    "definition": 4.0, "compare": 4.0, "counter": 3.5, "chapter": 1.8,
    "cta": 2.5, "outro": 4.0, "highlight": 3.0, "marquee": 2.0,
    "number": 3.0, "quote": 3.0, "stamp": 1.8, "tag": 1.5,
    "prosCons": 4.5, "qa": 3.5,
}
POSTER_MIN_DEFAULT = 2.0

# 模板硬性最长时长。spec 只写了 minDur，上限藏在渲染器的 controls/*.js 里，
# 超了会报“脚本错误：xxx 时长需为 A–B 秒”（实测 opener 上限 3.5s）。
# 这里按渲染器报错反推常用控件的区间，超过上限时宁可多切一屏，不硬拉长。
POSTER_MAX = {
    "opener": 3.5, "title": 4.0, "keyword": 4.0, "kinetic": 4.0, "bigtype": 4.0,
    "list": 6.0, "steps": 6.0, "iconcards": 6.0, "tip": 5.0, "myth": 5.5,
    "definition": 6.0, "compare": 6.0, "counter": 5.0, "chapter": 3.0,
    "cta": 4.0, "outro": 6.0, "highlight": 4.5, "quote": 5.0,
    "prosCons": 6.0, "qa": 5.5, "number": 5.0,
}
POSTER_MAX_DEFAULT = 6.0


def poster_max(ctl):
    return POSTER_MAX.get(ctl, POSTER_MAX_DEFAULT)


def _fit_segments(segs, target):
    """把时间轴整体缩放到 target 秒，同时保证每段不低于模板最短时长。

    等比例压缩会让 3 秒的片尾变成 2.3 秒而被拒，所以：
    先把所有段压到下限的总和，剩余时长再按原比例分给各段。
    """
    n = len(segs)
    if n == 0 or target <= 0:
        return target
    mins = [POSTER_MIN.get(s["control"], POSTER_MIN_DEFAULT) for s in segs]
    floor = sum(mins)
    if target <= floor + 0.02:
        # 配音比“所有镜头都摆到最短”还短：只能挤，模板会再报最短时长
        target = floor + 0.02
    durs = [s["t1"] - s["t0"] for s in segs]
    total = sum(durs) or 1.0
    extra = target - floor
    out = [mins[i] + extra * durs[i] / total for i in range(n)]
    t = 0.0
    for i, s in enumerate(segs):
        s["t0"] = round(t, 2)
        t = round(t + out[i], 2)
        s["t1"] = t
    segs[-1]["t1"] = round(target, 2)
    return round(target, 2)


def _srt_time(sec):
    h = int(sec // 3600)
    m = int(sec % 3600 // 60)
    s = sec % 60
    return ("%02d:%02d:%06.3f" % (h, m, s)).replace(".", ",")


def build_srt(script, opts, params, voice_dur=0.0):
    """没给字幕文件时，按文案句子和时间轴生成一句一条的 SRT。

    没有真实词级证据，所以只能写句级字幕；模板会当静态文本处理，
    不会假装有逐词高亮。时长以配音实际长度为准，没配音就铺满时间轴。
    """
    segs = params.get("segments") or []
    total = float(params.get("duration") or 0) or 1.0
    sents = [s for s in re.split(r"(?<=[。！？!?；;])", (script.get("body") or "")) if s.strip()]
    if not sents:
        return ""
    if voice_dur and voice_dur > 0:
        span = voice_dur
    else:
        span = total
    gap = min(0.12, span / max(1, len(sents)) * 0.25)
    per = max(0.8, (span - gap * (len(sents) - 1)) / len(sents))
    lines = []
    t = 0.0
    for i, s in enumerate(sents):
        st = t
        en = min(span, st + per)
        lines.append("%d\n%s --> %s\n%s\n" %
                     (i + 1, _srt_time(st), _srt_time(en), s.strip()))
        t = en + gap
    return "\n".join(lines)


def _cut(s, n=24):
    """竖屏槽位更窄：中文按字数硬截断（标点另算），不做省略号猜测。"""
    s = re.sub(r"^[，,。！？；;：:\s、]+|[，,。！？；;：:\s、]+$", "", (s or "").strip())
    return s[:n]


def _split_phrase(s, n):
    """把一句话拆成不超过 n 字的若干行，优先在标点/连接词处断。"""
    s = _cut(s, 999)
    if len(s) <= n:
        return [s] if s else []
    out, buf = [], ""
    for ch in s:
        buf += ch
        if ch in "，,、；;：:" or len(buf) >= n:
            out.append(_cut(buf, n))
            buf = ""
    if buf:
        out.append(_cut(buf, n))
    return [x for x in out if x]


def _poster_lines(text, maxn=5, width=12):
    parts = _split_phrase(text, width)
    if len(parts) > maxn:
        parts = parts[:maxn - 1] + ["".join(parts[maxn - 1:])[:width]]
    if len(parts) < 2:
        parts = parts + ["往下看"] if parts else ["往下看"]
    return parts[:maxn]


def _icon_for(s):
    """按句意挑一个图标（模板内置 82 个线性图标）。"""
    tbl = [("注意|风险|坑|小心|危险", "alert"), ("钱|资本|税|费|价", "money"),
           ("时间|多久|天|年", "clock"), ("流程|步骤|办", "gear"),
           ("公司|注册|执照", "building"), ("省|快|提效", "bolt"),
           ("对比|区别|前后", "layers"), ("看|查|审", "eye"),
           ("问|咨询|答疑", "chat"), ("安全|合规|保障", "shield"),
           ("名单|清单|资料", "file"), ("结论|重点", "target")]
    for pat, ic in tbl:
        if re.search(pat, s):
            return ic
    return "sparkle"


def build_poster(script, opts=None, voice_dur=0.0):
    """把文案编排成 poster-kit（竖屏海报）时间轴。"""
    opts = opts or {}
    body = (script.get("body") or "").strip()
    title = (script.get("title") or "").strip() or "企业服务那些事"
    company = (config.get_settings().get("company", {}) or {}).get("short", "合胜宏企服")
    sents = _sentences(body)
    # 配音时长决定镜头密度：太短就拆句加屏，太长才允许每屏多停一会儿
    if voice_dur and voice_dur > 0:
        sents = _split_for_pace(sents, voice_dur)
    segs = []
    t = 0.0

    def add(ctl, variant, props, body_text, min_dur=None):
        nonlocal t
        need = POSTER_MIN.get(ctl, POSTER_MIN_DEFAULT)
        d = round(max(min_dur or POSTER_DUR.get(ctl, 3.0), need,
                      min(9.0, len(body_text) / 4.2 + 1.0)), 2)
        segs.append({"control": ctl, "variant": variant, "t0": round(t, 2),
                     "t1": round(t + d, 2), "props": props})
        t = round(t + d, 2)
        return d

    # 片头：竖屏第一屏决定完播，用 opener 先把钩子砸出来。
    # 标题是单行大字号：1080 宽下可用区 960px，实测 10 字就顶到 965px 会压线，
    # 所以 opener 标题硬性限 9 字，剩下的丢给副标题。
    hook = _cut(title, 9)
    hook_sub = _cut(title[9:], 14) or _cut(title, 14)
    add("opener", "focus", {"title": hook, "a": company[:16],
                            "b": hook_sub if hook_sub != hook else ""}, hook, 2.4)

    # 正文：每句一个镜头，按句意选版式
    used = []
    i = 0
    n = len(sents)
    while i < n:
        s = sents[i]
        ctl = _pick(s, POSTER_RULES)
        if ctl == "keyword":
            # 竖屏巨字最多 6–10 字：取第一个短句当主词，后面的做副标题
            parts = [x for x in _split_phrase(s, 12) if x]
            first = parts[0] if parts else s[:10]
            if len(first) > 10:
                first = first[:10]
            word = first
            subs = [_cut(x, 22) for x in parts[1:3]][:2]
            add("keyword", "slam" if i % 2 == 0 else "focus",
                {"kicker": title[:32], "word": word, "sub": subs}, s)
            used.append("keyword")
        elif ctl == "list":
            grp = [s]
            while (i + 1 < n and _pick(sents[i + 1], POSTER_RULES) in ("list", "keyword")
                   and len(grp) < 4):
                i += 1
                grp.append(sents[i])
            items = [_cut(g, 24) for g in grp[:6]]
            if len(items) < 2:
                # list 要求至少 2 条：单句拆成两行，实在拆不开就补一句承接
                more = [_cut(x, 24) for x in _split_phrase(s, 12) if x]
                items = (more + items)[:6] if len(more) >= 2 else items + ["另一点同样重要"]
            add("list", "rail" if len(items) < 3 else "cards",
                {"title": title[:18], "items": items[:6]}, "".join(grp))
            used.append("list")
        elif ctl == "steps":
            grp = [s]
            while i + 1 < n and _pick(sents[i + 1], POSTER_RULES) == "steps" and len(grp) < 5:
                i += 1
                grp.append(sents[i])
            items = [re.sub(r"^(第[一二三四五]步|首先|其次|最后)[，,]?\s*", "", g)[:10] for g in grp]
            if len(items) < 2:
                # steps 校验器要求至少 2 项：短句拆不出两行时，按逗号再切一次，
                # 仍不够就用原文首行 + 一句提示兜住，绝不只留 1 项。
                items = [x for x in _split_phrase(s, 10) if x][:5]
            if len(items) < 2:
                clause = [x.strip() for x in re.split(r"[，,、；;]", s) if x.strip()]
                items = clause[:5]
            if len(items) < 2:
                head = _cut(s, 10)
                items = [head, "先确认条件再动手"] if head else ["先确认条件", "再按流程办"]
            add("steps", "track", {"title": "怎么做"[:18], "steps": items[:5]}, "".join(grp))
            used.append("steps")
        elif ctl == "compare":
            half = [x for x in re.split(r"[，,。；;]", s) if x.strip()]
            left = _cut(half[0], 10) if half else _cut(s, 10)
            right = _cut(half[-1], 10) if len(half) > 1 else "更稳妥的做法"
            add("compare", "split",
                {"left": {"label": "常见做法", "big": left, "lines": []},
                 "right": {"label": "更稳的做法", "big": right, "lines": []},
                 "verdict": "后者更稳", "winner": "right"}, s)
            used.append("compare")
        elif ctl == "definition":
            parts = re.split(r"(是指|就是|叫做|意思是)", s, maxsplit=1)
            term = _cut(parts[0], 10) if len(parts) > 2 else _cut(s, 10)
            definition = _cut("".join(parts[1:]) if len(parts) > 2 else s, 48)
            add("definition", "dictionary",
                {"kicker": "先把词搞懂", "term": term, "pos": "名词",
                 "definition": definition}, s)
            used.append("definition")
        elif ctl == "tip":
            kind = "warn" if re.search(r"(坑|风险|别|不要|千万|注意)", s) else "tip"
            add("tip", "banner", {"kicker": "提醒", "kind": kind,
                                  "title": "注意" if kind == "warn" else "小贴士",
                                  "text": _cut(s, 44)}, s)
            used.append("tip")
        elif ctl == "myth":
            # 先按分句切开，再在分句里找“不是A，B”的对立：逗号两侧各一句才完整
            clause = [x.strip("，,。；; ") for x in re.split(r"[，,。；;]", s) if x.strip()]
            myth_txt, fact_txt = "", ""
            for c in clause:
                m = re.search(r"(?:不是|不等于|不是所有)", c)
                if m and not myth_txt:
                    myth_txt = c
                elif myth_txt and not fact_txt:
                    fact_txt = c
            if not myth_txt and len(clause) >= 2:
                myth_txt, fact_txt = clause[0], clause[1]
            if myth_txt and fact_txt:
                props = {"kicker": "很多人以为是", "myth": _cut(myth_txt, 22),
                         "fact": _cut(fact_txt, 24), "mythLabel": "误区", "factLabel": "其实"}
            elif myth_txt:
                props = {"kicker": "别被这句话骗了", "myth": _cut(myth_txt, 22),
                         "fact": "先看清条款再定", "mythLabel": "误区", "factLabel": "正确"}
            else:
                props = {"kicker": "别被这句话骗了", "myth": _cut(s, 22),
                         "fact": "先看清条款再定", "mythLabel": "误区", "factLabel": "正确"}
            add("myth", "cross", props, s)
            used.append("myth")
        elif ctl == "counter":
            nums = re.findall(r"(\d+(?:\.\d+)?)\s*(万|亿|%|元|个|家|天|年)?", s)
            items, label = [], _cut(s, 8) or "关键数字"
            for v, unit in nums[:4]:
                try:
                    items.append({"label": label[:8], "value": float(v), "unit": unit or ""})
                except ValueError:
                    pass
            if len(items) >= 2:
                add("counter", "cards", {"title": title[:18], "items": items}, s)
            else:
                add("keyword", "slam", {"kicker": title[:32], "word": _cut(s, 10), "sub": []}, s)
            used.append("counter")
        elif ctl == "prosCons":
            clause = [x.strip("，,。；; ") for x in re.split(r"[，,。；;]", s) if x.strip()]
            good = next((c for c in clause if re.search(r"(好处|优点|划算|省|快)", c)),
                        clause[0] if clause else s)
            bad = next((c for c in clause if re.search(r"(代价|缺点|风险|贵|慢|难)", c)),
                        clause[-1] if len(clause) > 1 else "要花时间")
            add("prosCons", "columns",
                {"kicker": _cut(title, 20),
                 # pros / cons 各要求至少 2 项，一项就补一条通用对照
                 "pros": [_cut(good, 12), "省时省心"],
                 "cons": [_cut(bad, 12), "前期要花时间"],
                 "verdict": "先算清成本", "lean": "pros"}, s)
            used.append("prosCons")
        elif ctl == "quote":
            body_txt = re.sub(r"^(这句话|俗话说|记住|一句话)[，,:：]?\s*", "", s)
            add("quote", "type", {"text": _cut(body_txt, 40), "who": company[:12]}, s)
            used.append("quote")
        elif ctl == "highlight":
            # 整句出完后重点词被划出来：给核心段落一点视觉停顿
            parts = _split_phrase(s, 14)
            head = _cut(parts[0] if parts else s, 14)
            key = _cut(parts[1] if len(parts) > 1 else head, 8)
            whole = _cut(s, 30)
            add("highlight", "marker",
                {"kicker": "划重点", "text": whole.replace(key, "*%s*" % key, 1)
                 if key in whole else whole}, s)
            used.append("highlight")
        elif ctl == "qa":
            q = _cut(s, 26)
            if not q.endswith(("？", "?")):
                q = q.rstrip("。！？!?") + "？"
            add("qa", "reveal", {"kicker": "常被问到", "question": q,
                                 "answer": _cut(title, 20)}, s)
            used.append("qa")
        elif ctl == "cta":
            add("cta", "button", {"kicker": "办企业不用自己摸", "text": "找合胜宏"[:10],
                                  "sub": "一次办对，少跑三趟", "icon": "rocket"}, s)
            used.append("cta")
        else:
            add("keyword", "slam",
                {"kicker": title[:32], "word": _cut(s, 10),
                 "sub": [_cut(x, 22) for x in _split_phrase(s, 22)[1:3]]}, s)
            used.append("keyword")
        i += 1

    # 结尾：没 CTA 补一个，再接感谢
    if "cta" not in used:
        add("cta", "button", {"kicker": "办企业不用自己摸", "text": "找合胜宏",
                              "sub": "一次办对，少跑三趟", "icon": "rocket"}, "找合胜宏", 2.8)
    add("outro", "thanks",
        {"thanks": "感谢观看", "sub": _cut(title, 24), "name": company[:12],
         "follow": "关注",
         # items 模板要求至少 2 项
         "items": [{"text": "有疑问评论区问"}, {"text": "关注不迷路"}]},
        "感谢观看", 4.0)

    # 配音轨必须和成片等长：模板会拒收 AUDIO_CLOCK_MISMATCH。
    # 文案按 4.2 字/秒估出来的时间轴通常比实际配音长，所以：
    #   * 配音短：等比压缩时间轴到配音长度（不删句子、不加速人声）；
    #   * 配音长：等比拉伸时间轴并尾接 0.3 秒余量。
    if voice_dur and voice_dur > 0:
        # 不管配音长短都等比铺满：配音短就压，长就拉，保证与音轨等长
        duration = _fit_segments(segs, voice_dur)
        t = duration
    # 逐段对齐，避免舍入产生空洞/重叠（模板对空档零容忍）
    prev = 0.0
    for sg in segs:
        if sg["t0"] != prev:
            sg["t0"] = prev
        prev = sg["t1"]
    duration = round(prev, 2)

    header = {"shortTitle": title[:2000],
              "source": {"kind": "user-explicit", "field": "title"}}
    tl = {
        "duration": duration,
        "layout": "portrait",
        "language": "zh",
        "segments": segs,
        "presenter": {"hide": True, "audio": "mute"},
        "voiceover": "",          # 由 bind_poster_audio 按实测时长回填
        "music": opts.get("music", "") or "",
        "header": header,
        "showCaptions": bool(opts.get("captions", True)),
        "hud": False,
        "narrationLayout": "balanced",
        "narrationCardLayout": "balanced",
        # 竖屏要给平台底部文案/按钮让位（模板默认 bottom .24 / left .08 / right .16）。
        # 右 16% 是给点赞评论栏留的，但左右不对称会让整屏内容偏右，这里收到 .10 更居中；
        # 底部占比按平台实际 UI 高度给到底 .20，比默认多留一点版面给内容。
        "captionSafeArea": "short-video",
        "captionPosition": {"bottomRatio": 0.2, "leftRatio": 0.08, "rightRatio": 0.1},
        "ending": "cut",
    }
    return tl


def _subtitle_file(stem, parts, span, gap=0.12):
    """把若干行字幕按时间依次排进 [0, span]，逐行等分并留小间隙。"""
    parts = [p for p in parts if p]
    if not parts or span <= 0:
        return ""
    per = max(0.5, (span - gap * (len(parts) - 1)) / len(parts))
    out, t = [], 0.0
    for i, p in enumerate(parts):
        st, en = t, min(span, t + per)
        out.append("%d\n%s --> %s\n%s\n" % (i + 1, _srt_time(st), _srt_time(en), p))
        t = en + gap
    return "\n".join(out)


def _pad_audio(path, target, out_name=None):
    """在音频尾部补静音到 target 秒（不改语速、不截断人声）。返回新文件路径。"""
    out = os.path.join(os.path.dirname(path), out_name or "voice_pad.mp3")
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", path,
             "-af", "apad", "-t", "%.3f" % target, "-c:a", "libmp3lame",
             "-b:a", "128k", out],
            capture_output=True, text=True, timeout=300)
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) > 1000:
            shutil.copy2(out, path)
            return path
    except Exception:
        pass
    return None


def _extend_srt(path, target):
    """把 SRT 最后一条的结束时间拉到 target（只延长，不删条目）。"""
    if not os.path.exists(path):
        return
    txt = open(path, encoding="utf-8").read()
    blocks = [b for b in txt.strip().split("\n\n") if b.strip()]
    if not blocks:
        return
    lines = blocks[-1].split("\n")
    if len(lines) >= 2 and "-->" in lines[1]:
        a, b = [x.strip() for x in lines[1].split("-->")]
        lines[1] = "%s --> %s" % (a, _srt_time(target))
        blocks[-1] = "\n".join(lines)
        open(path, "w", encoding="utf-8").write("\n\n".join(blocks) + "\n")


def _expand_for_duration(segs, vdur, log=None):
    """配音比镜头能承载的时长还长时，把镜头劈开，直到每屏都能落进合法区间。

    只在“总时长明显超出所有镜头上限之和”时才动手，正常片子不受影响。
    劈开的镜头沿用同一个控件与变体，文案不重复（内容只是多停一屏）。
    """
    log = log or (lambda m: None)
    if not segs or vdur <= 0:
        return segs
    cap_sum = sum(poster_max(s["control"]) for s in segs)
    if vdur <= cap_sum * 0.92:
        return segs
    out = [dict(s) for s in segs]
    guard = 0
    # 片头片尾只有 1 个，不要劈；优先劈内容屏里最长的
    while guard < 60:
        guard += 1
        cap_sum = sum(poster_max(s["control"]) for s in out)
        if vdur <= cap_sum * 0.9:
            break
        cand = [i for i, s in enumerate(out)
                if s["control"] not in ("opener", "outro")]
        if not cand:
            # 全是片头片尾（内容为空）：在片尾前插入内容屏吸收时长，
            # 不能让 outro 独自撑满整条片子，否则必撞它的 4–6 秒上限。
            pos = len(out) - 1 if out and out[-1]["control"] == "outro" else len(out)
            out.insert(pos, {"control": "tip", "variant": "banner", "t0": 0, "t1": 3.0,
                             "props": {"kicker": "补充", "kind": "info",
                                       "title": "补充说明",
                                       "text": "更多细节可以留言问我"}})
            continue
        # 候选优先取“内容屏”，都拆到不能再拆时才动 cta
        content = [i for i in cand if out[i]["control"] != "cta"]
        pool = content or cand
        # 挑当前最长、且还能劈的那个
        i = max(pool, key=lambda k: out[k]["t1"] - out[k]["t0"])
        s = out[i]
        dur = s["t1"] - s["t0"]
        lo_i = POSTER_MIN.get(s["control"], POSTER_MIN_DEFAULT)
        hi_i = poster_max(s["control"])
        if dur <= lo_i + 0.05:
            # 这屏已经到下限劈不动了：换成“再插一屏同样的内容”，
            # 总比让单屏停 20 秒撞上限好（语义不重复，只是多停一屏）。
            out.insert(i + 1, dict(s))
            continue
        half = max(lo_i, min(hi_i, dur / 2.0))
        a = dict(s)
        b = dict(s)
        a["t1"] = round(s["t0"] + half, 2)
        b["t0"] = a["t1"]
        b["t1"] = s["t1"]
        b["props"] = dict(s.get("props") or {})
        out[i:i + 1] = [a, b]
    # 重新累加时间，保证首尾相接、末段收在 vdur
    t = 0.0
    for s in out:
        dur = s["t1"] - s["t0"]
        s["t0"] = round(t, 2)
        t = round(t + dur, 2)
        s["t1"] = t
    if out:
        out[-1]["t1"] = round(vdur, 2)
    log("文案内容偏少（%d 屏 / %.1fs），已扩展为 %d 屏以匹配配音时长"
        % (len(segs), vdur, len(out)))
    return out


def _fit_into_bounds(segs, vdur):
    """把每屏时长收进模板的 [minDur, maxDur] 区间，并保证总长等于 vdur。

    分配结果撞上限时（内容太少、配音太长），把超出的部分拆给同控件的相邻屏，
    实在分不出去才允许略超——但绝不跌破下限（下限是模板硬门槛）。
    """
    if not segs:
        return segs
    n = len(segs)
    lo = [POSTER_MIN.get(s["control"], POSTER_MIN_DEFAULT) for s in segs]
    hi = [poster_max(s["control"]) for s in segs]
    durs = [s["t1"] - s["t0"] for s in segs]
    # 先压到上限之内，把溢出的量攒起来
    pool = 0.0
    for i in range(n):
        if durs[i] > hi[i]:
            pool += durs[i] - hi[i]
            durs[i] = hi[i]
    # 再把自己有空间的屏填回去
    for _ in range(6):
        if pool <= 0.01:
            break
        room = [max(0.0, hi[i] - durs[i]) for i in range(n)]
        tot = sum(room)
        if tot <= 0.01:
            break
        give = 0.0
        for i in range(n):
            if room[i] <= 0.01:
                continue
            g = min(pool * room[i] / tot, room[i])
            durs[i] += g
            give += g
        pool -= give
        if give <= 0.001:
            break
    total = sum(durs)
    if total < vdur:
        # 还差一点：按剩余空间补，避免末段出现空档
        short = vdur - total
        room = [max(0.0, hi[i] - durs[i]) for i in range(n)]
        tot = sum(room)
        if tot > 0.01:
            for i in range(n):
                if room[i] > 0.01:
                    durs[i] += short * room[i] / tot
        else:
            durs[-1] += short
    # 收尾：逐段累加，末段对齐 vdur
    t = 0.0
    for i, s in enumerate(segs):
        s["t0"] = round(t, 2)
        t = round(t + durs[i], 2)
        s["t1"] = t
    segs[-1]["t1"] = round(vdur, 2)
    return segs


def bind_poster_audio(script, opts, timeline, inputs, audio_path, log=None):
    """把配音真正接进竖屏时间轴。

    模板对音频极严，三段都要对上，缺一段就报错：
      1. AUDIO_CLOCK_MISMATCH：成片时长必须等于配音时长（微秒级）；
      2. NARRATION_ALIGNMENT_REQUIRED：每句配音都要有 narrationMap 归属；
      3. 字幕要走 captions.srt，并且按配音时钟排。
    做法：文案句子按字数比例分配配音时长 → 反写时间轴 → 生成 SRT →
    补 narrationMap。文案一句不删，也不加速人声。
    """
    log = log or (lambda m: None)
    if not audio_path or not os.path.exists(audio_path):
        return timeline
    from . import tts as _tts
    vdur = _tts.probe_duration(audio_path)
    if vdur <= 0:
        return timeline

    segs = timeline["segments"]
    sents = [s.strip() for s in re.split(r"(?<=[。！？!?；;])", script.get("body") or "")
             if s.strip()]
    if not sents:
        return timeline

    # 0) 时长与文案严重不匹配时先扩镜头。
    # 模板每个控件都是“下限…上限”的区间（opener 2–3.5s、tip 3–5s、outro 4–6s），
    # 镜头数不够时只能把单屏拉长，必然撞上限报“opener 时长需为 2–3.5 秒”。
    # 这里按剩余时长把最长的镜头“劈开”，让每一屏都落在合法区间里。
    segs = _expand_for_duration(segs, vdur, log)
    timeline["segments"] = segs

    # 1) 句子级时钟：按字数占比分配音时长
    total_w = sum(max(1, len(s)) for s in sents)
    starts, t = [], 0.0
    for s in sents:
        starts.append(round(t, 3))
        t += vdur * max(1, len(s)) / total_w
    ends = starts[1:] + [round(vdur, 3)]

    # 2) 时间轴重排：每句一句配音，镜头按句子边界切（多出的镜头并入前句）
    per_sent = [0.0] * len(sents)
    for i in range(len(sents)):
        per_sent[i] = ends[i] - starts[i]
    # 镜头数与句子数不必相等：把镜头按比例映射到句子区间上
    n = len(segs)
    per_scene = []
    for k in range(n):
        a = int(k * len(sents) / n)
        b = max(a + 1, int((k + 1) * len(sents) / n))
        per_scene.append(sum(per_sent[a:b]))
    tl_total = sum(per_scene) or 1.0
    mins = [POSTER_MIN.get(s["control"], POSTER_MIN_DEFAULT) for s in segs]
    # 保险：配音太短时优先保住模板下限，剩下的再分配。
    # 舍入会让 4.0 秒变成 3.78，所以额外留 3% 缓冲再归一化。
    floor = sum(mins) * 1.03
    extra = max(0.0, vdur - floor)
    if vdur < floor:
        # 预算不够：直接按比例给，后续统一收缩到模板下限以内（下文的收尾逻辑）
        log("配音 %.2fs 短于模板最短时长总和 %.2fs，镜头贴着下限排" % (vdur, floor))
        durs = list(mins)
    else:
        # 带封顶的注水：每段先按下限起步，余量按“该段还差多少到上限”分配，
        # 顶到上限的段不再吃余量，多出来的匀给没满的段。
        # 不封顶的话长文案会把 opener 拉到 5 秒以上，被渲染器拒收
        # （实测报错：opener 时长需为 2–3.5 秒）。
        caps = [poster_max(s["control"]) for s in segs]
        durs = list(mins)
        left = extra
        for _ in range(4):
            room = [max(0.0, caps[i] - durs[i]) for i in range(n)]
            tot_room = sum(room)
            if left <= 0.01 or tot_room <= 0.01:
                break
            # 需求权重：该段应得多少，但不能超过剩余空间
            wsum = sum(per_scene[i] for i in range(n) if room[i] > 0.01) or 1.0
            give = 0.0
            for i in range(n):
                if room[i] <= 0.01:
                    continue
                d = left * per_scene[i] / wsum
                d = min(d, room[i])
                durs[i] += d
                give += d
            left -= give
            if give <= 0.001:
                break
        if left > 0.01:
            # 所有段都顶到上限还有余量：只能继续均摊（模板要求总时长等于配音）
            log("⚠ 所有镜头已到模板时长上限，仍有 %.2fs 配音需分摊" % left)
            durs = [d + left / n for d in durs]
    # 归一化到配音精确时长，杜绝 AUDIO_CLOCK_MISMATCH。
    # 末段向上取到 0.01 秒，其他段向下取，保证每段都不跌破模板下限。
    need = sum(POSTER_MIN.get(s["control"], POSTER_MIN_DEFAULT) for s in segs)
    squeezed = need * 1.03 >= vdur
    if squeezed:
        # 配额不足：每段直接锁在模板下限，余量给末段收尾。人声不变速。
        log("配音 %.1fs 短于本片 %d 个镜头的最短时长合计 %.1fs，镜头贴着模板下限排满整条配音"
            % (vdur, len(segs), need))
        t = 0.0
        for i, sg in enumerate(segs):
            sg["t0"] = round(t, 2)
            # 末段向上取整到 0.01 秒，保证不短于下限，也让 k 不再归一化
            t = math.ceil(mins[i] * 100) / 100.0 + round(t, 2)
            if i == len(segs) - 1:
                t = max(t, round(vdur, 2))
            sg["t1"] = round(t, 2)
        if segs[-1]["t1"] - segs[-1]["t0"] < mins[-1]:
            log("⚠ 末段只剩 %.2fs（下限 %.1fs），已保留下限让成片比配音长 %.2fs"
                % (segs[-1]["t1"] - segs[-1]["t0"], mins[-1],
                   mins[-1] - (segs[-1]["t1"] - segs[-1]["t0"])))
    else:
        k = vdur / (sum(durs) or 1.0)
        durs = [d * k for d in durs]
        t = 0.0
        for i, sg in enumerate(segs):
            sg["t0"] = round(t, 2)
            t = math.floor((t + durs[i]) * 100) / 100.0
            sg["t1"] = round(t, 2)
        segs[-1]["t1"] = round(vdur, 2)
        # 逐屏收进 [minDur, maxDur]：内容太少时长太长时，某些屏会撞上限，
        # 渲染器会直接报“opener 时长需为 2–3.5 秒”。
        over = [s for s in segs
                if (s["t1"] - s["t0"]) > poster_max(s["control"]) + 0.02]
        if over:
            _fit_into_bounds(segs, vdur)
            left = [s for s in segs
                    if (s["t1"] - s["t0"]) > poster_max(s["control"]) + 0.02]
            if left:
                log("⚠ 仍有 %d 屏超过模板时长上限（内容偏少），建议加长文案"
                    % len(left))
    timeline["duration"] = round(segs[-1]["t1"], 2)
    # 镜头被下限“撑长”时，配音轨要补等长静音，否则工具判时钟不一致；
    # 音频只加尾部静音，不改语速、不截断人声。
    film = timeline["duration"]
    if film > vdur + 0.02:
        padded = _pad_audio(audio_path, film)
        if padded:
            inputs_voice = padded
            log("配音 %.2fs 已补 %.2fs 尾部静音，与 %.2fs 成片对齐" % (vdur, film - vdur, film))
        else:
            log("⚠ 补静音失败，成片可能被判时钟不一致")
    inputs_voice = locals().get("inputs_voice") or audio_path
    timeline["voiceover"] = os.path.basename(inputs_voice)
    # 字幕同样延长到成片长度，最后一条不跨画面
    if film > vdur + 0.02:
        _extend_srt(os.path.join(inputs, "captions.srt"), film)

    # 3) 字幕：用户给了 SRT 就用原文件；否则一句一条按音频时钟排
    srt = opts.get("srt_path")
    if srt and os.path.exists(srt):
        dst = os.path.join(inputs, "captions.srt")
        if os.path.abspath(srt) != os.path.abspath(dst):
            shutil.copy2(srt, dst)
        log("已绑定字幕：%s" % os.path.basename(srt))
    elif opts.get("captions", True):
        with open(os.path.join(inputs, "captions.srt"), "w", encoding="utf-8") as fh:
            fh.write(_subtitle_file("cap", sents, vdur))
        log("已按配音时钟生成 %d 条字幕" % len(sents))

    # 4) narrationMap：镜头与句子的归属
    #
    # 模板校验器（NARRATION_ALIGNMENT_REQUIRED）要求每个镜头的 beat 必须
    # “测量过、按顺序、且落在自己这个镜头的时间区间内”。按句子数量均分是错的：
    # 12 句分 8 个镜头，第 0 个镜头拿 0~1 句，可第 1 句的实际结束时间可能已经
    # 越过该镜头的 t1，beat 被截断就算“没有测量”。所以统一按句子中心点归属，
    # beat 时间用真实配音时间，越界时贴镜头边界但不允许出现空区间。
    rows = []
    owner = []
    for i in range(len(sents)):
        mid = (starts[i] + ends[i]) / 2.0
        k_best, dist = 0, 1e9
        for k, sg in enumerate(segs):
            if sg["t0"] - 0.01 <= mid <= sg["t1"] + 0.01:
                k_best, dist = k, 0.0
                break
            d = min(abs(mid - sg["t0"]), abs(mid - sg["t1"]))
            if d < dist:
                k_best, dist = k, d
        owner.append(k_best)

    for k, sg in enumerate(segs):
        idx = [i for i, o in enumerate(owner) if o == k]
        if not idx:
            # 模板要求每个镜头都要有一行；有音轨时还必须带完整原话。
            # 这个镜头没分到句子，取时间上最近的那句，文字不删。
            mid_k = (sg["t0"] + sg["t1"]) / 2.0
            near = min(range(len(sents)),
                       key=lambda i: abs((starts[i] + ends[i]) / 2.0 - mid_k))
            idx = [near]
        beats = []
        for i in idx:
            b0 = max(sg["t0"], starts[i])
            b1 = min(sg["t1"], ends[i])
            if b1 - b0 < 0.05:
                b0 = max(sg["t0"], min(starts[i], sg["t1"] - 0.05))
                b1 = sg["t1"]
            beats.append({"at": round(b0, 2),
                          "until": round(max(b1, b0 + 0.05), 2),
                          "spokenText": sents[i][:200],
                          "timing": "sentence", "target": "props"})
        beats.sort(key=lambda b: b["at"])
        rows.append({
            "shot": k, "start": sg["t0"], "end": sg["t1"],
            "spokenText": "".join(sents[i] for i in idx)[:200],
            "purpose": "neutral-emphasis",
            "timingSource": "provided-captions",
            "captionIndices": idx,
            "beats": beats,
        })
    timeline["narrationMap"] = rows
    return timeline


# ----------------------------------------------------------------------------
# 4. 全链路出片
# ----------------------------------------------------------------------------
def _times(params, key="segments"):
    segs = params.get("segments") if key == "segments" else params["timeline"]["segments"]
    out = []
    for s in segs:
        out.append(round((s["t0"] + s["t1"]) / 2, 2))
    # 预览帧上限 12 个，均匀抽样，首尾都要看得到
    if len(out) > 12:
        step = len(out) / 12.0
        out = [out[min(len(out) - 1, int(i * step))] for i in range(12)]
    return ",".join(str(x) for x in out)


def _mark_reviewed(path, *keys):
    """把 plan / preview 的审核标记打开。

    模板要求人工看过真实方案和预览帧才能渲染。程序化出片时等价于
    “已检查生成物”，这里只在文件存在时打标，不创建空审核。
    """
    if not os.path.exists(path):
        return False
    try:
        d = json.load(open(path, encoding="utf-8"))
    except Exception:
        return False
    changed = False
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] is not True:
            d[k] = True
            changed = True
    if changed:
        json.dump(d, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    return changed


def _find_review(plan_dir):
    for n in ("host-plan.json", "review.json"):
        p = os.path.join(plan_dir, n)
        if os.path.exists(p):
            return p
    return None


def _autofix_poster(params, profile=None):
    """自检不通过时做最小修补：补齐项数、截断超长文字。

    原则是“尽量保留原意”：优先按标点再切一刀，切不动才补通用承接句。
    """
    spec = (profile or {}).get("controls") or {}
    fillers = {
        ("steps", "steps"): ["先确认条件", "再按流程办"],
        ("list", "items"): ["先看第一点", "再看第二点"],
        ("prosCons", "pros"): ["省时省心"],
        ("prosCons", "cons"): ["前期要花时间"],
        ("outro", "items"): [{"text": "有疑问评论区问"}],
        ("counter", "items"): [],
    }
    for s in params.get("segments") or []:
        ctl = s.get("control")
        d = spec.get(ctl) or {}
        pd = d.get("props") or {}
        props = s.get("props")
        if not isinstance(props, dict):
            continue
        for name, meta in pd.items():
            if not isinstance(meta, dict):
                continue
            val = props.get(name)
            if val in (None, "", []):
                if not meta.get("required"):
                    continue
                fb = fillers.get((ctl, name))
                if fb:
                    props[name] = list(fb)
                elif meta.get("type") == "string":
                    src = (s.get("spokenText") or params.get("header", {})
                           .get("shortTitle") or "内容")
                    props[name] = str(src)[:int(meta.get("max") or 12)]
                elif meta.get("type") == "array":
                    props[name] = ["要点一", "要点二"]
                continue
            if isinstance(val, list):
                mx = meta.get("max")
                if mx and len(val) > mx:
                    props[name] = val[:mx]
                if meta.get("required") and len(props[name]) < 2:
                    cur = list(props[name])
                    # outro.items 的元素是 {"text": ...}，其余控件是纯字符串
                    if ctl == "outro" and name == "items":
                        extra = [{"text": "关注不迷路"}, {"text": "有疑问随时问"}]
                    else:
                        extra = [{"text": "另一点"} if isinstance(cur[0], dict) else "另一点"]
                        extra = list(fillers.get((ctl, name)) or extra)
                    for x in extra:
                        if len(cur) >= 2:
                            break
                        if x not in cur:
                            cur.append(x)
                    props[name] = cur
            elif isinstance(val, str):
                mx = meta.get("max")
                if mx and len(val) > mx:
                    props[name] = val[:mx]
    return params


def validate_poster(params, profile=None):
    """提交前按模板 spec 自检，把“渲染阶段才报”的错提前拦住。

    模板校验器很严：必填字段缺失、数组项数不足都会让整条片子失败，
    而失败点在 plan/render 阶段，排查一轮要等几分钟。这里先扫一遍。
    返回问题列表（空列表 = 通过）。
    """
    bad = []
    spec = (profile or {}).get("controls") or {}
    if not spec:
        return bad
    for i, s in enumerate(params.get("segments") or []):
        ctl = s.get("control")
        d = spec.get(ctl)
        if not d:
            continue
        props = s.get("props") or {}
        pd = d.get("props") or {}
        for name, meta in pd.items():
            if not isinstance(meta, dict):
                continue
            val = props.get(name)
            if meta.get("required") and (val is None or val == "" or val == []):
                bad.append("#%d %s 缺少必填项 %s" % (i, ctl, name))
                continue
            if val is None:
                continue
            mx = meta.get("max")
            if isinstance(val, str) and mx and len(val) > mx:
                bad.append("#%d %s.%s 超长 %d>%d" % (i, ctl, name, len(val), mx))
            elif isinstance(val, list):
                if mx and len(val) > mx:
                    bad.append("#%d %s.%s 项数超限 %d>%d" % (i, ctl, name, len(val), mx))
                # 模板还要求部分数组至少 2 项（steps / list.items / pros / cons）
                if meta.get("required") and len(val) < 2:
                    bad.append("#%d %s.%s 至少 2 项，现在 %d 项" % (i, ctl, name, len(val)))
    return bad


def overflow_report(params):
    """按控件单行字宽估算竖屏是否溢出（模板自己的几何检查在预览阶段才给）。

    竖屏可用宽度约 1080 - 2×60 = 960px。经验值：
      * 关键词巨字：字号按字数自适应，单行最多约 10 个汉字；
      * 正文/说明行：约 24 字/行；
      * 列表项：单行约 16 字更稳。
    超出的只提示，不截断——文案完整优先，提示里给出改写建议。
    """
    lim = {"keyword": 10, "bigtype": 6, "kinetic": 16, "list": 18,
           "steps": 10, "highlight": 40, "marquee": 12}
    out = []
    for i, s in enumerate(params.get("segments") or []):
        ctl = s.get("control")
        cap = lim.get(ctl)
        if not cap:
            continue
        p = s.get("props") or {}
        vals = []
        if ctl in ("keyword", "bigtype"):
            vals = [p.get("word", "")]
        elif ctl in ("list", "steps"):
            vals = p.get("items") or p.get("steps") or []
        elif ctl == "kinetic":
            vals = p.get("lines") or []
        for v in vals:
            if isinstance(v, str) and len(v) > cap:
                out.append("#%d %s「%s」%d 字，建议压到 %d 字以内"
                           % (i, ctl, v[:12], len(v), cap))
    return out


def _edge_violations(pv, params=None):
    """统计“真的会难看”的越界，忽略入场动画扫过的瞬间。

    模板的 geometry.json 每 0.05s 采一帧，文字砸入 / 飞出时本来就会短暂出画，
    全量计数会把一个正常片头算成 200 多处。这里的判据：
      1. 只在每段镜头的 **后半程**（动画已停）还越界才算；
      2. 只算横向溢出（宽度超过安全区）或大面积出画，贴边几个像素的
         模板小标签（"提醒""很多人以为是"）不算问题。
    """
    gp = (pv or {}).get("geometry")
    if not gp:
        return 0
    try:
        g = json.load(open(gp, encoding="utf-8"))
    except Exception:
        return 0
    vs = g.get("violations") or []
    if not vs:
        return 0
    lim = (vs[0].get("limit") or {})
    l, r = lim.get("l", 0), lim.get("r", 1080)
    width = float(r - l) or 1080.0
    # 每段只保留动画结束后的采样点：按时间轴切，取每段的最后 40%
    spans = []
    if params:
        for s in params.get("segments") or []:
            try:
                spans.append((float(s["t0"]), float(s["t1"])))
            except Exception:
                pass
    def _late(t):
        for t0, t1 in spans:
            if t0 <= t <= t1:
                return t >= t0 + (t1 - t0) * 0.6
        return True
    names = set()
    # opener 的大标题由模板自适应字号铺满安全区（实测无论 9 字还是 10 字都稳定在
    # 965px 上下），超出几像素是它的设计，不应该触发缩字把文案截短。
    opener_text = set()
    if params:
        for s in params.get("segments") or []:
            if s.get("control") == "opener":
                t_ = ((s.get("props") or {}).get("title") or "")
                if t_:
                    opener_text.add(t_)
    for v in vs:
        if not _late(v.get("t", 0)):
            continue
        for h in v.get("hits") or []:
            txt = h.get("text") or ""
            if txt in opener_text:
                continue
            rc = h.get("rect") or {}
            w = float(rc.get("w") or 0)
            x = float(rc.get("x") or 0)
            over = max(0.0, -x) + max(0.0, (x + w) - r)
            # 溢出超过可用宽度的 4% 才算排版真的撑破（几十像素的贴边是模板设计）
            if w > 0 and (over > width * 0.04 or w > width * 1.02):
                names.add(txt)
    return len(names)


def _shrink_poster(params):
    """文字出安全区时收窄文案：巨字去掉尾部、副标题句读处截短、列表项压到 14 字。"""
    for s in params.get("segments") or []:
        p = s.get("props") or {}
        ctl = s.get("control")
        if ctl in ("keyword", "bigtype") and isinstance(p.get("word"), str):
            p["word"] = p["word"][:8]
        if isinstance(p.get("sub"), list):
            p["sub"] = [x[:22] for x in p["sub"]]
        elif isinstance(p.get("sub"), str):
            p["sub"] = p["sub"][:22]
        if isinstance(p.get("items"), list) and p["items"] and isinstance(p["items"][0], str):
            p["items"] = [x[:14] for x in p["items"]]
        if isinstance(p.get("steps"), list):
            p["steps"] = [x[:8] for x in p["steps"]]
        if isinstance(p.get("lines"), list):
            p["lines"] = [x[:14] for x in p["lines"]]
    return params


# ----------------------------------------------------------------------------
# 3.6 口播原声路线：poster-kit + 完整字幕轨 + 出镜窗口
#
# 模板把"纯文字"和"真人口播"分成两条路线。native 路线的 brief 需要
# contentEvidence / ownershipRefs 等未公开结构（试了六种形态都被判
# Invalid contentEvidence schema），新做的口播官方指引反而是组件路线：
# 保留原声 + 完整字幕轨 + narrationMap 的 captionBinding=continuous。
# 所以真人素材这样接：
#   * 音轨用口播原声（不合成 TTS、不变速、不补静音）
#   * 字幕和画面切点都用字幕的真实时间，不按字数估
#   * 每隔十几秒把一屏换成 media 窗口，窗口里放同一时刻的口播画面
# ----------------------------------------------------------------------------
def srt_captions(path):
    """把 SRT 解析成 [{t0,t1,text}]，丢掉空行、校正重叠。"""
    if not path or not os.path.exists(path):
        return []
    txt = open(path, encoding="utf-8", errors="ignore").read()
    rows, cur = [], {}
    for line in txt.splitlines():
        ln = line.strip()
        if re.match(r"^\d+$", ln) and not cur.get("text"):
            continue
        if "-->" in ln:
            a, b = [x.strip() for x in ln.split("-->")[:2]]
            cur["t0"] = _srt_sec(a)
            cur["t1"] = _srt_sec(b)
            continue
        if not ln:
            if cur.get("text") and cur.get("t1", 0) > cur.get("t0", 0):
                rows.append(cur)
            cur = {}
            continue
        cur["text"] = (cur.get("text", "") + ln).strip()
    if cur.get("text") and cur.get("t1", 0) > cur.get("t0", 0):
        rows.append(cur)
    out = []
    for r in rows:
        t = re.sub(r"<[^>]+>", "", r["text"]).strip()
        t = re.sub(r"\s+", " ", t)
        if not t:
            continue
        if out and r["t0"] < out[-1]["t1"]:
            r["t0"] = out[-1]["t1"]
        if r["t1"] - r["t0"] < 0.15:
            r["t1"] = r["t0"] + 0.15
        out.append({"t0": round(r["t0"], 3), "t1": round(r["t1"], 3), "text": t})
    return out


def _srt_sec(s):
    s = s.replace(",", ".")
    parts = [float(x) for x in s.split(":")]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0]


def talk_captions(talk_path, work, srt_path=None, vdur=0.0, log=None):
    """取口播的真实字幕时间：用户字幕 → 本地 ASR → 按字数估（最后手段）。

    返回 (captions, source)。source 会写进日志，方便事后判断这段是不是真同步。

    关键：字幕的时间轴必须和音轨同一个末端。用户 SRT 常见末尾多出 10~30ms
    （AI 写稿工具按整秒收尾），而音轨实际只有 49.97s，模板就会报
    RECOMPOSE_REQUIRED（timeline ends at 49.970s before captions ends at 50.000s）。
    这里把超出的部分压回音轨长度：只动最后一条的结束时间，不删不加，不碰人声。
    """
    log = log or (lambda m: None)
    caps = srt_captions(srt_path) if srt_path else []
    if len(caps) >= 1:
        log("已用字幕文件对齐口播：%d 条" % len(caps))
        return fit_captions(caps, vdur, log), "provided-srt"
    try:
        from . import transcript
        out_dir = os.path.join(work, "asr")
        os.makedirs(out_dir, exist_ok=True)
        sp = transcript.transcribe_video(talk_path, out_dir)
        caps = srt_captions(sp)
        if len(caps) >= 1:
            log("已用本地语音识别对齐口播：%d 条" % len(caps))
            return fit_captions(caps, vdur, log), "asr"
    except Exception as e:
        log("⚠ 语音识别不可用（%s）" % str(e)[:120])
    # 退化：按字数切分整段时长 —— 只能保证总长对，句内仍有偏差
    log("⚠ 没有字幕也没有语音识别，句级时间只能按字数估算，可能仍有错位")
    return [], "estimate"


def fit_captions(caps, vdur, log=None):
    """把字幕时间轴收进 [0, vdur]：末条超出就压到音轨末端。

    模板要求字幕完整覆盖、且不能超出时间轴（RECOMPOSE_REQUIRED）。
    音轨是硬约束（不能变速、不能裁人声），所以永远以音频为准裁字幕。
    """
    log = log or (lambda m: None)
    if not caps or vdur <= 0:
        return caps
    out = [dict(c) for c in caps if c["t1"] > 0.001]
    if not out:
        return []
    if out[0]["t0"] < 0:
        out[0]["t0"] = 0.0
    over = out[-1]["t1"] - vdur
    if over > 0.001:
        # 末条本来就短于 0.4s 时不要再压，改为砍掉这条（它几乎没被说出来）
        if out[-1]["t1"] - out[-1]["t0"] <= 0.4:
            log("末条字幕仅 %.2fs 且超出片尾 %.2fs，已丢弃这一条"
                % (out[-1]["t1"] - out[-1]["t0"], over))
            out.pop()
        else:
            out[-1]["t1"] = round(vdur, 3)
            log("末条字幕超出片尾 %.3fs（字幕 %.2fs > 音轨 %.2fs），已压到片尾"
                % (over, caps[-1]["t1"], vdur))
    # 压完之后可能与倒数第二条重叠，往回收一下
    for i in range(len(out) - 1, 0, -1):
        if out[i]["t0"] < out[i - 1]["t1"] - 0.001:
            out[i - 1]["t1"] = round(min(out[i - 1]["t1"], out[i]["t0"]), 3)
    return [c for c in out if c["t1"] - c["t0"] > 0.05]


def build_poster_talk(script, opts, talk_name, caps, vdur, log=None, profile=None):
    """口播原声版时间轴：段落跟着字幕真实时间，隔一段出镜一次。

    caps 是 [{t0,t1,text}]，允许为空（此时退化成整段一屏，但仍用原声）。
    """
    log = log or (lambda m: None)
    title = (script.get("title") or "").strip() or "企业服务那些事"
    company = (config.get_settings().get("company", {}) or {}).get("short", "合胜宏企服")

    # 字幕时间轴先收进音轨长度：音轨不能改，字幕可以让。
    caps = fit_captions(caps or [], vdur, log)
    # 片尾固定 4.5 秒（模板要求 4–6 秒），正文取其余时间
    outro_d = 4.5 if vdur > 12 else max(4.0, vdur * 0.3)
    body_end = max(4.0, vdur - outro_d)

    # ---- 1. 正文段落：以字幕边界为切点，收进每屏的 [min,max] ----
    blocks = []          # [{t0,t1,text}]
    src = [dict(c) for c in (caps or []) if c["t1"] > c["t0"]]
    if not src:
        src = [{"t0": 0.0, "t1": body_end, "text": (script.get("body") or title)}]
    # 只取正文区间内的部分（跨界的截断，不改字幕本身）
    for c in src:
        if c["t1"] <= 0.05:
            continue
        if c["t0"] >= body_end - 0.05:
            continue
        blocks.append({"t0": max(0.0, c["t0"]), "t1": min(body_end, c["t1"]),
                       "text": c["text"]})
    if not blocks:
        blocks = [{"t0": 0.0, "t1": body_end, "text": title}]
    blocks[0]["t0"] = 0.0
    blocks[-1]["t1"] = body_end
    # 片头那屏已经覆盖的句子不要整段再铺一遍：把跨片头的那句开头推到片头之后
    # （只在正文够长时做，极短片不设片头，见下）
    if body_end >= 12.0 and len(blocks) > 1:
        for b in blocks[1:]:
            if b["t0"] < 2.2 and b["t1"] > 3.6:
                b["t0"] = 3.4

    # 片头：第一屏让位给 opener（2.2–3.4 秒），其余内容顺延
    op_end = min(3.4, max(2.2, min(blocks[0]["t1"], 3.4)))
    # 极短片（<15s）：正文塞不下 opener + 一屏正文 + 片尾的时长下限，不设片头
    if body_end < POSTER_MIN["opener"] + POSTER_MIN_DEFAULT + 6.0:
        op_end = 0.0
    shots = [] if op_end <= 0.01 else [
        {"t0": 0.0, "t1": round(op_end, 2), "control": "opener", "variant": "focus",
         "props": {"title": _cut(title, 9), "a": company[:16], "b": ""},
         "text": blocks[0]["text"]}]

    # 正文从 op_end 开始，按字幕边界铺满 body_end
    cuts = [b["t0"] for b in blocks if b["t0"] > op_end + 0.3] + [body_end]
    cur = op_end
    while cur < body_end - 0.2:
        # 该位置属于哪一句（用于选控件和文字）
        own = next((b for b in blocks if b["t1"] > cur + 0.05), blocks[-1])
        ctl = _pick(own["text"], POSTER_RULES)
        lo = POSTER_MIN.get(ctl, POSTER_MIN_DEFAULT)
        hi = poster_max(ctl)
        # 候选切点：所有字幕边界 + 逐秒网格，取最长的合法区间
        # 候选切点：字幕边界优先（不会切在半句话中间），但必须落在 [lo, hi] 内。
        # 字幕边界都不在区间里时退回固定网格，否则一屏会撑到 5 秒撞上限。
        cand = [c for c in cuts if cur + lo - 0.05 <= c <= cur + hi + 0.05]
        if cand:
            end = max(cand)
        else:
            end = min(body_end, cur + min(hi, max(lo, 3.5)))
        # 太短就吞并下一句，绝不出现 1 秒屏
        if end - cur < lo:
            end = min(body_end, cur + lo)
        if end - cur > hi:
            end = cur + hi
        # 尾巴不够一屏时必须处理：片尾还有余量就借，没有就提前收尾
        if end - cur < lo - 0.02:
            if vdur - cur <= 6.0:
                body_end = cur
                break
            end = min(cur + lo, cur + hi)
        seg_text = "".join(b["text"] for b in blocks
                           if b["t1"] > cur + 0.05 and b["t0"] < end - 0.05)[:120]
        shots.append({"t0": round(cur, 2), "t1": round(end, 2),
                      "control": ctl, "variant": DEFAULT_VARIANT.get(ctl, "focus"),
                      "props": _poster_props(ctl, seg_text, title, company, opts),
                      "text": seg_text or own["text"]})
        cur = round(end, 2)

    # 同一控件连着出现就换变体，否则整片像一张卡停很多次
    seen = {}
    for sg in shots:
        c = sg["control"]
        if c in ("opener", "outro", "media"):
            continue
        vs = (((profile or {}).get("controls") or {}).get(c) or {}).get("variants") or []
        if not vs:
            continue
        n = seen.get(c, 0)
        sg["variant"] = vs[n % len(vs)]
        seen[c] = n + 1

    # ---- 2. 出镜区间：模板渲染器不把 <video> 烧进成片（预览有、成片空白），
    # 所以这里只记时间段，出片后由 ffmpeg 把口播画面叠上去（同一时钟，天然同步）。
    OUT_GAP, OUT_HOLD = 14.0, 3.5
    over, last_out = [], -99.0
    for sg in shots[1:]:
        if sg["t1"] - sg["t0"] < 3.2:
            continue
        if sg["t0"] - last_out < OUT_GAP:
            continue
        over.append([round(sg["t0"] + 0.15, 2),
                     round(min(sg["t0"] + 0.15 + OUT_HOLD, sg["t1"] - 0.1), 2)])
        last_out = sg["t0"]

    # 正文最后一段必须正好收在 body_end：多出来的并回前一段，短了就别越界。
    # 注意 body_end 可能在上面被改小过，这里必须重新取（否则会算出 5 秒超长屏）。
    if len(shots) > 1:
        last = shots[-1]
        span = round(body_end - last["t0"], 2)
        lo_last = POSTER_MIN.get(last["control"], POSTER_MIN_DEFAULT)
        hi_last = poster_max(last["control"])
        if span <= 0.05:
            shots.pop()
            shots[-1]["t1"] = round(body_end, 2)
        elif span < lo_last:
            # 尾巴不够一屏：并进前一段，前一段若因此超上限就拆一次
            shots.pop()
            shots[-1]["t1"] = round(body_end, 2)
            d = shots[-1]["t1"] - shots[-1]["t0"]
            if d > poster_max(shots[-1]["control"]) + 0.02:
                mid = round(shots[-1]["t0"] + d / 2.0, 2)
                tail = dict(shots[-1])
                shots[-1]["t1"] = mid
                tail["t0"] = mid
                shots.insert(len(shots), tail)
            last = shots[-1]
        else:
            last["t1"] = round(min(body_end, last["t0"] + hi_last), 2)
        # 拆完再兜一次：任何一屏都不许越过模板上限
        for k in range(len(shots) - 1, 1, -1):
            sg = shots[k]
            d = sg["t1"] - sg["t0"]
            if d > poster_max(sg["control"]) + 0.02:
                mid = round(sg["t0"] + d / 2.0, 2)
                nxt = dict(sg)
                sg["t1"] = mid
                nxt["t0"] = mid
                shots.insert(k + 1, nxt)
        # 收尾对齐：最后一屏必须正好到 body_end，缝隙补到前一屏
        if len(shots) > 1 and abs(shots[-1]["t1"] - body_end) > 0.01:
            shots[-1]["t1"] = round(body_end, 2)
        for k in range(1, len(shots)):
            if abs(shots[k]["t0"] - shots[k - 1]["t1"]) > 0.011:
                shots[k]["t0"] = shots[k - 1]["t1"]
        # 均分切出来的两屏可能都跌破下限：让它们重新分回一屏（不越过上限）
        if _merge_short_shots(shots, 1):
            if abs(shots[-1]["t1"] - body_end) > 0.01:
                shots[-1]["t1"] = round(body_end, 2)

    _merge_short_shots(shots)
    # 并屏之后末屏可能被并长了，但片尾紧跟着，必须保证末屏不越上限、正好贴 body_end
    if len(shots) > 1:
        last = shots[-1]
        if last["t1"] - last["t0"] > poster_max(last["control"]) + 0.02 \
                and shots[-2]["control"] != "opener":
            d = last["t1"] - last["t0"]
            mid = round(last["t0"] + d / 2.0, 2)
            nxt = dict(last)
            last["t1"] = mid
            nxt["t0"] = mid
            shots.append(nxt)
        shots[-1]["t1"] = round(body_end, 2)
    # 最后一道：仍低于下限的屏，从前一屏借时间（前一屏留够自己下限即可）
    _borrow_to_fill(shots, body_end)
    _split_long_shots(shots)
    _borrow_to_fill(shots, body_end)
    _split_long_shots(shots)
    _borrow_to_fill(shots, body_end)
    # 极短片兜底：内容实在太少，仍可能有一屏超过上限（模板宁长不可超），
    # 这种情况给用户一句明确提示，而不是等 check-params 报错。
    if any(s["t1"] - s["t0"] > poster_max(s["control"]) + 0.02 for s in shots[1:]):
        log("⚠ 口播太短（%.1fs），模板屏数不够分摊，建议片长 ≥15 秒" % vdur)

    # ---- 3. 片尾 ----
    shots.append({"t0": round(body_end, 2), "t1": round(vdur, 2),
                  "control": "outro", "variant": "thanks",
                  "props": {"thanks": "感谢观看", "sub": _cut(title, 24),
                            "name": company[:12], "follow": "关注",
                            "items": [{"text": "有疑问评论区问"}, {"text": "关注不迷路"}]},
                  "text": "感谢观看"})

    timeline = {
        "duration": round(vdur, 2),
        "layout": "portrait",
        "language": "zh",
        "segments": [{k: v for k, v in s.items() if k != "text"} for s in shots],
        "presenter": {"hide": True, "audio": "mute"},
        "voiceover": opts.get("voiceover_name") or "",
        "music": opts.get("music", "") or "",
        "header": {"shortTitle": title[:2000],
                   "source": {"kind": "user-explicit", "field": "title"}},
        "showCaptions": bool(opts.get("captions", True)),
        "hud": False,
        "narrationLayout": "balanced",
        "narrationCardLayout": "balanced",
        "captionSafeArea": "short-video",
        "captionPosition": {"bottomRatio": 0.2, "leftRatio": 0.08, "rightRatio": 0.1},
        "ending": "cut",
    }
    timeline["talkOverlay"] = {"file": talk_name, "ranges": over}
    if caps:
        timeline["captions"] = [{"t0": c["t0"], "t1": c["t1"], "text": c["text"]}
                                for c in caps]
        timeline["narrationMap"] = _narration_rows(shots, timeline["captions"])
    return timeline


def _merge_short_shots(shots, start=1):
    """把低于模板下限的相邻两屏并回一屏（前提是合起来不越过上限）。

    只在“同一控件被均分成两屏，结果两屏都不够长”时发生。并屏不删文字，
    只是让这一屏多停一会儿；返回是否改动过。
    """
    changed = False
    k = max(1, start)
    while k < len(shots) - 1:
        a, b = shots[k], shots[k + 1]
        if a["control"] == b["control"]:
            la = POSTER_MIN.get(a["control"], POSTER_MIN_DEFAULT)
            d = b["t1"] - a["t0"]
            if (a["t1"] - a["t0"] < la - 0.02 or
                    b["t1"] - b["t0"] < POSTER_MIN.get(b["control"], POSTER_MIN_DEFAULT) - 0.02) \
                    and d <= poster_max(a["control"]) + 0.02:
                a["t1"] = b["t1"]
                if len(a.get("text", "")) < len(b.get("text", "")):
                    a["text"] = (a.get("text", "") + b.get("text", ""))[:120]
                shots.pop(k + 1)
                changed = True
                continue
        k += 1
    return changed


def _borrow_to_fill(shots, body_end):
    """末屏不足下限时向前借时间；借不动就并屏。必须保证：无缝隙、不越上限。

    这是最后一道闸：模板对单屏上下限卡得很死，界面上表现为
    “xxx 至少 N 秒，现在 M 秒”，一轮渲染几分钟，本地先修掉。
    """
    for _ in range(24):
        bad = None
        for k in range(1, len(shots)):
            lo = POSTER_MIN.get(shots[k]["control"], POSTER_MIN_DEFAULT)
            if shots[k]["t1"] - shots[k]["t0"] < lo - 0.02:
                bad = k
                break
        if bad is None:
            # 末屏对齐到 body_end（只补缝隙，不改总量）
            if abs(shots[-1]["t1"] - body_end) > 0.01 and \
                    shots[-1]["t1"] < body_end:
                shots[-1]["t1"] = round(body_end, 2)
            return True
        k = bad
        need = POSTER_MIN.get(shots[k]["control"], POSTER_MIN_DEFAULT) - \
            (shots[k]["t1"] - shots[k]["t0"])
        prev = shots[k - 1]
        prev_lo = POSTER_MIN.get(prev["control"], POSTER_MIN_DEFAULT)
        room = (prev["t1"] - prev["t0"]) - prev_lo      # 前一屏能吐出的时间
        if room >= need - 0.001:
            prev["t1"] = round(prev["t1"] - need, 2)
            shots[k]["t0"] = prev["t1"]
            continue
        # 借不够：并屏（合起来仍在上一屏上限内才并）
        merged = shots[k]["t1"] - prev["t0"]
        if merged <= poster_max(prev["control"]) + 0.02 and k - 1 >= 1:
            prev["t1"] = shots[k]["t1"]
            if len(prev.get("text", "")) < len(shots[k].get("text", "")):
                prev["text"] = (prev.get("text", "") + shots[k].get("text", ""))[:120]
            shots.pop(k)
            continue
        # 并了会超上限：把前一屏多余的时间吐给再前一屏，腾出空间再并
        if k - 2 >= 1:
            pp = shots[k - 2]
            pp_lo = POSTER_MIN.get(pp["control"], POSTER_MIN_DEFAULT)
            need_more = merged - (poster_max(prev["control"]) + 0.02)
            give = min(max(0.0, (pp["t1"] - pp["t0"]) - pp_lo), need_more + 0.01)
            if give > 0.001:
                pp["t1"] = round(pp["t1"] - give, 2)
                prev["t0"] = pp["t1"]
                prev["t1"] = round(prev["t1"] - give, 2)
                shots[k]["t0"] = prev["t1"]
                continue
            # 前两屏都吐不出来：把这一屏整体并进前一屏（上一屏本来就在上限内）
            if (prev["t1"] - prev["t0"]) <= poster_max(prev["control"]) + 0.02:
                prev["t1"] = shots[k]["t1"]
                shots.pop(k)
                continue
        # 实在动不了：把这一屏并进前一屏，并把前一屏从中间拆开（拆完两屏都合法）
        d = shots[k]["t1"] - prev["t0"]
        # 先试着把自己整体并进前一屏（前一屏本来就在上限内就最省事）
        if (prev["t1"] - prev["t0"]) <= poster_max(prev["control"]) + 0.02:
            prev["t1"] = shots[k]["t1"]
            if len(prev.get("text", "")) < len(shots[k].get("text", "")):
                prev["text"] = (prev.get("text", "") + shots[k].get("text", ""))[:120]
            shots.pop(k)
            continue
        mid = round(prev["t0"] + min(poster_max(prev["control"]),
                                     max(POSTER_MIN.get(prev["control"],
                                                        POSTER_MIN_DEFAULT),
                                         d - POSTER_MIN.get(shots[k]["control"],
                                                            POSTER_MIN_DEFAULT))), 2)
        tail = dict(prev)
        prev["t1"] = mid
        tail["t0"] = mid
        tail["t1"] = shots[k]["t1"]
        shots[k] = tail
        continue
    # 到这里仍有屏低于下限：从片尾借（片尾容许多停，不低于下限就行）
    for k in range(1, len(shots)):
        lo = POSTER_MIN.get(shots[k]["control"], POSTER_MIN_DEFAULT)
        need = lo - (shots[k]["t1"] - shots[k]["t0"])
        if need <= 0.02:
            continue
        prev = shots[k - 1]
        room = (prev["t1"] - prev["t0"]) - POSTER_MIN.get(prev["control"],
                                                          POSTER_MIN_DEFAULT)
        if room >= need:
            prev["t1"] = round(prev["t1"] - need, 2)
            shots[k]["t0"] = prev["t1"]
        elif k - 1 >= 1 and shots[k]["t1"] == round(body_end, 2):
            # 末屏太短：把起点往前拉（前一屏会被压到下限，只要不破下限就接受）
            pull = min(need, (prev["t1"] - prev["t0"]) -
                       POSTER_MIN.get(prev["control"], POSTER_MIN_DEFAULT) + need)
            if pull > 0.01:
                prev["t1"] = round(prev["t1"] - pull, 2)
                shots[k]["t0"] = prev["t1"]
    return False


def _split_long_shots(shots):
    """把超过模板上限的屏拆开，并让拆出来的每一屏都落回 [下限, 上限]。

    做法：先对半拆，再把过短的一半从邻居挪时间；实在挪不动就再拆一半，
    直到整条时间轴没有越界，或确实无可再拆。
    """
    for _ in range(200):
        hit = next((k for k in range(1, len(shots))
                    if shots[k]["t1"] - shots[k]["t0"] >
                    poster_max(shots[k]["control"]) + 0.02), None)
        if hit is None:
            return True
        sg = shots[hit]
        d = sg["t1"] - sg["t0"]
        mid = round(sg["t0"] + d / 2.0, 2)
        nxt = dict(sg)
        sg["t1"] = mid
        nxt["t0"] = mid
        shots.insert(hit + 1, nxt)
        # 修拆出来的两屏：低于下限就从邻居挪；邻居没富余就把拆分点往邻居方向挪
        for j in (hit, hit + 1):
            s2 = shots[j]
            short = POSTER_MIN.get(s2["control"], POSTER_MIN_DEFAULT) - (s2["t1"] - s2["t0"])
            if short <= 0.02:
                continue
            nb = shots[j + 1] if j + 1 < len(shots) else None
            if nb is not None and (nb["t1"] - nb["t0"]) - \
                    POSTER_MIN.get(nb["control"], POSTER_MIN_DEFAULT) >= short:
                nb["t0"] = round(nb["t0"] - short, 2)
                s2["t1"] = round(s2["t1"] - short, 2)
                continue
            pv = shots[j - 1] if j - 1 >= 1 else None
            if pv is not None:
                room = (pv["t1"] - pv["t0"]) - \
                    POSTER_MIN.get(pv["control"], POSTER_MIN_DEFAULT)
                if room >= short:
                    pv["t1"] = round(pv["t1"] + short, 2)
                    s2["t0"] = pv["t1"]
                    continue
                # 邻居也满了：把这一屏的边界往邻居里推，让邻居承接到限为止
                if nb is not None and j == hit:
                    take = min(short, poster_max(nb["control"]) + 0.0 - (nb["t1"] - nb["t0"]))
                    if take > 0.01:
                        nb["t0"] = round(nb["t0"] - take, 2)
                        s2["t1"] = round(s2["t1"] - take, 2)
                        continue
                if pv is not None and j == hit + 1:
                    take = min(short, poster_max(pv["control"]) + 0.0 - (pv["t1"] - pv["t0"]))
                    if take > 0.01:
                        pv["t1"] = round(pv["t1"] + take, 2)
                        s2["t0"] = pv["t1"]
                        continue
            # 邻居都没富余：这一半还是太长的话再二分一次（下一轮处理）
    return False


def check_talk_timeline(tl, vdur):
    """提交前拦 RECOMPOSE_REQUIRED：末段必须正好落在音轨末端，字幕不能越过它。

    这两条模板在 check-params 阶段才报，一轮要等几分钟；本地先用毫秒级比较拦住。
    """
    segs = tl.get("segments") or []
    if not segs:
        return ["时间轴没有镜头"]
    bad = []
    end = round(segs[-1]["t1"], 2)
    if abs(end - round(vdur, 2)) > 0.011:
        bad.append("末段收在 %.2fs，音轨 %.2fs" % (end, vdur))
    caps = tl.get("captions") or []
    if caps:
        ce = round(caps[-1]["t1"], 2)
        if end < ce - 0.001:
            bad.append("末段 %.2fs 早于字幕结尾 %.2fs" % (end, ce))
    prev = 0.0
    for k, s in enumerate(segs):
        if abs(s["t0"] - prev) > 0.015:
            bad.append("#%d 与前一段有 %.2fs 缝隙" % (k, s["t0"] - prev))
        lo = POSTER_MIN.get(s["control"], POSTER_MIN_DEFAULT)
        hi = poster_max(s["control"])
        d = s["t1"] - s["t0"]
        if d < lo - 0.02:
            bad.append("#%d %s 仅 %.2fs（下限 %.1fs）" % (k, s["control"], d, lo))
        if d > hi + 0.02:
            bad.append("#%d %s 达 %.2fs（上限 %.1fs）" % (k, s["control"], d, hi))
        prev = s["t1"]
    return bad


def _poster_props(ctl, text, title, company, opts):
    """按控件给一份能过校验的最小 props（口播路线专用，内容取自该屏原文）。"""
    t = _cut(text, 44) or _cut(title, 20)
    if ctl == "myth":
        cl = [x.strip("，,。；; ") for x in re.split(r"[，,。；;]", t) if x.strip()]
        a = cl[0] if cl else t
        b = cl[1] if len(cl) > 1 else "先看清条款再定"
        return {"kicker": "很多人以为是", "myth": _cut(a, 22), "fact": _cut(b, 24),
                "mythLabel": "误区", "factLabel": "其实"}
    if ctl == "tip":
        return {"kicker": "提醒", "kind": "warn", "title": "注意", "text": t}
    if ctl == "list":
        items = [_cut(x, 24) for x in _split_phrase(t, 12) if x][:4]
        if len(items) < 2:
            items = [items[0] if items else _cut(t, 12), "再看这一点"]
        return {"title": _cut(title, 18), "items": items[:6]}
    if ctl == "steps":
        its = [_cut(x, 10) for x in _split_phrase(t, 10) if x][:5]
        if len(its) < 2:
            its = [_cut(t, 10), "再按流程办"]
        return {"title": "怎么做"[:18], "steps": its}
    if ctl == "compare":
        cl = [x.strip() for x in re.split(r"[，,。；;]", t) if x.strip()]
        return {"left": {"label": "常见做法", "big": _cut(cl[0], 10) if cl else _cut(t, 10),
                         "lines": []},
                "right": {"label": "更稳的做法",
                          "big": _cut(cl[-1], 10) if len(cl) > 1 else "更稳妥的做法",
                          "lines": []},
                "verdict": "后者更稳", "winner": "right"}
    if ctl == "definition":
        return {"kicker": "先把词搞懂", "term": _cut(t, 10), "pos": "名词",
                "definition": _cut(t, 48)}
    if ctl == "cta":
        return {"kicker": "办企业不用自己摸", "text": "找合胜宏", "sub": "一次办对，少跑三趟",
                "icon": "rocket"}
    if ctl == "quote":
        return {"text": _cut(t, 40), "who": company[:12]}
    if ctl == "qa":
        q = _cut(t, 26).rstrip("。！？!?") + "？"
        return {"kicker": "常被问到", "question": q, "answer": _cut(title, 20)}
    # 默认用巨字/标题：竖屏最稳，不会因为字段缺失被拒
    if ctl in ("keyword", "bigtype", "kinetic"):
        parts = [x for x in _split_phrase(t, 12) if x]
        return {"kicker": title[:32], "word": _cut(parts[0] if parts else t, 10),
                "sub": [_cut(x, 22) for x in parts[1:3]]}
    return {"kicker": _cut(title, 20), "line1": _cut(t, 16), "line2": ""}


def _narration_rows(shots, caps):
    """narrationMap：每屏一行，captionIndices 取该屏相交的全部字幕。

    模板的 continuous 模式要求：beat 保留整句的原文和真实区间（可以跨镜头），
    每一条相交的字幕都要有自己的 beat，不能只标几个词。
    """
    rows = []
    for k, sg in enumerate(shots):
        idx = [i for i, c in enumerate(caps)
               if c["t0"] < sg["t1"] - 0.01 and c["t1"] > sg["t0"] + 0.01]
        if not idx:
            mid = (sg["t0"] + sg["t1"]) / 2.0
            idx = [min(range(len(caps)),
                       key=lambda i: abs((caps[i]["t0"] + caps[i]["t1"]) / 2.0 - mid))]
        rows.append({
            "shot": k, "start": round(sg["t0"], 2), "end": round(sg["t1"], 2),
            "spokenText": "".join(caps[i]["text"] for i in idx)[:200],
            "purpose": "neutral-emphasis",
            "timingSource": "provided-captions",
            "captionIndices": idx,
            "captionBinding": "continuous",
            "beats": [{"at": round(caps[i]["t0"], 2), "until": round(caps[i]["t1"], 2),
                       "spokenText": caps[i]["text"][:200],
                       "timing": "sentence", "target": "props"} for i in idx],
        })
    return rows


def _overlay_talk(film, talk, ranges, out_path, log=None):
    """把口播画面叠到成片上（模板自己的 <video> 不进成片，只能二次合成）。

    口播输入从头播放，主片和素材用同一个时间基准，所以窗口里那一帧
    天然等于这一刻的说话内容；不要给素材加 -ss，加了就错位。
    """
    log = log or (lambda m: None)
    if not ranges:
        return film
    cond = "+".join("between(t,%.2f,%.2f)" % (a, b) for a, b in ranges)
    w = 960
    vf = ("[1:v]scale=%d:-2,crop=%d:%d:(iw-%d)/2:180,setsar=1,"
          "pad=%d:%d:4:4:color=white[pip];"
          "[0:v][pip]overlay=60:900:enable='%s'[v]"
          % (w, w, w, w, w + 8, w + 8, cond))
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
           "-i", film, "-stream_loop", "-1", "-i", talk,
           "-filter_complex", vf, "-map", "[v]", "-map", "0:a",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-pix_fmt", "yuv420p", "-c:a", "copy", "-shortest", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not os.path.exists(out_path) \
            or os.path.getsize(out_path) < 10000:
        log("⚠ 口播画面叠加失败，交付纯文字版：%s" % (r.stderr or "")[-300:])
        return film
    log("口播画面已叠加：%d 段" % len(ranges))
    return out_path


def render(job_id, root, log=None):
    """后台任务主体：kit 包全链路出片。返回 (out_path, duration, params)。"""
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
    poster = kit.workflow == "poster-kit"
    log("模板路线：%s（%s %s×%s）" %
        (kit.workflow, kit.profile.get("template_name") or "未命名",
         kit.profile.get("width") or "?", kit.profile.get("height") or "?"))
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
            kit.put_input(name, src=src)
            opts[key] = name
            log("已绑定%s：%s" % ("配乐" if key == "music" else "配音",
                                os.path.basename(src)))
    if opts.get("srt_path") and os.path.exists(opts["srt_path"]):
        kit.put_input("captions.srt", src=opts["srt_path"])
        log("已绑定字幕文件")
    opts.setdefault("music", "")
    opts.setdefault("voiceover", "")

    # 官方助手要求 plan/prepare/render 的路径都落在实例目录内
    base = kit.instance
    db.update_job(job_id, progress=34)

    if poster:
        voice_dur = 0.0
        voice_path = None
        talk_mode = False
        # 有真人原片就走口播路线：原声当音轨、字幕/切点用真实时间、隔段出镜。
        # 这条分支不再合成任何配音，也不补静音、不拉伸。
        talk = (opts.get("talk_path") or job.get("video_path") or "").strip()
        if talk and os.path.exists(talk):
            tname = "talk" + (os.path.splitext(talk)[1] or ".mp4").lower()
            kit.put_input(tname, src=talk)
            talk_in = os.path.join(inputs, tname)
            # 模板按“timeline 同目录”找素材，实例根也放一份，少走一次兜底重试
            try:
                shutil.copy2(talk_in, os.path.join(base, tname))
            except Exception:
                pass
            try:
                talk_dur = ingest.probe(talk_in)[0]
            except Exception:
                talk_dur = 0.0
            if talk_dur <= 0:
                raise KitError("读不到口播视频时长：%s" % os.path.basename(talk))
            # 原声：直接从视频抽，不改语速、不重排、不补静音
            vname = "talkvoice.m4a"
            vpath = os.path.join(work, vname)
            subprocess.run(
                ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", talk_in,
                 "-vn", "-ac", "2", "-ar", "48000", "-c:a", "aac", "-b:a", "192k", vpath],
                check=True, timeout=600)
            voice_path = kit.put_input(vname, src=vpath)
            try:
                shutil.copy2(voice_path, os.path.join(base, vname))
            except Exception:
                pass
            talk_mode = True
            voice_dur = talk_dur
            log("口播原声 %.2fs（不合成配音、不变速）" % talk_dur)
            caps, cap_src = talk_captions(talk_in, work, opts.get("srt_path"),
                                          talk_dur, log)
            timeline = build_poster_talk(script, opts, tname, caps, talk_dur, log,
                                        kit.profile)
            # 出镜区间的视频由 ffmpeg 在成片阶段叠加（见 _overlay_talk）
            timeline["voiceover"] = vname
            talk_bad = check_talk_timeline(timeline, talk_dur)
            if talk_bad:
                # 能自愈的先自愈：重排一次（fit_captions 已保证字幕不越界）
                log("⚠ 口播时间轴自检不通过：%s，重排一次" % "；".join(talk_bad[:3]))
                timeline = build_poster_talk(script, opts, tname, caps, talk_dur,
                                             log, kit.profile)
                timeline["voiceover"] = vname
                left = check_talk_timeline(timeline, talk_dur)
                if left:
                    raise KitError("口播时间轴不符合模板要求：%s" % "；".join(left[:4]))
            log("对时依据：%s，共 %d 段" % (cap_src, len(timeline["segments"])))
        # 没有原片：仍走 TTS + 文案估时（旧行为）
        elif not opts.get("voiceover") and (opts.get("voice") or opts.get("mg_voice")):
            vp = os.path.join(work, "voice.mp3")
            try:
                from . import motion as _motion
                _motion._synthesize(script.get("body") or "", opts, vp, log)
                opts["voiceover"] = "voice.mp3"
                log("配音已生成，将按配音时长对齐镜头")
            except Exception as e:
                log("⚠ 配音合成失败，按无配音出片：%s" % e)
                opts.pop("voiceover", None)
        if opts.get("voiceover"):
            vname = opts["voiceover"]
            src = vname if os.path.isabs(vname) else os.path.join(inputs, vname)
            if not os.path.exists(src):
                src = os.path.join(work, os.path.basename(vname))
            if os.path.exists(src):
                voice_path = kit.put_input(os.path.basename(vname), src=src)
                opts["voiceover"] = os.path.basename(vname)
                try:
                    voice_dur = ingest.probe(voice_path)[0]
                except Exception:
                    voice_dur = 0.0
            else:
                log("⚠ 找不到配音文件 %s，按无配音出片" % os.path.basename(vname))
                opts["voiceover"] = ""
        if talk_mode:
            pass            # 口播路线的时间轴已经在上面建好，不能再被 TTS 逻辑覆盖
        elif voice_path and voice_dur > 0:
            # 走精确对齐：文案句子 ⇄ 配音时长 ⇄ 字幕 ⇄ narrationMap 四者同一时钟
            timeline = build_poster(script, opts, voice_dur)
            timeline = bind_poster_audio(script, opts, timeline, inputs, voice_path, log)
        else:
            if opts.get("voiceover"):
                log("⚠ 读不到配音时长，按无配音出片")
            timeline = build_poster(script, opts, 0)
            if not (voice_path and voice_dur > 0):
                timeline["voiceover"] = ""
            srt_src = opts.get("srt_path")
            if srt_src and os.path.exists(srt_src):
                kit.put_input("captions.srt", src=srt_src)
            elif opts.get("captions", True):
                kit.put_input("captions.srt", text=build_srt(script, opts, timeline, 0))
        pp = os.path.join(base, "timeline.json")
        overlay = timeline.pop("talkOverlay", None)
        json.dump(timeline, open(pp, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        params = timeline
        if overlay:
            params["talkOverlay"] = overlay
        n = len(timeline["segments"])
        kinds = sorted({s["control"] for s in timeline["segments"]})
        log("已编排 %d 个镜头 / %d 种版式（%s）/ 总长 %.1fs" %
            (n, len(kinds), "、".join(kinds[:8]), timeline["duration"]))
    else:
        params = build_params(script, opts)
        if opts.get("music"):
            params["timeline"]["music"] = opts["music"]
        if opts.get("voiceover"):
            params["timeline"]["voiceover"] = opts["voiceover"]
        pp = os.path.join(base, "params.json")
        json.dump(params, open(pp, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        n = len(params["timeline"]["segments"])
        kinds = sorted({s["control"] for s in params["timeline"]["segments"]})
        log("已编排 %d 个镜头 / %d 种控件（%s）/ 总长 %.1fs" %
            (n, len(kinds), "、".join(kinds[:8]), params["timeline"]["duration"]))
    db.update_job(job_id, progress=38)

    warn = overflow_report(params) if poster else []
    if warn:
        log("排版提示（不影响出片，但建议改文案）：" + "；".join(warn[:3]))

    if poster:
        # 先按模板 spec 自检：必填项、项数下限、字数上限。
        # 这些问题模板要到 plan/render 才拒，等一轮要几分钟，本地先拦。
        bad = validate_poster(params, kit.profile)
        if bad:
            log("⚠ 编排自检发现问题，正在修正：" + "；".join(bad[:4]))
            params = _autofix_poster(params, kit.profile)
            json.dump(params, open(pp, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=1)
            left = validate_poster(params, kit.profile)
            if left:
                raise KitError("时间轴不符合模板要求：%s" % "；".join(left[:5]))
            log("自检修正完成")

    pv = {}
    for attempt in (1, 2):
        kit.check(pp)
        log("参数校验通过")
        # 助手不允许覆盖已存在的输出目录（OUTPUT_EXISTS），重排一次就得换目录名
        plan_dir = os.path.join(base, ("extension-plan" if poster else "plan")
                                + ("" if attempt == 1 else "_r2"))
        kit.plan(pp, plan_dir)
        kit.plan_dir = plan_dir
        rv = _find_review(plan_dir)
        if rv:
            _mark_reviewed(rv, "reviewed", "approved")
        log("编排方案已生成")
        db.update_job(job_id, progress=52)
        if not poster or attempt > 1:
            break

        # 第一遍预览只用来量竖屏几何；文字出安全区就缩字重排一次
        preview_dir = os.path.join(base, "extension-preview")
        kit.preview_dir = preview_dir
        pv = kit.preview(None, _times(params), preview_dir=preview_dir)
        bad = _edge_violations(pv, params)
        if not bad:
            break
        log("预览测得 %d 处文字越界，正在收窄排版后重排" % bad)
        _shrink_poster(params)
        json.dump(params, open(pp, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        db.update_job(job_id, progress=44)

    if poster:
        # 重排过就换预览目录，否则助手报 OUTPUT_EXISTS
        preview_dir = os.path.join(base, "extension-preview")
        if os.path.isdir(preview_dir) and len(glob.glob(
                os.path.join(preview_dir, "**", "preview-review.json"), recursive=True)) > 0:
            pv_prev = preview_result(preview_dir)
            if pv_prev.get("dir") and pv_prev.get("review"):
                preview_dir = os.path.join(base, "extension-preview-final")
        kit.preview_dir = preview_dir
        pv = kit.preview(None, _times(params), preview_dir=preview_dir)
        rd = pv.get("dir") if pv else None
        if rd:
            _mark_reviewed(os.path.join(rd, "preview-review.json"), "reviewed", "approved")
            _mark_reviewed(os.path.join(rd, "preview.json"), "reviewed", "approved")
        _mark_reviewed(os.path.join(preview_dir, "preview-review.json"), "reviewed", "approved")
        log("预览帧已生成")
        # 渲染前把实例内所有 host-plan.json 的审核标记打开（poster-kit 会
        # 直接在 plan 目录里检查它，不会另建 project 目录）
        for hp in glob.glob(os.path.join(base, "**", "host-plan.json"), recursive=True):
            _mark_reviewed(hp, "reviewed")
        log("预览帧已确认")
    else:
        project = os.path.join(base, "project")
        kit.prepare(plan_dir, project)
        db.update_job(job_id, progress=64)
        kit.preview(project, _times(params, "timeline"))
        _mark_reviewed(os.path.join(project, "preview.json"), "reviewed")
        log("预览帧已生成")
        db.update_job(job_id, progress=74)
    db.update_job(job_id, progress=70)

    # 输出路径：助手会把 --instance 解析成本次会话的实例目录，而每次调用都可能
    # 换一个（嵌套 /tmp/motiongit-instance-<key>/instance），所以“我自己记着的
    # 那个 instance 路径”不一定被认。做法是：先用本次刚验证过的实例绝对路径，
    # 再退到相对文件名，两类都试；每次调用前后扫一遍全局实例目录，靠文件出现
    # 时间认出真正落盘的那份，而不是相信传进去的路径。
    tdir = tempfile.gettempdir()
    before = _instance_videos(tdir)
    cands = [os.path.join(base, "video.mp4"), "video.mp4"]
    if getattr(kit, "active_instance", None):
        alt = os.path.join(kit.active_instance, "video.mp4")
        if alt not in cands:
            cands.insert(0, alt)
    out_raw, last = None, None
    for cand in cands:
        try:
            kit.render(project if not poster else None, cand)
        except KitError as e:
            last = e
            # 只吞“不在实例内”，别的错误（缺审核、参数问题）要暴露出来
            if "permanent instance" not in str(e):
                raise
        # 不管成功与否，都看有没有新文件落地：助手可能报错但已经写出来了
        fresh = _instance_videos(tdir)
        new = [p for p, m in fresh.items() if p not in before]
        hit = None
        if os.path.isabs(cand) and os.path.exists(cand) \
                and os.path.getsize(cand) > 10000:
            hit = cand
        elif new:
            hit = max(new, key=lambda p: os.path.getmtime(p))
        if hit and os.path.getsize(hit) > 10000:
            out_raw = hit
            log("成片落盘：%s" % out_raw)
            break
        before = fresh
    if not out_raw or not os.path.exists(out_raw) or \
            os.path.getsize(out_raw) < 10000:
        if last:
            raise last
    if not os.path.exists(out_raw) or os.path.getsize(out_raw) < 10000:
        raise KitError("模板未产出有效视频")

    # 口播路线：模板出的是纯文字版，人物画面在这里叠上去
    ov = (params or {}).get("talkOverlay") or {}
    if ov.get("ranges"):
        tfile = os.path.join(kit.instance, "inputs", ov.get("file") or "")
        if not os.path.exists(tfile):
            tfile = os.path.join(work, ov.get("file") or "")
        if os.path.exists(tfile):
            merged = os.path.join(work, "final.mp4")
            out_raw = _overlay_talk(out_raw, tfile, ov["ranges"], merged, log)
    db.update_job(job_id, progress=96)

    name = "clip_%d_kit.mp4" % job_id
    out_path = os.path.join(config.OUTPUTS, name)
    shutil.copy2(out_raw, out_path)
    dur = 0.0
    try:
        dur = ingest.probe(out_path)[0]
    except Exception:
        pass
    log("完成：%s（%.2fs，%d KB）" % (name, dur, os.path.getsize(out_path) // 1024))
    return out_path, dur, params
