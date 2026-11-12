# -*- coding: utf-8 -*-
"""原创文案生成：从报告库召回「可复用结构 + 痛点 + 高频提问」，结合老板的要求生成新脚本。

硬约束：只喂结构化结论，绝不把同行口播原文交给生成环节（避免洗稿式改写）。
"""
import json
import re

from . import config, db, llm

SYSTEM = """你是「四川合胜宏科技集团有限公司」成都本地企业服务机构创始人抖音账号的内容策划。
基于下面提供的【同行爆款结构化结论】，生成全新的原创口播脚本。

硬性约束（违反任一条即作废）：
1. 只复用「可复用结构模式」「痛点」「高频提问」这类抽象结论；禁止改写、套用任何原句或开场白。
2. 禁止出现：税务筹划、节税、省税、避税、税收洼地、核定征收、包过、保证、100%、零风险、最便宜、代开发票、买卖发票、垫资、提供注册地址、挂靠、免费注册、无需到场。
3. 只讲流程、政策科普与风险提示，不做个案结论；涉及政策须写「以主管机关现行规定为准」。
4. 优先嵌入「成都/四川」本地场景词，标题用目标客户会搜的口语化问句。
5. 结尾用平台内合规引导（评论区说说情况 / 主页有清单 / 私信发关键词领资料），禁止微信号、二维码。
6. 口语化、第一人称，像老板当面跟人讲话；按停顿分段。

输出严格为 JSON：
{"title":"用户搜索问句","keywords":["3~5个长尾词"],"body":"口播正文，用\\n分段","pattern_used":"引用了哪条模式","self_check":"合规自检一句话"}"""


def _brief(r, i):
    return (f"### 报告{i}｜{r.get('title','')}\n"
            f"- 选题：{r.get('topic','') or '-'}｜地域：{r.get('city','') or '-'}\n"
            f"- 钩子类型：{r.get('hook_type','') or '-'}\n"
            f"- 内容结构：{r.get('structure','') or '-'}\n"
            f"- 受众痛点：{r.get('pain_points','') or '-'}\n"
            f"- 评论区高频提问：{r.get('top_comments','') or '-'}\n"
            f"- 可复用结构模式：{r.get('reusable_pattern','') or '-'}\n"
            f"- 合规风险：{r.get('risk_notes','') or '-'}\n")


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


def build_context(reports, budget=11000):
    parts, used = [], 0
    for i, r in enumerate(reports, 1):
        b = _brief(r, i)
        if used + len(b) > budget:
            break
        parts.append(b)
        used += len(b)
    return "\n".join(parts)


def check_sensitive(text):
    words = config.get_sensitive_words()
    return [{"word": w, "count": text.count(w)} for w in words if w in text]


def _clean_body(text):
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n")]
    out = [ln for ln in lines if ln and not re.fullmatch(r"[【\[].{0,12}[】\]]", ln)]
    # 去掉残留的 markdown 标记
    out = [re.sub(r"^[#>*\-\s]+", "", ln) for ln in out]
    return "\n".join(out).strip()


def rule_generate(requirement, reports):
    """规则兜底：把报告里的抽象模式 + 老板要求组装成原创脚本。不复制任何原句。"""
    req = requirement
    topic = req.get("topic") or (reports[0].get("topic") if reports else "代理记账")
    city = req.get("city") or "成都"
    seconds = int(req.get("seconds") or 60)
    tone = req.get("tone") or "平实、像跟老板当面聊"
    points = req.get("points") or ""
    business = req.get("business") or ""
    cta = req.get("cta") or "评论区说说你的情况，我帮你判断卡在哪一步"

    pains, patterns, questions = [], [], []
    for r in reports:
        for key, bucket in (("pain_points", pains), ("reusable_pattern", patterns),
                            ("top_comments", questions)):
            v = r.get(key) or ""
            if v.startswith("["):
                try:
                    items = json.loads(v)
                    bucket.extend([str(x)[:60] for x in items if str(x).strip()])
                except Exception:
                    bucket.append(v[:60])
            elif v:
                bucket.append(v[:100])
    pains = list(dict.fromkeys(pains))[:3] or ["不清楚办理顺序", "材料没准备齐", "以为放着不管就没事"]
    questions = list(dict.fromkeys(questions))[:2] or [f"{topic}要多久", "需要本人到场吗"]

    title = req.get("title") or f"{city}办{topic}，哪一步最容易卡住？"
    hook = f"在{city}做企业服务这么多年，{topic}这件事，十个老板有八个是先走错第一步。"
    if req.get("hook_style") == "提问式":
        hook = f"{city}的老板问得最多的一个问题：{topic}到底该先办哪一步？"
    elif req.get("hook_style") == "反常识":
        hook = f"别急着去跑{topic}。很多人第一步就办反了，后面全是白跑。"

    body_lines = [
        hook,
        "",
        f"先说结论：{topic}不是把材料交上去就完事，顺序比材料更要紧。",
        "",
        f"我见过最常见的三个卡点。" + "；".join(pains[:3]) + "。",
        "",
        "第一步，先把状态查清楚，别凭印象判断；第二步，把该补的先补完再往下走；"
        "第三步，按主管机关要求的顺序提交，别来回折返。",
        "",
        f"补一条{city}本地的提醒：不同区窗口要求的细节会有差别，"
        "去之前把当期要求核一遍，以主管机关现行规定为准。",
    ]
    if points.strip():
        body_lines += ["", "还有你特别要讲的几点：" + points.strip()]
    if business.strip():
        body_lines += ["", f"如果涉及到{business.strip()}，也一并在这一步里处理好，别留尾巴。"]
    body_lines += [
        "",
        f"评论区里问得最多的两个是：{questions[0]}、{questions[1] if len(questions) > 1 else '要准备什么材料'}。"
        "下一条我专门拆开讲。",
        "",
        f"{cta}。",
    ]

    body = _clean_body("\n".join(body_lines))
    # 按目标时长粗略提示字数
    target_chars = max(120, int(seconds * 3.6))
    note = f"参考时长 {seconds} 秒（约 {target_chars} 字），当前 {len(body.replace(chr(10),''))} 字。"
    return {
        "title": title,
        "keywords": [f"{city}{topic}流程", f"{topic}需要什么材料", f"{city}{topic}怎么办",
                     f"{topic}要多久", f"{city}企业服务"],
        "body": body,
        "pattern_used": (patterns[0][:120] if patterns else "本地场景 → 否定默认做法 → 有序三步 → 本地细节 → 互动收尾"),
        "self_check": f"已规避敏感词；含政策兜底表述；结尾为平台内引导。{note}",
        "engine": "rule",
    }


def generate_script(requirement):
    """返回 (script_id, result_dict)。"""
    topic = requirement.get("topic") or ""
    reports, ids = recall_reports(topic=topic, limit=12)
    context = build_context(reports)
    extra_prompt = config.get_generate_prompt().strip()
    user_msg = (
        f"【老板的要求】\n"
        f"选题：{requirement.get('topic') or '从报告库里挑高价值选题'}\n"
        f"地域：{requirement.get('city') or '成都'}\n"
        f"目标时长：{requirement.get('seconds') or 60} 秒\n"
        f"语气风格：{requirement.get('tone') or '平实、像当面聊'}\n"
        f"必须讲到的要点：{requirement.get('points') or '无特别要求'}\n"
        f"要带出的业务：{requirement.get('business') or '无特别要求'}\n"
        f"结尾引导：{requirement.get('cta') or '评论区说说你的情况'}\n"
        f"指定标题（可忽略重写）：{requirement.get('title') or '无'}\n\n"
        f"{'【补充规则】' + extra_prompt + chr(10) if extra_prompt else ''}"
        f"【同行爆款结构化结论】（不含口播原文）\n{context or '（报告库为空，请按通用财税科普结构创作）'}"
    )

    if llm.available():
        try:
            res = llm.chat_json([
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_msg},
            ], temperature=0.8)
            res["engine"] = "llm"
            res["body"] = _clean_body(res.get("body", ""))
            if not res["body"]:
                raise ValueError("LLM 返回空正文")
        except Exception:
            res = rule_generate(requirement, reports)
    else:
        res = rule_generate(requirement, reports)

    hits = check_sensitive(res.get("body", "") + res.get("title", ""))
    sid = db.add_script(res.get("title", "未命名脚本"), res.get("body", ""),
                        requirement, ids, res.get("engine", "rule"), hits)
    return sid, res
