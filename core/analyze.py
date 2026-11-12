# -*- coding: utf-8 -*-
"""多维度视频分析：把口播文案变成结构化报告，存入报告库。

启用 LLM → 真实多维分析；未启用 → 内置规则引擎，输出同结构报告（可跑通、可入库、可被生成环节消费）。
"""
import json
import re

from . import config, db, llm

TOPIC_RULES = [
    ("工商注销", ["注销", "吊销", "简易注销", "清税"]),
    ("税务异常", ["异常", "解非", "非正常", "风险纳税人", "逾期申报"]),
    ("代理记账", ["代理记账", "代账", "记账报税", "零申报", "小规模"]),
    ("注册公司", ["注册公司", "执照", "开办", "新公司", "核名"]),
    ("汇算清缴", ["汇算", "所得税", "年报", "工商年报"]),
    ("税收优惠", ["优惠", "减免", "加计扣除", "补贴", "退税"]),
    ("发票业务", ["发票", "增量", "增版", "开票", "专票"]),
    ("公司变更", ["变更", "法人变更", "股权", "地址迁移"]),
    ("理账补账", ["乱账", "理账", "补账", "旧账"]),
    ("审计报告", ["审计", "验资", "评估"]),
    ("财税合规", ["合规", "风险检测", "财务分析", "税务监控"]),
    ("社保开户", ["社保", "公积金", "医保", "银行开户"]),
]

HOOK_RULES = [
    ("提问式", ["吗", "？", "?", "怎么", "为什么", "要不要", "能不能"]),
    ("反常识", ["别再", "错了", "其实", "并不是", "很多人以为", "不是"]),
    ("数字式", ["3步", "三步", "几步", "个", "条", "第一", "90%", "块"]),
    ("痛点场景", ["跑了", "被罚", "卡住", "办不", "来不及", "踩坑", "白跑"]),
    ("政策变动", ["新规", "新政", "今年", "最新", "刚出", "调整"]),
    ("案例故事", ["有个客户", "昨天", "上周", "一位老板", "遇到一个"]),
]

CITY_WORDS = ["成都", "四川", "高新区", "天府新区", "锦江", "青羊", "武侯", "成华", "金牛", "龙泉驿"]


def guess_topic(text):
    hit, best = "其他", 0
    for name, kws in TOPIC_RULES:
        n = sum(text.count(k) for k in kws)
        if n > best:
            best, hit = n, name
    return hit


def guess_hook(text):
    head = text[:120]
    for name, kws in HOOK_RULES:
        if any(k in head for k in kws):
            return name
    return "平铺直叙"


def split_sentences(text):
    text = re.sub(r"[ \t]+", "", text)
    parts = re.split(r"[。！？!?；;\n]+", text)
    return [p.strip() for p in parts if len(p.strip()) >= 4]


def rule_analyze(title, transcript, meta=None):
    """规则引擎：输出与 LLM 完全一致的字段结构。"""
    meta = meta or {}
    text = (title + "\n" + transcript).strip()
    sents = split_sentences(transcript)
    topic = guess_topic(text)
    hook = guess_hook(transcript or title)
    city = next((c for c in CITY_WORDS if c in text), "全国")

    # 结构：按顺序把句子归成几段
    structure = []
    for i, s in enumerate(sents[:8]):
        role = "开场钩子" if i == 0 else ("收尾引导" if i == len(sents[:8]) - 1 else "主体论述")
        structure.append({"seq": i + 1, "role": role, "gist": s[:60]})
    if not structure:
        structure = [{"seq": 1, "role": "开场钩子", "gist": title[:60]}]

    pain = [s[:50] for s in sents if any(k in s for k in
            ["不", "错", "麻烦", "罚", "风险", "卡", "难", "坑", "逾期", "贵", "慢"])][:5]
    if not pain:
        pain = [f"{topic}的办理顺序不清楚", "不知道该准备哪些材料"]

    top_comments = [f"{topic}要多久能办完？", "需要本人到场吗？", "费用大概多少？"]
    cta = "结尾引导" if any(k in (sents[-1] if sents else "") for k in
                          ["评论", "私信", "关注", "留言", "主页", "联系"]) else "无明显引导"

    pattern = (f"【{city if city != '全国' else '地域'}场景】{hook}开场 → "
               f"点出『{topic}』里最容易卡住的一步 → 按 {max(2, min(4, len(structure)))} 步给出顺序 → "
               f"补一条本地办理细节 → 低门槛互动收尾。可复用于同类流程型选题。")

    risk = "未发现明显违规表述（仍需人工复核政策时效）。" if not any(
        w in text for w in ["筹划", "节税", "包过", "保证"]) else "⚠️ 检测到高风险的承诺/节税类表述，禁止复用。"

    return {
        "title": title,
        "topic": topic,
        "city": city,
        "hook_type": hook,
        "hook_text": (sents[0][:80] if sents else title[:80]),
        "structure": structure,
        "cta": cta,
        "pain_points": pain,
        "top_comments": top_comments,
        "policy_refs": ["需人工核验现行有效版本后再引用"],
        "reusable_pattern": pattern,
        "risk_notes": risk,
        "summary": f"这是一条面向{city if city != '全国' else '泛地域'}老板的『{topic}』内容，"
                   f"以{hook}开场，共 {len(sents)} 个语义句，{cta}。",
        "engine": "rule",
        "meta": {"likes": meta.get("likes", 0), "author": meta.get("author", ""),
                 "duration": meta.get("duration", 0)},
    }


ANALYZE_SYSTEM = """你是短视频内容分析专家，服务对象是成都本地的企业财税/工商服务机构。
请对给定的同行视频口播文案做多维度拆解，只输出 JSON 对象，不要任何解释文字。

字段要求（全部必填）：
{
 "title": "视频标题",
 "topic": "选题归类，从这些里选最接近的一个：工商注销/税务异常/代理记账/注册公司/汇算清缴/税收优惠/发票业务/公司变更/理账补账/审计报告/财税合规/社保开户/其他",
 "city": "地域词，如 成都/四川/全国",
 "hook_type": "提问式/反常识/数字式/痛点场景/政策变动/案例故事/平铺直叙",
 "hook_text": "开头3秒原话（仅记录用于分析）",
 "structure": [{"seq":1,"role":"开场钩子","gist":"这一段的要点"}],
 "cta": "结尾引导方式及效果判断",
 "pain_points": ["受众痛点"],
 "top_comments": ["推测的评论区高频提问"],
 "policy_refs": ["引用的政策名称，需标注待核验"],
 "reusable_pattern": "抽象的可复用结构模式：只描述结构与节奏，不要引用原句",
 "risk_notes": "合规风险：敏感词、夸大政策、焦虑营销、平台禁限业务",
 "summary": "一句话总结"
}
注意：reusable_pattern 必须是抽象模式，不能包含可直接搬用的原句。"""


def analyze_video(video_id):
    """分析一条素材，落库返回 report id。"""
    v = db.get_video(vid=video_id)
    if not v:
        raise RuntimeError("素材不存在")
    title = v["title"] or "未命名素材"
    transcript = v.get("transcript") or ""
    meta = {"likes": v.get("likes", 0), "author": v.get("author", ""),
            "duration": v.get("duration", 0)}

    if llm.available():
        try:
            r = llm.chat_json([
                {"role": "system", "content": ANALYZE_SYSTEM},
                {"role": "user", "content":
                    f"标题：{title}\n作者：{meta['author']}\n点赞：{meta['likes']}\n"
                    f"口播文案：\n{transcript[:6000]}"},
            ], temperature=0.3)
            r["engine"] = "llm"
            r.setdefault("title", title)
            r.setdefault("meta", meta)
        except Exception:
            r = rule_analyze(title, transcript, meta)
    else:
        r = rule_analyze(title, transcript, meta)

    engine = r.get("engine", "rule")
    rid = db.add_report(r, video_id=video_id, engine=engine)
    db.update_video(video_id, status="analyzed")
    return rid
