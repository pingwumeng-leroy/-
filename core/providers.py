# -*- coding: utf-8 -*-
"""数据源契约：把任意供应商的视频搜索结果归一化成统一的 SearchItem。

下游（harvest / analyze / reports）只认 SearchItem，不认供应商。
- MockProvider   : 内置财税垂类演示数据，不联网、不花钱，就能跑通全链路
- TikHubProvider : 对接 TikHub /api/v1/douyin/search/fetch_video_search_v2

换供应商 = 新增一个子类 + 在 get_provider() 注册，其余代码零改动。
"""
import time
from datetime import datetime, timedelta

import requests

from . import analyze, config

# 统一字段：所有 provider 都必须吐出这些键（缺的补默认值）
ITEM_FIELDS = (
    "aweme_id", "title", "desc", "author", "author_id",
    "digg_count", "comment_count", "collect_count", "share_count",
    "publish_time", "duration", "share_url", "cover_url", "play_url",
)
NUM_FIELDS = {"digg_count", "comment_count", "collect_count", "share_count",
              "publish_time", "duration"}


class ProviderError(Exception):
    """数据源异常基类。"""


class ProviderUnavailable(ProviderError):
    """上游抓取失败/超时，可重试或降级，通常不计费。"""


class RateLimited(ProviderError):
    pass


class QuotaExceeded(ProviderError):
    pass


class AuthFailed(ProviderError):
    pass


def normalize(raw):
    """补齐字段并规范类型，保证下游拿到的结构恒定。"""
    item = {k: raw.get(k, "") if k not in NUM_FIELDS else 0 for k in ITEM_FIELDS}
    for k in NUM_FIELDS:
        try:
            item[k] = int(raw.get(k) or 0)
        except (TypeError, ValueError):
            item[k] = 0
    item["aweme_id"] = str(raw.get("aweme_id") or "").strip()
    item["title"] = (item["title"] or item["desc"] or "未命名视频").strip()
    if not item["share_url"] and item["aweme_id"] and not item["aweme_id"].startswith("MOCK"):
        item["share_url"] = "https://www.douyin.com/video/" + item["aweme_id"]
    return item


class SearchProvider:
    name = "base"
    label = "基础接口"
    supports_comments = False

    def search(self, keyword, *, cursor=0, sort_by="likes", publish_days=180,
               page_size=8, **kwargs):
        """返回 {"items": [SearchItem...], "cursor": int, "has_more": bool, "cost_calls": int}

        kwargs 接纳供应商专属的翻页令牌（search_id / backtrace），
        MockProvider 会忽略，TikHubProvider 用来串起下一页。
        """
        raise NotImplementedError

    def comments(self, aweme_id, cursor=0):
        """可选：返回公开评论文本列表。失败返回空列表，不影响主流程。"""
        return {"items": [], "cursor": 0, "has_more": False, "cost_calls": 0}

    def health(self):
        return {"ok": True, "provider": self.name}


# --------------------------------------------------------------------------
# 演示数据源：财税垂类样本，明确用 MOCK 前缀，避免被误认成真实采集结果
# --------------------------------------------------------------------------
_MOCK_SEED = [
    ("工商注销", "成都公司注销，为什么有人跑了三趟还没办完？", "成都企服老周", 12860, 8,
     "先把状态查清楚，再补材料，最后按顺序提交。很多老板第一步就跑反了。"),
    ("工商注销", "简易注销不是谁都能用，这几种情况办不了", "四川财税笔记", 9340, 21,
     "有欠税、有发票没缴销、有分支机构，都走不了简易注销。"),
    ("工商注销", "成都注销一家有限公司，完整流程有几步", "老周说企业服务", 7120, 35,
     "清算组备案、公告、清税、注销税务、注销工商、注销银行账户。"),
    ("工商注销", "成都注销公司，清税这一步最容易漏什么", "成都财税阿敏", 6210, 19,
     "清税没做完，工商那边不会受理，先查有没有未申报的期。"),
    ("工商注销", "公司吊销了不注销，会有什么后果", "老周说企业服务", 10480, 6,
     "吊销不等于注销，后续任职和再开公司都会受限制。"),
    ("工商注销", "简易注销公示期，成都老板常踩的两个坑", "四川财税笔记", 5580, 29,
     "公示期内有异议就转不了简易流程，只能走一般注销。"),
    ("税务异常", "公司被列入税务异常，先别急着去大厅", "成都财税阿敏", 15620, 5,
     "先查清楚是逾期申报还是地址失联，原因不同处理路径完全不同。"),
    ("税务异常", "税务异常解除要多久？成都老板常问的三个问题", "四川财税笔记", 8810, 14,
     "补申报、补资料、等审核，快的一周，慢的要看异常成因。"),
    ("代理记账", "成都找代账，别只比价格，先看这三件事", "老周说企业服务", 11400, 11,
     "看有没有许可证、看谁做账、看交接清不清楚。"),
    ("代理记账", "小规模纳税人零申报，长期做有没有风险", "成都财税阿敏", 6430, 42,
     "长期零申报又有开票记录，很容易被系统比对出来。"),
    ("注册公司", "成都注册公司流程和费用，一次讲清楚", "成都企服老周", 18230, 3,
     "核名、提交材料、领执照、刻章、银行开户、税务报到。"),
    ("注册公司", "在四川开公司，经营范围别照抄网上的模板", "四川财税笔记", 7280, 27,
     "经营范围顺序和表述会影响到后续核定和开票。"),
    ("汇算清缴", "汇算清缴前，先把这几张表对一遍", "成都财税阿敏", 9950, 18,
     "收入、成本、费用、纳税调整，四块对上再申报。"),
    ("税收优惠", "今年的小微企业优惠，成都企业怎么用", "老周说企业服务", 13750, 9,
     "先判断是否符合条件，再看申报表怎么填，以主管机关现行规定为准。"),
    ("发票业务", "发票增量增版被退回，多半卡在这一个材料", "成都企服老周", 5640, 51,
     "合同、流水、实际经营场所证明，缺一样都容易被打回。"),
    ("公司变更", "成都公司变更法人，先后顺序别搞反", "四川财税笔记", 6190, 33,
     "先做工商变更，再同步税务、银行、社保，别留尾巴。"),
    ("理账补账", "接手一套乱账，第一步不是补凭证", "成都财税阿敏", 4820, 64,
     "先把银行流水和发票对上，再倒推该补哪些。"),
    ("财税合规", "财务风险检测报告到底看什么", "老周说企业服务", 7930, 25,
     "看往来、看存货、看税负波动，不是只看利润表。"),
    ("社保开户", "成都新公司社保公积金开户，什么时候办", "成都企服老周", 4310, 76,
     "拿到执照、银行户开好后就该办，别等招了人才想起来。"),
]

_MOCK_COMMENTS = {
    "工商注销": ["注销要多久啊", "有欠税是不是就办不了", "需要本人到场吗"],
    "税务异常": ["逾期申报怎么补", "解除要几个工作日", "会不会影响开票"],
    "代理记账": ["一年大概多少钱", "怎么判断靠不靠谱", "换代账要交接什么"],
    "注册公司": ["流程和费用是多少", "要不要本人到场", "多久能拿执照"],
    "汇算清缴": ["什么时候截止", "都要准备哪些表", "亏损要不要做"],
    "税收优惠": ["我们这种能不能享受", "要备案吗", "怎么填申报表"],
}


class MockProvider(SearchProvider):
    name = "mock"
    label = "演示数据（不联网）"
    supports_comments = True

    def _all(self):
        now = datetime.now()
        out = []
        for i, (topic, title, author, likes, days, desc) in enumerate(_MOCK_SEED, 1):
            out.append(normalize({
                "aweme_id": "MOCK%012d" % i,
                "title": title, "desc": desc, "author": author,
                "author_id": "MOCKUSER%03d" % i,
                "digg_count": likes,
                "comment_count": int(likes * 0.033),
                "collect_count": int(likes * 0.16),
                "share_count": int(likes * 0.07),
                "publish_time": int((now - timedelta(days=days)).timestamp()),
                "duration": 45 + (i * 7) % 60,
                "cover_url": "", "play_url": "",
            }))
            out[-1]["_topic"] = topic
        return out

    def search(self, keyword, *, cursor=0, sort_by="likes", publish_days=180,
               page_size=8, **kwargs):
        kw = (keyword or "").strip()
        pool = self._all()
        if publish_days:
            floor = int((datetime.now() - timedelta(days=publish_days)).timestamp())
            pool = [x for x in pool if x["publish_time"] >= floor]
        if kw:
            hit = [x for x in pool if any(k in (x["title"] + x["desc"] + x["_topic"])
                                          for k in _split_kw(kw))]
            if not hit:
                # 模拟真实搜索：搜不到就返回空，绝不拿全池凑数污染报告
                pool = []
            else:
                topics = {x["_topic"] for x in hit}
                extra = [x for x in pool if x["_topic"] in topics and x not in hit]
                pool = hit + extra
        if sort_by == "likes":
            pool.sort(key=lambda x: x["digg_count"], reverse=True)
        else:
            pool.sort(key=lambda x: x["publish_time"], reverse=True)
        start = int(cursor)
        page = pool[start:start + page_size]
        nxt = start + len(page)
        return {"items": page, "cursor": nxt, "has_more": nxt < len(pool),
                "cost_calls": 1, "provider": self.name}

    def comments(self, aweme_id, cursor=0):
        idx = 0
        try:
            idx = int(str(aweme_id).replace("MOCK", ""))
        except ValueError:
            pass
        topic = _MOCK_SEED[(idx - 1) % len(_MOCK_SEED)][0] if idx else "工商注销"
        texts = _MOCK_COMMENTS.get(topic, ["这个要多久", "需要准备什么材料", "成都能办吗"])
        return {"items": [{"text": t, "digg_count": 120 - i * 30} for i, t in enumerate(texts)],
                "cursor": 0, "has_more": False, "cost_calls": 0, "provider": self.name}


def _split_kw(keyword):
    """把搜索词拆成可用于匹配的片段，并补上标准业务词。

    不用字符滑窗：滑窗会把「代账」「汇算清缴」这类无关视频也匹配进来，
    模拟真实搜索时必须保持关键词与选题的相关性。
    """
    parts = [keyword]
    for w in ("成都", "四川", "怎么办", "流程", "费用", "多少钱", "要多久", "需要什么", "材料"):
        if w in keyword:
            parts.append(keyword.replace(w, "").strip())
    for name, _kws in analyze.TOPIC_RULES:
        if name in keyword:
            parts.append(name)
        elif any(k in keyword for k in _kws):
            parts.append(name)
    return [p for p in dict.fromkeys(parts) if p]


# --------------------------------------------------------------------------
# TikHub：官方 spec V5.3.2 的路径是 /api/v1/douyin/search/fetch_video_search_v2（POST + JSON Body）
# --------------------------------------------------------------------------
class TikHubProvider(SearchProvider):
    name = "tikhub"
    label = "TikHub（真实抖音数据）"
    supports_comments = True
    SEARCH_PATH = "/api/v1/douyin/search/fetch_video_search_v2"
    COMMENTS_PATH = "/api/v1/douyin/web/fetch_video_comments"

    def __init__(self, base_url="", api_key="", timeout=30):
        self.base_url = (base_url or "https://api.tikhub.io").rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout or 30

    def _request(self, method, path, *, params=None, payload=None):
        """统一请求出口，认证与错误映射集中在这里。

        官方 spec V5.3.2：
        - 搜索 /search/fetch_video_search_v2 是 POST + JSON Body
        - 评论 /web/fetch_video_comments 是 GET + query 参数
        两者都用 Authorization: Bearer <token>。
        """
        if not self.api_key:
            raise AuthFailed("未配置 TikHub API Key，请在「设置 → 数据源」里填写")
        url = self.base_url + path
        try:
            r = requests.request(method, url, params=params, json=payload,
                                 timeout=self.timeout,
                                 headers={"Authorization": "Bearer " + self.api_key,
                                          "Accept": "application/json"})
        except requests.RequestException as exc:
            raise ProviderUnavailable("网络请求失败：%s" % type(exc).__name__)

        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                raise ProviderUnavailable("返回不是合法 JSON")
        # 关键：TikHub 的错误体会回显完整请求头（含 Authorization），
        # 这里绝不把原始响应体写进异常/日志，只取有限长度的中英文消息。
        msg = ""
        try:
            j = r.json()
            msg = j.get("message_zh") or j.get("message") or ""
        except ValueError:
            pass
        brief = "HTTP %d %s" % (r.status_code, str(msg)[:100])
        if r.status_code in (401, 403):
            raise AuthFailed(brief + "（检查 Key 是否有效、邮箱是否验证）")
        if r.status_code == 402:
            raise QuotaExceeded(brief + "（余额不足或该端点不接受免费额度）")
        if r.status_code == 429:
            raise RateLimited(brief)
        raise ProviderUnavailable(brief + "（多为上游抓取失败，可重试/降级）")

    @staticmethod
    def _pt(publish_days):
        return "180" if publish_days >= 180 else ("7" if publish_days >= 7 else ("1" if publish_days >= 1 else "0"))

    @staticmethod
    def _pick(*vals):
        """取第一个非 None 的值；has_more=0 是合法值，不能用 or 链。"""
        for v in vals:
            if v is not None:
                return v
        return None

    def search(self, keyword, *, cursor=0, sort_by="likes", publish_days=180,
               page_size=8, search_id="", backtrace=""):
        payload = {
            "keyword": keyword, "cursor": int(cursor),
            "sort_type": "1" if sort_by == "likes" else "2",
            "publish_time": self._pt(publish_days),
            "filter_duration": "0", "content_type": "1",
            # 翻页令牌必须用上一次响应里带回的值，首次请求才传空字符串
            "search_id": search_id or "", "backtrace": backtrace or "",
        }
        data = self._request("POST", self.SEARCH_PATH, payload=payload)
        body = data.get("data") or {}
        pg = body.get("pagination") or {}
        # 字段层级在不同版本可能落在 data / data.pagination / 顶层，逐层兜底
        rows = body.get("business_data")
        if rows is None:
            rows = data.get("business_data") or []
        items = []
        for row in rows:
            node = (row.get("data") or {}).get("aweme_info") or {}
            if not node.get("aweme_id"):
                continue
            st = node.get("statistics") or {}
            au = node.get("author") or {}
            video = node.get("video") or {}
            play = (video.get("play_addr") or {}).get("url_list") or []
            cover = (video.get("cover") or {}).get("url_list") or []
            items.append(normalize({
                "aweme_id": node.get("aweme_id"),
                "title": node.get("desc") or "",
                "desc": node.get("desc") or "",
                "author": au.get("nickname") or "",
                "author_id": au.get("uid") or "",
                "digg_count": st.get("digg_count"),
                "comment_count": st.get("comment_count"),
                "collect_count": st.get("collect_count"),
                "share_count": st.get("share_count"),
                "publish_time": node.get("create_time"),
                "duration": (video.get("duration") or 0) / 1000.0,
                "share_url": node.get("share_url") or "",
                "play_url": play[0] if play else "",
                "cover_url": cover[0] if cover else "",
            }))
        raw_cursor = self._pick(body.get("cursor"), pg.get("offset"), data.get("cursor"))
        try:
            nxt = int(raw_cursor)
        except (TypeError, ValueError):
            nxt = int(cursor) + len(items)
        has_more = self._pick(body.get("has_more"), pg.get("has_more"), data.get("has_more"))
        return {"items": items, "cursor": nxt,
                "has_more": bool(has_more) and bool(items),
                "search_id": self._pick(body.get("search_id"), pg.get("search_id"),
                                        data.get("search_id")) or "",
                "backtrace": self._pick(body.get("backtrace"), pg.get("backtrace"),
                                        data.get("backtrace")) or "",
                "cost_calls": 1, "provider": self.name}

    def comments(self, aweme_id, cursor=0):
        """评论端点为 GET + query；拿不到就返回空，不拖累主流程。

        早期误按 POST + JSON Body 发出，TikHub 会回 405/422，异常被吞掉后
        表现为「分析报告里没有评论区内容」。这里按官方 spec 修正。
        """
        empty = {"items": [], "cursor": 0, "has_more": False,
                 "cost_calls": 0, "provider": self.name}
        try:
            data = self._request("GET", self.COMMENTS_PATH,
                                 params={"aweme_id": aweme_id,
                                         "cursor": int(cursor), "count": 20})
        except ProviderError:
            return empty
        body = data.get("data") or {}
        rows = self._pick(body.get("comments"), body.get("comment_list"),
                          data.get("comments")) or []
        items = [{"text": c.get("text") or "", "digg_count": c.get("digg_count") or 0}
                 for c in rows if isinstance(c, dict) and c.get("text")]
        return {"items": items, "cursor": 0, "has_more": False,
                "cost_calls": 1, "provider": self.name}

    def health(self):
        if not self.api_key:
            return {"ok": False, "provider": self.name, "reason": "未配置 API Key"}
        return {"ok": True, "provider": self.name, "base_url": self.base_url}


_REGISTRY = {"mock": MockProvider, "tikhub": TikHubProvider}


def get_provider(name=None):
    """按配置返回 provider 实例。未指定时读 settings.search.provider。"""
    s = config.get_settings().get("search", {})
    name = (name or s.get("provider") or "mock").strip().lower()
    cls = _REGISTRY.get(name, MockProvider)
    if cls is TikHubProvider:
        t = s.get("tikhub", {})
        return cls(base_url=t.get("base_url", ""), api_key=t.get("api_key", ""),
                   timeout=t.get("timeout", 30))
    return cls()


def list_providers():
    s = config.get_settings().get("search", {})
    cur = s.get("provider") or "mock"
    out = []
    for key, cls in _REGISTRY.items():
        inst = get_provider(key)
        h = inst.health()
        out.append({"id": key, "label": cls.label, "current": key == cur, "ok": h.get("ok", False),
                    "reason": h.get("reason", "")})
    return out
