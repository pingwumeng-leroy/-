# -*- coding: utf-8 -*-
"""原创文案生成 v2（方案 v4 定稿版）。

参考结构（进 prompt 的内容，逐字稿原文永不进）：
1. 结构骨架：formula / 十维 why_it_works / reusable
2. 节奏指令：库内中位语速、长停顿、密度特征（学节奏不学句子）
3. 评论区真话：top_comments + comment_insight
4. 爆款短板：weaknesses（这次要避开）
5. 行话词典：从逐字稿本地提取的行业术语（均短于 8 字，句子永不进）
6. 政策要点卡（可选）：上传政策文件提取，文案主体内容讲该政策

原创性双保险：提示词硬约束 + 生成后 8 字 n-gram 查重（命中自动重写，
仍命中标红人工复核；政策官方术语进豁免词表）。

时长控制：目标字数 = 时长 × 库内中位语速，偏差 ±15% 外自动修订。
输出：每段带口播节奏（快/中/慢/停顿）与素材提示（cue），供口播与剪辑用。
"""
import json
import os
import re
import statistics
import subprocess

from . import config, db, llm

SYSTEM = """你是「四川合胜宏科技集团有限公司」成都本地企业服务机构创始人抖音账号的内容策划。
基于提供的【参考洞察】，生成全新的原创口播脚本。

硬性约束（违反任一条即作废）：
1. 只参考抽象结论（结构、节奏、痛点、评论区关心的事）；禁止改写、套用任何参考原文的句子或开场白。
2. 与任何参考内容不得出现连续 8 个字以上的文字重叠：结构可以学，句子必须是全新组织。
3. 禁止出现：税务筹划、节税、省税、避税、税收洼地、核定征收、包过、保证、100%、零风险、最便宜、代开发票、买卖发票、垫资、提供注册地址、挂靠、免费注册、无需到场。
4. 只讲流程、政策科普与风险提示，不做个案结论；涉及政策须写「以主管机关现行规定为准」。
5. 优先嵌入「成都/四川」本地场景词，标题用目标客户会搜的口语化问句。
6. 结尾用平台内合规引导（评论区说说情况 / 主页有清单 / 私信发关键词领资料），禁止微信号、二维码。
7. 口语化、第一人称，像老板当面跟人讲话。
8. 若提供了【行话词典】：自然使用这些行业惯用词汇让文案专业，但禁止照搬任何参考原句。
9. 若提供了【政策要点卡】：文案主体内容讲这个政策；所有政策名称、数字、条件、日期只能逐字来自要点卡，
   卡里没有的不得出现，不确定的不得编；官方术语须与卡一致。

输出严格为 JSON（不要 markdown、不要解释）：
{"title":"用户搜索问句","keywords":["3~5个长尾词"],
 "segments":[{"seq":1,"text":"本段口播原文（纯文字，不含任何标记）",
   "pace":"口播节奏，用 快/中/慢/停N秒 组合，如「快-停0.5s-中」","secs":5.1,
   "cue":"该段需要素材的画面建议（如：政策文件封面+条款特写2秒）；不需要素材则为空字符串"}],
 "pattern_used":"引用了哪条结构模式","self_check":"合规自检一句话"}

segments 要求：5~12 段；每段 text 15~60 字；全部 secs 相加应接近目标时长；
只有画面支撑会明显提升效果的段落才给 cue，别每段都给。"""

POLICY_SYSTEM = """你是政策文件解析助手。从政策原文中提取结构化要点卡，供短视频文案创作使用。
只输出一个 JSON 对象，不要任何解释文字。

字段（全部必填，原文没有的填空字符串）：
{"policy_name":"政策/文件全称（逐字来自原文）","doc_number":"文号",
 "effective_date":"生效/印发时间","deadline":"申报或办理截止时间",
 "targets":"适用对象","core_clauses":["核心条款，每条≤60字，尽量保留原文关键表述"],
 "amounts":["补贴金额/比例/数字，逐字来自原文"],"conditions":["申报或适用条件"],
 "prohibitions":["禁止或限制情形"],"notes":"一句话说明本文讲了什么",
 "official_terms":["政策名称、法规条款号、官方术语等必须保持原文的词"]}

硬约束：所有数字、日期、条款表述必须逐字来自原文；禁止推断、补全或编造；
每个字符串字段不超过 80 字；输出为紧凑单行 JSON。"""

# ---------------------------------------------------------------- 参考报告加工

def _rj(r):
    try:
        return json.loads(r.get("report_json") or "{}")
    except Exception:
        return {}


def gather_refs(reports):
    """把报告库的原始行加工成文案生成需要的六类参考。"""
    refs = []
    for r in reports:
        R = _rj(r)
        ai = R.get("ai_analysis") or {}
        f = R.get("facts") or {}
        tf = f.get("transcript") or {}
        ins = ai.get("insights") or {}
        comments = [(x.get("text") or "") for x in
                    ((f.get("comments") or {}).get("top") or [])][:5]
        if not comments:
            comments = _as_list(r.get("top_comments"))[:5]
        refs.append({
            "id": r.get("id"), "title": r.get("title", ""),
            "formula": ai.get("formula") or r.get("reusable_pattern") or "",
            "weaknesses": [str(x) for x in (ai.get("weaknesses") or [])][:3],
            "pains": _as_list(r.get("pain_points"))[:4],
            "comments": [c for c in comments if c][:5],
            "comment_insight": ins.get("6", ""),
            "reusables": [str(d.get("reusable")) for d in (ai.get("dimensions") or [])
                          if isinstance(d, dict) and d.get("reusable")][:6],
            "cpm": tf.get("speech_rate_cpm"),
            "long_gaps": tf.get("long_gaps"),
            # 原文只用于本地查重，永不进 prompt
            "_tr": str(tf.get("full_text") or ""),
        })
    return refs


def _as_list(v):
    if not v:
        return []
    if isinstance(v, list):
        return [str(x) for x in v if str(x).strip()]
    try:
        x = json.loads(v)
        return [str(i) for i in x if str(i).strip()] if isinstance(x, list) else [str(x)]
    except Exception:
        return [str(v)]


def recall_reports(topic="", limit=12, per_topic=3):
    """按选题召回并打散，避免 AI 只看到单一选题。"""
    if topic:
        rows = db.q("SELECT * FROM reports WHERE topic LIKE ? ORDER BY id DESC LIMIT ?",
                    (f"%{topic}%", limit * 4))
    else:
        rows = db.q("SELECT * FROM reports ORDER BY id DESC LIMIT ?", (limit * 4,))
    seen, picked, ids = {}, [], []
    for r in rows:
        t = r.get("topic") or "未分类"
        if seen.get(t, 0) >= per_topic:
            continue
        seen[t] = seen.get(t, 0) + 1
        picked.append(r)
        ids.append(r["id"])
        if len(picked) >= limit:
            break
    return picked, ids


def build_context(refs):
    """四类参考提炼（不含任何逐字稿原文）。"""
    parts = []
    for i, x in enumerate(refs, 1):
        lines = [f"### 参考{i}｜{x['title'][:40]}（报告#{x['id']}）"]
        if x["formula"]:
            lines.append(f"- 结构骨架：{x['formula'][:120]}")
        if x["reusables"]:
            lines.append("- 可复用手法：" + "；".join(r[:40] for r in x["reusables"][:4]))
        if x["pains"]:
            lines.append("- 受众痛点：" + "；".join(p[:40] for p in x["pains"][:3]))
        if x["comments"]:
            lines.append("- 评论区真话：" + "；".join(c[:50] for c in x["comments"][:4]))
        if x["comment_insight"]:
            lines.append(f"- 评论区洞察：{x['comment_insight'][:80]}")
        if x["weaknesses"]:
            lines.append("- 爆款短板（本次要避开）：" + "；".join(w[:40] for w in x["weaknesses"]))
        parts.append("\n".join(lines))
    return "\n\n".join(parts)


# ---------------------------------------------------------------- 行话词典（方案 A）

_STOP_CH = set("的了是在和与及或者对于把我你他她它这那也就都很还又再不用没被一个上中下去出入为到之各每另如由从则而并但因此所以如果虽然便却已曾将地得着过其")
_GRAM_PUNCT = re.compile(r"[\s，。！？；：、,.!?;:()（）\[\]【】{}\"'“”‘’《》<>—…·]+")


def extract_terms(texts, min_freq=3, max_terms=40):
    """从逐字稿本地提取行业术语：2~7 字 n-gram，出现 ≥min_freq，均短于 8 字。
    只出词汇表，不出句子 —— 原创性由构造保证。
    边界独立性：gram 的出现大部分嵌在固定更长的串里（扩展频次≈自身频次）
    说明它只是窗口碎片而非词，剔除。"""
    cnt = {}
    for text in texts:
        text = _GRAM_PUNCT.sub("", text or "")
        # 按数字边界切一刀，避免"2024年10月"这类被当术语
        for chunk in re.split(r"(?<=\d)(?=\D)|(?<=\D)(?=\d)", text):
            L = len(chunk)
            # 数到 8 字：7 字候选的扩展频次需要 8 字 gram 才算得出
            for n in range(2, 9):
                for i in range(L - n + 1):
                    g = chunk[i:i + n]
                    cnt[g] = cnt.get(g, 0) + 1
    # 扩展频次：g 左/右加一字后的最长频次
    ext = {}
    for s, c in cnt.items():
        if len(s) < 3:
            continue
        for g in (s[1:], s[:-1]):
            ext[g] = max(ext.get(g, 0), c)

    cands = []
    for g, c in cnt.items():
        if c < min_freq or len(g) > 7:
            continue
        if g[0] in _STOP_CH or g[-1] in _STOP_CH:
            continue
        if len(g) > 2 and any(ch in _STOP_CH for ch in g[1:-1]):
            continue
        if re.fullmatch(r"[\d\.%年月日个]+", g):
            continue
        # 独立出现次数：总频次 - 被更长串覆盖的频次
        if c - ext.get(g, 0) < 2:
            continue
        cands.append((g, c))
    # 长词优先、频次高优先，去掉是更长已选术语子串的候选
    cands.sort(key=lambda x: (-len(x[0]), -x[1]))
    kept = []
    for g, c in cands:
        if any(g in k for k, _ in kept):
            continue
        kept.append((g, c))
        if len(kept) >= max_terms:
            break
    return [g for g, _ in kept]


# ---------------------------------------------------------------- 语速与时长

def median_cpm(refs):
    rates = [x["cpm"] for x in refs if x.get("cpm")]
    return float(statistics.median(rates)) if rates else 330.0


def target_chars(seconds, cpm):
    return max(80, int(seconds * cpm / 60.0))


# ---------------------------------------------------------------- 8 字查重

def _grams(s, n=8):
    s = _GRAM_PUNCT.sub("", s or "")
    return [s[i:i + n] for i in range(len(s) - n + 1)]


def ngram_overlaps(body, ref_texts, whitelist_text="", n=8):
    """正文与参考逐字稿的连续 n 字重叠检测。
    政策要点卡短语豁免用掩码法：正文里出现白名单短语（政策名/官方术语/
    条款，≥4 字）的位置替换成占位符再比对，这样短语及其边界两侧的 gram
    都不会与参考原文形成假命中。返回 [{quote}]。"""
    phrases = sorted({p for p in re.split(r"[；;，,。.\s、：:]+", whitelist_text or "")
                      if len(p) >= 4}, key=len, reverse=True)
    clean = _GRAM_PUNCT.sub("", body or "")
    masked = clean
    for p in phrases:
        masked = masked.replace(p, "□" * len(p))
    refset = set()
    for t in ref_texts:
        refset.update(_grams(t, n))
    if not refset:
        return []
    runs, cur_start, last_i = [], None, None
    for i in range(len(masked) - n + 1):
        g = masked[i:i + n]
        if "□" in g or g not in refset:
            if cur_start is not None:
                runs.append(clean[cur_start:i - 1 + n])
                cur_start, last_i = None, None
            continue
        if cur_start is None:
            cur_start, last_i = i, i
        elif i == last_i + 1:
            last_i = i
        else:
            runs.append(clean[cur_start:last_i + n])
            cur_start, last_i = i, i
    if cur_start is not None:
        runs.append(clean[cur_start:last_i + n])
    return [{"quote": q} for q in runs[:15] if len(q) >= n]


# ---------------------------------------------------------------- 敏感词 / 清洗

def check_sensitive(text):
    words = config.get_sensitive_words()
    return [{"word": w, "count": text.count(w)} for w in words if w in text]


def _clean_body(text):
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
    out = [ln for ln in lines if ln and not re.fullmatch(r"[【\[].{0,12}[】\]]", ln)]
    out = [re.sub(r"^[#>*\-\s]+", "", ln) for ln in out]
    return "\n".join(out).strip()


# ---------------------------------------------------------------- 政策文件

def policy_text(path):
    """PDF（pdftotext）/ docx（unzip document.xml）/ txt。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            out = subprocess.run(["pdftotext", "-enc", "UTF-8", path, "-"],
                                 capture_output=True, timeout=90)
            return out.stdout.decode("utf-8", "ignore")
        if ext == ".docx":
            xml = subprocess.run(["unzip", "-p", path, "word/document.xml"],
                                 capture_output=True, timeout=60).stdout.decode("utf-8", "ignore")
            xml = xml.replace("</w:p>", "\n")
            return re.sub(r"<[^>]+>", "", xml)
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            return fh.read()
    except Exception:
        return ""


def extract_policy_card(text):
    """一次 DeepSeek 调用提取要点卡；返回 dict（解析失败抛异常）。"""
    data = llm.chat_json([
        {"role": "system", "content": POLICY_SYSTEM},
        {"role": "user", "content": "政策原文（可能不完整，只依据给出的部分提取）：\n" + text[:8000]},
    ], temperature=0.1, max_tokens=4000)
    if not isinstance(data, dict) or not (data.get("policy_name") or data.get("core_clauses")):
        raise RuntimeError("要点卡解析失败：缺少政策名称或核心条款")
    for k in ("core_clauses", "amounts", "conditions", "prohibitions", "official_terms"):
        if not isinstance(data.get(k), list):
            data[k] = _as_list(data.get(k))
    return data


def _card_text(card):
    """要点卡全文（用于查重豁免词表）。"""
    parts = [str(card.get(k) or "") for k in
             ("policy_name", "doc_number", "effective_date", "deadline", "targets", "notes")]
    for k in ("core_clauses", "amounts", "conditions", "prohibitions", "official_terms"):
        parts.extend(str(x) for x in (card.get(k) or []))
    return "；".join(p for p in parts if p)


def _card_block(card):
    lines = [f"政策全称：{card.get('policy_name', '')}",
             f"文号：{card.get('doc_number', '') or '—'}",
             f"生效时间：{card.get('effective_date', '') or '—'}",
             f"截止时间：{card.get('deadline', '') or '—'}",
             f"适用对象：{card.get('targets', '') or '—'}"]
    for key, name in (("core_clauses", "核心条款"), ("amounts", "数字/补贴"),
                      ("conditions", "条件"), ("prohibitions", "禁止情形")):
        items = [str(x) for x in (card.get(key) or []) if str(x).strip()]
        if items:
            lines.append(f"{name}：" + "；".join(items))
    lines.append(f"官方术语（须保持原文）：{'、'.join(str(x) for x in (card.get('official_terms') or []) if str(x).strip()) or '—'}")
    lines.append("说明：" + str(card.get("notes") or "—"))
    return "\n".join(lines)


# ---------------------------------------------------------------- 规则兜底

def rule_generate(requirement, refs):
    """规则兜底：按结构骨架拼装合格但平庸的脚本，带基础节奏标记，素材提示从缺。"""
    topic = requirement.get("topic") or "代理记账"
    city = requirement.get("city") or "成都"
    seconds = int(requirement.get("seconds") or 60)
    cpm = median_cpm(refs) if refs else 330.0
    per_sec = cpm / 60.0

    pains = [p for x in refs for p in x["pains"]][:3] or ["不清楚办理顺序", "材料没准备齐", "以为放着不管就没事"]
    pains = list(dict.fromkeys(pains))[:3]

    lines = [
        f"在{city}做企业服务这么多年，{topic}这件事，十个老板有八个第一步就走错。",
        "先说结论：顺序比材料更要紧，别一上来就往窗口跑。",
        "最常见的三个卡点：" + "；".join(pains[:3]) + "。",
        "第一步，先把状态查清楚，别凭印象判断；第二步，把该补的先补完；第三步，按主管机关要求的顺序提交。",
        f"补一条{city}本地的提醒：不同区窗口的细节要求会有差别，以主管机关现行规定为准。",
    ]
    if requirement.get("policy_card"):
        lines.insert(1, "今天讲的这个政策，条件和时间我先给你捋清楚，对照着自查。")
        lines.append("政策细节以文件原文和主管机关现行规定为准，别听二道贩子转述。")
    points = (requirement.get("points") or "").strip()
    if points:
        lines.append("还有你特别要讲的几点：" + points)
    lines.append("评论区说说你的情况，我帮你判断卡在哪一步。")

    segments = []
    for i, text in enumerate(lines, 1):
        text = text.strip()
        segments.append({"seq": i, "text": text, "pace": "中" if i == 1 else "中-停0.3s",
                         "secs": round(len(text) / per_sec, 1), "cue": ""})
    total = round(sum(s["secs"] for s in segments), 1)
    body = _clean_body("\n".join(s["text"] for s in segments))
    return {
        "title": requirement.get("title") or f"{city}办{topic}，哪一步最容易卡住？",
        "keywords": [f"{city}{topic}流程", f"{topic}需要什么材料", f"{topic}要多久",
                     f"{city}企业服务"],
        "body": body,
        "segments": segments,
        "pattern_used": "本地场景 → 否定默认做法 → 有序三步 → 本地细节 → 互动收尾",
        "self_check": f"兜底模式生成：已规避敏感词；含政策兜底表述；共 {len(body.replace(chr(10), ''))} 字 / "
                      f"预计 {total}s（目标 {seconds}s）。DeepSeek 不可用时的模板稿，建议恢复 AI 后重新生成。",
        "engine": "rule",
    }


# ---------------------------------------------------------------- 主流程

def _fix_segments(res, cpm):
    """补齐/规范 segments：seq、secs 缺省按字数与语速估算。"""
    per_sec = max(2.0, cpm / 60.0)
    segs = []
    for i, s in enumerate(res.get("segments") or [], 1):
        if not isinstance(s, dict):
            continue
        text = _clean_body(str(s.get("text") or ""))
        if not text:
            continue
        try:
            secs = round(float(s.get("secs") or 0), 1)
        except (TypeError, ValueError):
            secs = 0
        if secs <= 0:
            secs = round(len(text) / per_sec, 1)
        segs.append({"seq": i, "text": text, "pace": str(s.get("pace") or "中"),
                     "secs": secs, "cue": str(s.get("cue") or "").strip()})
    return segs


def generate_script(requirement):
    """返回 (script_id, result_dict)。"""
    topic = requirement.get("topic") or ""
    seconds = max(10, int(requirement.get("seconds") or 60))
    policy_card = requirement.get("policy_card") if isinstance(requirement.get("policy_card"), dict) else None

    reports, ids = recall_reports(topic=topic, limit=12)
    refs = gather_refs(reports)
    transcripts = [x["_tr"] for x in refs if x["_tr"]]
    terms = extract_terms(transcripts) if transcripts else []
    cpm = median_cpm(refs)
    tgt_chars = target_chars(seconds, cpm)
    context = build_context(refs)
    extra_prompt = config.get_generate_prompt().strip()

    req_lines = [
        f"选题：{topic or '从参考洞察里挑高价值选题'}",
        f"地域：{requirement.get('city') or '成都'}",
        f"目标时长：{seconds} 秒（约 {tgt_chars} 字，库内中位语速 {round(cpm)} 字/分）",
        f"语气风格：{requirement.get('tone') or '平实、像当面聊'}",
        f"必须讲到的要点：{requirement.get('points') or '无特别要求'}",
        f"要带出的业务：{requirement.get('business') or '无特别要求'}",
        f"结尾引导：{requirement.get('cta') or '评论区说说你的情况'}",
        f"指定标题（可忽略重写）：{requirement.get('title') or '无'}",
    ]
    if requirement.get("hook_style"):
        req_lines.append(f"开场风格：{requirement.get('hook_style')}")
    sections = ["【老板的要求】\n" + "\n".join(req_lines)]
    if extra_prompt:
        sections.append("【补充规则】\n" + extra_prompt)
    if policy_card:
        sections.append("【政策要点卡 —— 文案主体内容讲这个政策，数字/条件/日期只能来自这里】\n"
                        + _card_block(policy_card))
    if context:
        sections.append("【参考洞察（结构骨架 / 节奏 / 评论区真话 / 爆款短板；不含口播原文）】\n"
                        + context)
    if terms:
        sections.append("【行话词典 —— 目标行业从业者惯用词汇，应自然使用，禁止照搬任何原句】\n"
                        + "、".join(terms))
    sections.append("【节奏指令】全篇节奏贴近库内中位语速；每段给出快/中/慢/停顿标记与秒数，"
                    "secs 合计 ≈ 目标时长；只在画面支撑明显提升效果的段落给 cue（素材建议）。")
    user_msg = "\n\n".join(sections)

    res, rewrites, orig = None, 0, {"hits": [], "rewrites": 0, "checked": bool(transcripts)}
    wl_text = _card_text(policy_card) if policy_card else ""

    if llm.available():
        messages = [{"role": "system", "content": SYSTEM},
                    {"role": "user", "content": user_msg}]
        feedback = ""
        try:
            for attempt in range(3):  # 首次 + 最多 2 次修订（查重命中 / 时长偏差）
                prompt_msgs = messages if not feedback else \
                    messages + [{"role": "user", "content": "【上一版问题，必须修复后重新输出完整 JSON】\n" + feedback}]
                res = llm.chat_json(prompt_msgs, temperature=0.8 if attempt == 0 else 0.6,
                                    max_tokens=4000)
                res["segments"] = _fix_segments(res, cpm)
                if not res.get("title") or len(res.get("segments") or []) < 3:
                    raise ValueError("输出不完整：缺标题或段落不足")
                res["engine"] = "llm"
                rewrites = attempt

                body = "\n".join(s["text"] for s in res["segments"])
                hits = ngram_overlaps(body, transcripts, wl_text)
                total = round(sum(s["secs"] for s in res["segments"]), 1)
                dev = abs(total - seconds) / seconds
                problems = []
                if hits:
                    problems.append("以下片段与参考原文重叠超过 8 字，必须换说法重写：\n- "
                                    + "\n- ".join(h["quote"] for h in hits[:6]))
                if dev > 0.15:
                    problems.append(f"当前预计 {total}s，目标 {seconds}s，偏差 {round(dev*100)}%，"
                                    f"请增删段落把总时长拉回 ±15% 以内（约 {tgt_chars} 字）。")
                if not problems:
                    orig = {"hits": [], "rewrites": rewrites, "checked": bool(transcripts)}
                    break
                feedback = "\n".join(problems)
                if attempt == 2:
                    orig = {"hits": hits, "rewrites": rewrites, "checked": bool(transcripts)}
            if res and res.get("engine") == "llm":
                body = "\n".join(s["text"] for s in res["segments"])
                res["body"] = _clean_body(body)
                res.setdefault("keywords", [])
                res.setdefault("pattern_used", "")
                res.setdefault("self_check", "")
        except Exception:
            res = rule_generate(requirement, refs)
    else:
        res = rule_generate(requirement, refs)

    hits = check_sensitive(res.get("body", "") + res.get("title", ""))
    total_secs = round(sum(s["secs"] for s in (res.get("segments") or [])), 1)
    res["total_secs"] = total_secs
    res["target_secs"] = seconds
    res["target_chars"] = tgt_chars
    res["char_count"] = len((res.get("body") or "").replace("\n", ""))
    res["originality"] = orig
    res["source_report_ids"] = ids
    sid = db.add_script(res.get("title", "未命名脚本"), res.get("body", ""),
                        requirement, ids, res.get("engine", "rule"), hits,
                        segments=res.get("segments") or [], originality=orig)
    return sid, res
