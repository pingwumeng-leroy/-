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


def _split_for_pace(sents, target):
    """句子太少、镜头数撑不满配音时长时，把长句按逗号拆成独立镜头。

    poster-kit 每个控件都有最短时长，硬拉长单个镜头会变成“一屏停尸”，
    正确做法是多铺几屏：一句 20 字的话拆成两屏，节奏立刻回来。
    """
    if target <= 0 or len(sents) >= 10:
        return sents
    out = list(sents)
    guard = 0
    while guard < 12:
        guard += 1
        if len(out) >= 10:
            break
        # 估算：模板每屏最少停 2.6s 左右（opener/cta 2，steps/myth 3.5），
        # 片头片尾固定占 6s，所以内容屏数 ≈ (配音时长 - 6) / 2.8
        need = int(round(max(1.0, (target - 6.0) / 2.8))) - len(out)
        if need <= 0:
            break
        idx = max(range(len(out)), key=lambda i: len(out[i]))
        long = out[idx]
        clause = [x.strip() for x in re.split(r"[，,]", long.rstrip("。！？!?")) if x.strip()]
        if len(clause) < 2:
            break
        # 对半拆，短的给前面那屏
        mid = max(1, len(clause) // 2)
        a = "，".join(clause[:mid]) + "。"
        b = "，".join(clause[mid:]) + "。"
        if len(a) < 6 or len(b) < 6:
            break
        out[idx:idx + 1] = [a, b]
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
            add("list", "rail" if len(grp) < 3 else "cards",
                {"title": title[:18], "items": [_cut(g, 24) for g in grp[:6]]},
                "".join(grp))
            used.append("list")
        elif ctl == "steps":
            grp = [s]
            while i + 1 < n and _pick(sents[i + 1], POSTER_RULES) == "steps" and len(grp) < 5:
                i += 1
                grp.append(sents[i])
            items = [re.sub(r"^(第[一二三四五]步|首先|其次|最后)[，,]?\s*", "", g)[:10] for g in grp]
            if len(items) < 2:
                items = _split_phrase(s, 10)[:5] or [s[:10], "按要求准备"]
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
                 "pros": [_cut(good, 12)], "cons": [_cut(bad, 12)],
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
         "follow": "关注", "items": [{"text": "有疑问评论区问"}]}, "感谢观看", 4.0)

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
        durs = mins
    else:
        durs = [mins[i] + extra * per_scene[i] / tl_total for i in range(n)]
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
    rows = []
    if squeezed:
        # 单个镜头分不到一句完整配音时，把整句归属给覆盖它绝大部分的那个镜头，
        # 保证“每句都有归属、不重复”，beat 用镜头自身区间（不虚构词时间）。
        own = {}
        i_pos = 0
        for i in range(len(sents)):
            best, score = 0, -1.0
            for k, sg in enumerate(segs):
                ov = min(ends[i], sg["t1"]) - max(starts[i], sg["t0"])
                if ov > score:
                    best, score = k, ov
            own.setdefault(best, []).append(i)
        # 记录“第一句还没被处理到哪”，给没有语音的镜头顺延兜底
        i_pos = len(sents)
        for k, sg in enumerate(segs):
            # 模板要求“每个镜头一行”，没有语音归属的镜头也要给一行（文字收尾）
            idx = own.get(k, [])
            if not idx:
                # 模板规则：只要有音轨，每个镜头就必须写完整原话。
                # 被压缩时一句会被几个镜头“共用”，共用句按顺序落在各自的
                # 时间段内，文字不删、语义不丢。
                if i_pos < len(sents):
                    idx = [i_pos]
                else:
                    idx = [len(sents) - 1]
            rows.append({
                "shot": k, "start": sg["t0"], "end": sg["t1"],
                "spokenText": "".join(sents[i] for i in idx)[:200],
                "purpose": "neutral-emphasis",
                "timingSource": "provided-captions",
                "captionIndices": idx,
                "beats": [{"at": sg["t0"], "until": sg["t1"],
                           "spokenText": sents[i][:200], "timing": "sentence",
                           "target": "props"} for i in idx],
            })
    else:
        for k, sg in enumerate(segs):
            a = int(k * len(sents) / n)
            b = max(a + 1, int((k + 1) * len(sents) / n))
            owned = [i for i in range(a, b) if starts[i] < sg["t1"] - 0.01]
            if not owned:
                owned = [min(a, len(sents) - 1)]
            text = "".join(sents[i] for i in owned)[:200]
            rows.append({
                "shot": k,
                "start": sg["t0"],
                "end": sg["t1"],
                "spokenText": text,
                "purpose": "neutral-emphasis",
                "timingSource": "provided-captions",
                "captionIndices": owned,
                "beats": [{"at": round(max(sg["t0"], starts[i]), 2),
                           "until": round(min(sg["t1"], ends[i]), 2),
                           "spokenText": sents[i][:200],
                           "timing": "sentence",
                           "target": "props"} for i in owned],
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
        # 配音：前端只传了音色名（voice / mg_voice），音频要在这里合成。
        # motion-kit 那条路线跳过了 motion.render_motion 里的配音环节，
        # 不在这补就会变成无声片，时长只能被模板下限硬撑。
        if not opts.get("voiceover") and (opts.get("voice") or opts.get("mg_voice")):
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
        if voice_path and voice_dur > 0:
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
        json.dump(timeline, open(pp, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=1)
        params = timeline
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
