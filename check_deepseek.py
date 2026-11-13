# -*- coding: utf-8 -*-
"""DeepSeek 真实分析链路自检（不改产品代码、不采集视频、不花 TikHub 的钱）。

在服务器上（git pull 后脚本就在项目根目录）：
    cd /opt/douyin-studio && sudo .venv/bin/python check_deepseek.py
    # 脚本被放到别处时，用 DS_ROOT 指向项目目录：
    # DS_ROOT=/opt/douyin-studio sudo -E /opt/douyin-studio/.venv/bin/python ~/check_deepseek.py

网页上的「测试大模型连接」只发 16 tokens 的短文本，不带 json_mode；
而真实分析要 response_format=json_object + 4000 tokens 的十维 JSON。
本脚本直接调用生产用的 analyze.deep_analyze()，完整复现采集时的那条链路，
所以在重新采集之前就能判断会不会静默回落规则引擎。
"""
import os
import sys
import time

ROOT = os.environ.get("DS_ROOT") or os.path.dirname(os.path.abspath(__file__))
if not os.path.isdir(os.path.join(ROOT, "core")):
    print("找不到项目目录：%s（可用 DS_ROOT=/path 指定）" % ROOT)
    sys.exit(2)
sys.path.insert(0, ROOT)

from core import config, llm, analyze  # noqa: E402

s = config.get_settings().get("llm", {})
key = str(s.get("api_key") or "")
print("配置：enabled=%s  base_url=%s  model=%s  key=%s" % (
    s.get("enabled"), s.get("base_url"), s.get("model"),
    (key[:6] + "…" + key[-4:]) if len(key) > 10 else "(空)"))

if not llm.available():
    print("\n✗ 未启用或 Key / Base URL 缺失，先在网页设置页保存后再跑。")
    sys.exit(2)

# 与生产同构的一条最小假数据，不触发任何 TikHub 调用
item = {
    "aweme_id": "SELFTEST0001",
    "title": "个体户注销，清税申报这一步最容易卡住 #注销 #代理记账",
    "desc": "个体户注销前要先做清税申报，很多人卡在这一步，材料没备齐会白跑一趟。",
    "author": "自检样例",
    "digg_count": 4130, "comment_count": 133, "collect_count": 10,
    "share_count": 11, "duration": 44.0, "publish_time": 0,
    "share_url": "",
}
comments = [
    {"text": "终于讲明白了，我跑了三趟都没办下来", "digg_count": 88, "replies": 3, "date": "2026-11-01"},
    {"text": "成都这边也是这个流程吗", "digg_count": 45, "replies": 1, "date": "2026-11-02"},
    {"text": "学到了，收藏", "digg_count": 12, "replies": 0, "date": "2026-11-03"},
]

print("\n正在按生产链路调用 %s（json_mode + 4000 tokens 十维 JSON）…" % s.get("model"))
t0 = time.time()
try:
    r = analyze.deep_analyze(item, comments)
except Exception as exc:
    print("\n✗ 真实分析调用失败：%s: %s" % (type(exc).__name__, exc))
    print("  这就是批量采集会「静默回落规则引擎」的原因。常见三类：")
    print("  1) 模型不支持 response_format=json_object → 设置页模型名改 deepseek-chat")
    print("  2) 输出被截断 / 不是合法 JSON → 检查 llm.max_tokens 是否被改小")
    print("  3) 超时 → config/settings.json 里把 llm.timeout 调到 120")
    sys.exit(1)

cost = time.time() - t0
ai = r.get("ai_analysis") or {}
dims = ai.get("dimensions") or []
ins = ai.get("insights") or {}
print("\n✓ 真实链路可用，耗时 %.1fs" % cost)
print("  engine       = %s" % r.get("engine"))
print("  十维维度数   = %d（应为 10）" % len(dims))
print("  章节洞察     = %s（应为 1/2/3/4/6）" % ",".join(sorted(ins.keys())))
print("  chapters     = %s" % ai.get("chapters"))
print("  facts 节点   = %s" % ",".join(sorted((r.get("facts") or {}).keys())))
print("  一句话结论   = %s" % (ai.get("one_line") or "")[:80])

ok = (r.get("engine") == "llm" and len(dims) == 10
      and all(k in ins for k in ("1", "2", "3", "4", "6")))
print("\n" + ("✓ 自检通过：现在去第 2 步换一个没采过的关键词采 1 条，就能看到 AI 十维详情页。"
              if ok else "! 调用成功但结构不完整，把上面输出发我即可定位。"))
sys.exit(0 if ok else 1)
