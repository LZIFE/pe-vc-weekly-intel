#!/usr/bin/env python3
"""中国TOP100私募股权GP动态周报 — China PE/VC GP Weekly Intelligence Report."""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import email.message
import html
import json
import os
import re
import shutil
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import cost_tracker

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
ENV_PATH = BASE_DIR / ".env"
OUT_JSON = BASE_DIR / "pe_vc_weekly_last_report.json"
OUT_HTML = BASE_DIR / "pe_vc_weekly_last_report.html"
OUT_JSON_BAK = BASE_DIR / "pe_vc_weekly_last_report.json.bak"
OUT_HTML_BAK = BASE_DIR / "pe_vc_weekly_last_report.html.bak"

REPORT_NAME = "中国TOP100私募股权GP动态周报"
DEFAULT_RECIPIENTS: list[str] = []  # override via --recipients CLI or RECIPIENTS env var


# ── PE/VC-specific RSS sources ──────────────────────────────────────────

RSS_SOURCES = [
    # 一级市场资讯
    {"name": "投资界", "url": "https://rsshub.app/pedaily/news", "category": "一级市场资讯"},
    {"name": "36氪创投", "url": "https://rsshub.app/36kr/motif/startup", "category": "一级市场资讯"},
    {"name": "36氪快讯", "url": "https://rsshub.app/36kr/news/latest", "category": "一级市场资讯"},
    # 财经媒体
    {"name": "财联社公司深度", "url": "https://rsshub.app/cls/depth/1005", "category": "财经媒体"},
    {"name": "财联社公告电报", "url": "https://rsshub.app/cls/telegraph/announcement", "category": "财经媒体"},
    {"name": "证券时报公司", "url": "https://rsshub.app/stcn/article/list/company", "category": "财经媒体"},
    {"name": "证券时报金融", "url": "https://rsshub.app/stcn/article/list/finance", "category": "财经媒体"},
    {"name": "新浪财经创投", "url": "https://rsshub.app/sina/finance/rollnews/2681", "category": "财经媒体"},
    {"name": "新浪财经股市", "url": "https://rsshub.app/sina/finance/rollnews/2671", "category": "财经媒体"},
    {"name": "21财经公司动态", "url": "https://rsshub.app/21caijing/channel/%E5%85%AC%E5%8F%B8/%E5%8A%A8%E6%80%81", "category": "财经媒体"},
    {"name": "21财经公告精选", "url": "https://rsshub.app/21caijing/channel/%E6%8A%95%E8%B5%84%E9%80%9A/%E5%85%AC%E5%91%8A%E7%B2%BE%E9%80%89", "category": "财经媒体"},
    {"name": "21财经公司洞察", "url": "https://rsshub.app/21caijing/channel/%E6%8A%95%E8%B5%84%E9%80%9A/%E5%85%AC%E5%8F%B8%E6%B4%9E%E5%AF%9F", "category": "财经媒体"},
    {"name": "财经网滚动新闻", "url": "https://rsshub.app/caijing/roll", "category": "财经媒体"},
    # 央媒/权威
    {"name": "人民网财经", "url": "https://rsshub.app/people/finance", "category": "央媒/权威"},
    {"name": "新华网财经", "url": "http://rss.xinhuanet.com/rss/fortune.xml", "category": "央媒/权威"},
]

HTML_SOURCES = [
    # Fund industry association public pages
    {"name": "中国基金业协会", "url": "https://gs.amac.org.cn/", "kind": "amac_list", "default_company": "行业/综合", "default_group": "行业动态"},
    # PE/VC industry pages (add as needed)
]

# ── PE/VC Competitive Intelligence Matrix ─────────────────────────────
# 7 dimensions from the user's framework, mapped to P0/P1/P2 priorities

MATRIX_RULES = [
    # ⭐⭐⭐ 战略级
    {
        "priority": "P0",
        "dimension": "基金募集动态",
        "label": "募资进展与资金来源",
        "keywords": [
            "首关", "终关", "目标规模", "认缴", "募资完成", "募资关闭",
            "新设基金", "管理规模", "出资人", "LP", "政府引导基金",
            "产业资本", "高净值", "机构投资者", "保险资金", "社保基金",
            "母基金", "FOF", "基金备案", "工商预核名", "基金路演",
            "管理人登记", "合伙制", "公司制", "存续期", "投资期",
            "管理费率", "业绩报酬", "carry", "超额收益",
        ],
    },
    {
        "priority": "P0",
        "dimension": "投资组合与交易动态",
        "label": "新增投资与项目退出",
        "keywords": [
            "新增投资", "投资项目", "领投", "跟投", "联合投资",
            "天使轮", "Pre-A", "A轮", "B轮", "C轮", "D轮",
            "融资", "估值", "投资金额", "投资轮次",
            "IPO", "上市", "过会", "申报", "首发", "科创板",
            "并购", "收购", "交易对价", "老股转让",
            "回购", "对赌", "退出", "减持", "减持退出",
            "投资节奏", "单笔投资", "行业集中度",
        ],
    },
    # ⭐⭐ 运营级
    {
        "priority": "P1",
        "dimension": "已投项目投后管理",
        "label": "项目经营与治理",
        "keywords": [
            "投后管理", "被投企业", "财务数据", "营收", "利润",
            "现金流", "后续融资", "估值预期",
            "董事会", "监事会", "一票否决权", "对赌条款",
            "创始人变更", "核心团队", "核心人员流失",
            "重大诉讼", "监管处罚", "经营异常", "风险事件",
            "破产", "清算", "停业", "违约",
        ],
    },
    {
        "priority": "P1",
        "dimension": "组织与团队建设",
        "label": "人事变动与组织架构",
        "keywords": [
            "合伙人", "董事总经理", "MD", "投资总监", "副总裁",
            "入职", "离职", "晋升", "任命", "辞职", "加盟",
            "团队扩张", "编制", "行业组", "新能源组", "硬科技组",
            "医疗健康组", "消费组", "TMT组", "招聘",
            "投资委员会", "投委会", "决策流程", "中后台",
            "组织架构", "部门调整", "裁员",
        ],
    },
    # ⭐ 品牌与生态
    {
        "priority": "P2",
        "dimension": "品牌与行业影响力",
        "label": "排名奖项与公开活动",
        "keywords": [
            "行业排名", "榜单", "获奖", "荣誉", "评选",
            "投中年会", "清科论坛", "行业峰会", "论坛",
            "主题演讲", "圆桌", "路演活动",
            "行业研究报告", "白皮书", "媒体专访",
            "品牌", "行业话语权", "入选榜单",
        ],
    },
    {
        "priority": "P2",
        "dimension": "战略动向与合作关系",
        "label": "战略合作与区域布局",
        "keywords": [
            "战略合作", "战略协议", "产业龙头", "科研院所",
            "区域办公室", "分支机构", "新设办公室",
            "地方政府", "产业基金", "区域基金",
            "新赛道", "行业专项基金", "赛道调整",
            "重点布局", "投资方向", "策略调整",
        ],
    },
    {
        "priority": "P2",
        "dimension": "合规与监管动态",
        "label": "监管检查与合规事件",
        "keywords": [
            "监管检查", "现场检查", "自律核查", "自查",
            "备案", "重大事项变更", "管理人变更",
            "行政处罚", "监管措施", "纪律处分",
            "证监局", "基金业协会", "中基协",
            "警示", "处罚", "整改", "处分",
            "异常机构", "经营异常", "注销", "撤销登记",
        ],
    },
]

PRIORITY_ORDER = {"P0": 0, "P1": 1, "P2": 2}
PRIORITY_STARS = {"P0": "⭐⭐⭐", "P1": "⭐⭐", "P2": "⭐"}
PRIORITY_NAMES = {
    "P0": "战略情报：募资与投资交易",
    "P1": "运营情报：投后管理与组织建设",
    "P2": "生态情报：品牌影响与合规动态",
}

REPORT_GROUP_ORDER = ["核心机构", "活跃机构", "观察名单", "行业动态"]
TIER_NAMES = {1: "核心机构", 2: "活跃机构", 3: "观察名单"}

EXTRA_COMPANY_ALIASES: dict[str, list[str]] = {
    "红杉中国": ["红杉资本", "红杉", "Sequoia China"],
    "IDG资本": ["IDG", "IDG Capital"],
    "深创投集团": ["深创投", "深圳创新投"],
    "高瓴投资": ["高瓴资本", "高瓴", "Hillhouse"],
    "达晨财智": ["达晨创投", "达晨投资"],
    "君联资本": ["君联"],
    "纪源资本": ["GGV", "GGV Capital"],
    "启明创投": ["启明"],
    "中金资本": ["中金"],
    "招商资本": ["招商局资本"],
    "CPE源峰": ["CPE", "源峰资本"],
    "鼎晖投资": ["鼎晖"],
    "腾讯投资": ["Tencent Investment"],
    "阿里资本": ["阿里巴巴投资", "阿里投资"],
    "淡马锡": ["Temasek"],
    "KKR": ["Kohlberg Kravis Roberts"],
    "摩根士丹利": ["Morgan Stanley", "摩根斯坦利"],
    "浦东科创/海望资本": ["浦东科创", "海望资本"],
    "建信(北京)投资": ["建信投资", "建信北京"],
    "Monolith砺思资本": ["Monolith", "砺思资本"],
    "LongRiver江远投资": ["LongRiver", "江远投资"],
    "L Catterton路威凯腾": ["L Catterton", "路威凯腾"],
    "LYFE Capital洲嶺资本": ["LYFE Capital", "洲嶺资本"],
    "星连资本(Z基金)": ["星连资本", "Z基金"],
    "德同资本": ["德同", "Detong Capital", "Detong", "dtcap", "邵俊", "田立新"],
    "联影医疗": ["联影", "United Imaging"],
    "禾赛科技": ["禾赛", "Hesai"],
    "伟测半导体": ["伟测"],
    "盟科药业": ["盟科"],
    "百融云创": ["百融云", "百融"],
    "微拍堂": ["微拍"],
    "轻松健康": ["轻松筹", "轻松集团"],
    "小电科技": ["小电"],
    "博睿康": ["博睿"],
    "微芯生物": ["微芯"],
    "影谱科技": ["影谱"],
    "无问芯穹": ["无问"],
    "灵心巧手": ["灵心"],
}

# Static aliases for company matching
STATIC_COMPANY_ALIASES: dict[str, tuple[str, str, str]] = {}

# Aliases that may cause false matches - skip when exclusion pattern is present
COMPANY_EXCLUSION: dict[str, list[str]] = {
    "中金资本": ["中金公司"],
}

CNINFO_LOW_VALUE_KEYWORDS: list[str] = []
CNINFO_HIGH_SIGNAL_KEYWORDS: list[str] = []

# ── Business action keywords for PE/VC ─────────────────────────────────

BUSINESS_ACTION_KEYWORDS = [
    # 基金募集
    "首关", "终关", "募资", "认缴", "目标规模", "新设基金",
    "基金备案", "管理人登记", "路演", "出资人",
    # 投资交易
    "新增投资", "领投", "跟投", "融资", "IPO", "上市",
    "并购", "收购", "退出", "回购", "对赌",
    # 投后
    "投后", "被投", "董事会", "创始人变更",
    # 人事
    "合伙人", "董事总经理", "入职", "离职", "晋升", "任命",
    # 品牌
    "榜单", "获奖", "排名", "论坛",
    # 战略
    "战略合作", "区域布局", "新赛道",
    # 合规
    "监管", "处罚", "备案", "检查", "处分", "警示",
]

NON_BUSINESS_DISCIPLINE_KEYWORDS = [
    "纪委", "监委", "审查调查", "被开除党籍", "双开",
    "严重违纪", "违法", "受贿", "贪污", "被查",
]

# ── Source scope classification ──────────────────────────────────────────

SOURCE_SCOPE_MAP: dict[str, str] = {}


def _build_source_scope_map() -> None:
    self_broadcast_names: set[str] = set()
    official_names: set[str] = {"中国基金业协会"}
    for src in RSS_SOURCES:
        name = src["name"]
        if name in official_names:
            SOURCE_SCOPE_MAP[name] = "official_disclosure"
        elif name in self_broadcast_names:
            SOURCE_SCOPE_MAP[name] = "self_broadcast"
        else:
            SOURCE_SCOPE_MAP[name] = "third_party"
    for src in HTML_SOURCES:
        SOURCE_SCOPE_MAP[src["name"]] = "official_disclosure"


_build_source_scope_map()


def source_scope(source_name: str, channel: str = "") -> str:
    if channel == "公告":
        return "official_disclosure"
    if channel in {"AI Search", "搜索RSS"}:
        return "third_party"
    return SOURCE_SCOPE_MAP.get(source_name, "third_party")


def infer_credibility(item: "IntelItem") -> str:
    """Return credibility level: 高/中高/中."""
    scope = source_scope(item.source, item.channel)
    if scope == "official_disclosure":
        return "高"
    if scope == "self_broadcast":
        return "中高"
    auth_sources = {"36氪", "投资界", "财联社", "证券时报", "新浪财经", "人民网", "新华网",
                    "21财经", "界面新闻", "财新", "中国证券报", "上海证券报"}
    if any(s in item.source for s in auth_sources):
        return "中高"
    return "中"


def infer_verification(item: "IntelItem") -> str:
    """Return verification notes based on what's missing."""
    notes: list[str] = []
    text = f"{item.title} {item.summary}"
    if item.dimension == "基金募集动态":
        if "首关" not in text and "终关" not in text and "备案" not in text:
            notes.append("基金备案号/首关金额待核实")
        if "LP" not in text and "出资" not in text:
            notes.append("LP出资结构待披露")
    elif item.dimension == "投资组合与交易动态":
        if "亿" not in text and "万" not in text and "美元" not in text:
            notes.append("投资金额待核实")
    elif item.dimension == "组织与团队建设":
        notes.append("人事变动详情待核实")
    elif item.dimension == "合规与监管动态":
        notes.append("监管措施详情待核实")
    return "；".join(notes)


def validate_url(url: str, timeout: int = 8) -> bool:
    """Check if a URL is reachable (HEAD request)."""
    if not url or not url.startswith(('http://', 'https://')):
        return False
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"}, method="HEAD")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 400
    except Exception:
        return False



# ── IntelItem dataclass ────────────────────────────────────────────────

@dataclass
class IntelItem:
    title: str
    url: str
    source: str
    published: str
    summary: str
    company: str
    company_group: str
    priority: str
    dimension: str
    matrix_label: str
    channel: str
    credibility: str = "中"  # 高/中高/中
    verification_notes: str = ""  # 待核实事项


# ── Utility functions ──────────────────────────────────────────────────

def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def clean_text(value: str | None) -> str:
    value = re.sub(r"<[^>]+>", " ", value or "")
    value = html.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    # Collapse whitespace between Chinese characters (e.g. "高 瓴" -> "高瓴")
    value = re.sub(r'(?<=[\u4e00-\u9fff\u3400-\u4dbf])\s+(?=[\u4e00-\u9fff\u3400-\u4dbf])', '', value)
    return value

def bold_company(text: str, company: str) -> str:
    """Wrap company name occurrences in text with <strong> tags for HTML display."""
    if not company or len(company) < 2:
        return text
    return re.sub(r'(%s)' % re.escape(company), r'<strong>\1</strong>', text, count=3)


def shorten(value: str, limit: int = 220) -> str:
    text = clean_text(value)
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def quote_url(url: str) -> str:
    return urllib.parse.quote(url, safe=":/?&=#%+")


def normalize_source_url(url: str) -> str:
    url = clean_text(url)
    if url.startswith("//"):
        url = "https:" + url
    return url


_url_verify_cache: dict[str, bool] = {}

def is_plausible_url(url: str) -> bool:
    """Check if a URL looks like a real, accessible link (not fabricated)."""
    if not url or len(url) < 20:
        return False
    if "/123456" in url:
        return False
    m = re.match(r"https?://([^/]+)", url)
    if not m:
        return False
    domain = m.group(1)
    if "localhost" in domain or "example" in domain:
        return False
    # Known trustworthy domains - skip HTTP verification
    _trusted_domains = {"finance.eastmoney.com", "www.36kr.com", "finance.sina.com.cn",
                        "www.stcn.com", "www.cls.cn", "finance.people.com.cn",
                        "rsshub.app", "rsshub.rssforever.com",
                        # AI Search (DeepSeek Anthropic) commonly returns these
                        "36kr.com", "pedaily.cn", "www.pedaily.cn",
                        "mp.weixin.qq.com", "chinaventure.com.cn",
                        "www.chinaventure.com.cn", "news.qq.com",
                        "www.jiemian.com", "m.jiemian.com",
                        "eastmoney.com", "www.eastmoney.com",
                        "sina.com.cn", "www.sina.com.cn",
                        "163.com", "www.163.com",
                        "lieyunpro.com", "m.lieyunpro.com",
                        "toutiao.com", "m.toutiao.com",
                        "alumni.fudan.edu.cn", "econ.fudan.edu.cn",
                        "gov.cn", "jingan.gov.cn", "beijihainews.com.cn",
                        "sohu.com", "www.sohu.com",
                        "ifeng.com", "www.ifeng.com",
                        "cn.chinadaily.com.cn", "www.chinadaily.com.cn",
                        "smarthey.com", "www.smarthey.com",
                        "eastfi.com", "www.eastfi.com",
                        "guba.eastmoney.com", "wap.eastmoney.com",
                        "pe.pedaily.cn", "fund.eastmoney.com",
                        "xueqiu.com", "www.xueqiu.com",
                        "tencent.com", "www.tencent.com",
                        "eefocus.com", "www.eefocus.com",
                        "laoyaoba.com", "www.laoyaoba.com",
    }
    if domain in _trusted_domains:
        return True
    # For other domains, do a quick HEAD request to verify accessibility
    if url in _url_verify_cache:
        return _url_verify_cache[url]
    try:
        ctx = ssl.create_default_context()
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
            ok = resp.status < 400
    except Exception:
        ok = False
    _url_verify_cache[url] = ok
    return ok


def source_authority_score(item: "IntelItem") -> int:
    """0 = official/disclosure, 1 = authoritative media, 2 = supplemental."""
    scope = source_scope(item.source, item.channel)
    if scope == "official_disclosure":
        return 0
    authoritative_hosts = {
        "people.com.cn", "xinhuanet.com", "cctv.com",
        "stcn.com", "cs.com.cn", "cnstock.com",
        "21jingji.com", "cls.cn", "pedaily.cn",
        "36kr.com", "sina.com.cn", "eastmoney.com",
    }
    try:
        from urllib.parse import urlparse
        host = urlparse(item.url).hostname or ""
        if any(auth in host for auth in authoritative_hosts):
            return 1
    except Exception:
        pass
    return 2 if scope == "third_party" else 1


def report_group(item: "IntelItem") -> str:
    group = item.company_group or "行业动态"
    if group in TIER_NAMES.values():
        return group
    return "行业动态"


def date_sort_value(value: str) -> int:
    try:
        d = dt.date.fromisoformat(value)
        return d.toordinal()
    except Exception:
        return 0


def is_business_action(text: str) -> bool:
    return any(kw in text for kw in BUSINESS_ACTION_KEYWORDS)


def is_discipline_gossip(text: str) -> bool:
    return any(kw in text for kw in NON_BUSINESS_DISCIPLINE_KEYWORDS)


# ── HTTP helpers ───────────────────────────────────────────────────────

_HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "20"))
_RSS_RETRIES = int(os.environ.get("RSS_RETRIES", "3"))
_RSSHUB_BASE = os.environ.get("RSSHUB_BASE_URL", "https://rsshub.rssforever.com").rstrip("/")
_RSSHUB_FALLBACK = os.environ.get("RSSHUB_FALLBACK_URL", "https://rsshub.app").rstrip("/")
# Additional RSSHub mirrors for 403 fallback
_RSSHUB_MIRRORS = [
    m.strip()
    for m in os.environ.get("RSSHUB_MIRRORS", "https://rsshub.bili.xyz,https://rsshub.vercel.app").split(",")
    if m.strip()
]


def http_get(url: str, timeout: int | None = None, retries: int = 1) -> bytes:
    if timeout is None:
        timeout = _HTTP_TIMEOUT
    last_exc: Exception | None = None
    for attempt in range(1 + retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except Exception as exc:
            last_exc = exc
            if attempt < retries:
                sleep_time = 2 * (2 ** attempt)  # exponential backoff: 2s, 4s, 8s
                time.sleep(sleep_time)
    raise last_exc  # type: ignore[misc]


def http_get_with_fallback(
    primary_url: str,
    fallback_url: str | None = None,
    timeout: int | None = None,
    retries: int = 3,
) -> bytes:
    """Fetch with retries on primary, then fallback to alternative URLs."""
    if timeout is None:
        timeout = _HTTP_TIMEOUT
    last_exc: Exception | None = None
    # Try primary
    try:
        return http_get(primary_url, timeout=timeout, retries=retries)
    except Exception as exc:
        last_exc = exc
    # Try fallback (if provided and different)
    if fallback_url and fallback_url != primary_url:
        try:
            return http_get(fallback_url, timeout=timeout, retries=retries)
        except Exception as exc:
            last_exc = exc
    # Try additional mirrors
    for mirror in _RSSHUB_MIRRORS:
        if mirror in primary_url or mirror in (fallback_url or ""):
            continue
        mirror_url = primary_url.replace(_RSSHUB_BASE, mirror).replace(_RSSHUB_FALLBACK, mirror)
        if mirror_url == primary_url and fallback_url:
            mirror_url = fallback_url.replace(_RSSHUB_FALLBACK, mirror).replace(_RSSHUB_BASE, mirror)
        if mirror_url == primary_url or mirror_url == fallback_url:
            continue
        try:
            return http_get(mirror_url, timeout=timeout, retries=retries)
        except Exception as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    raise RuntimeError(f"所有镜像均失败：{primary_url}")


def http_post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: int = 180) -> dict[str, Any]:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={**headers, "Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── Date parsing ───────────────────────────────────────────────────────

def parse_date(value: str | None, tz: ZoneInfo) -> dt.datetime | None:
    if not value:
        return None
    value = clean_text(value)
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                "%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                "%Y年%m月%d日"):
        try:
            if fmt == "%Y年%m月%d日":
                nums = re.findall(r"\d+", value)
                if len(nums) == 3:
                    value_fixed = f"{nums[0]}-{nums[1].zfill(2)}-{nums[2].zfill(2)}"
                    return dt.datetime.strptime(value_fixed, "%Y-%m-%d").replace(tzinfo=tz)
                continue
            return dt.datetime.strptime(value, fmt).replace(tzinfo=tz) if "%z" not in fmt and "%Z" not in fmt else dt.datetime.strptime(value, fmt).astimezone(tz)
        except ValueError:
            continue
    try:
        parsed = parsedate_to_datetime(value)
        if parsed:
            return parsed.astimezone(tz)
    except Exception:
        pass
    return None


# ── Config helpers ─────────────────────────────────────────────────────

def load_config() -> dict[str, Any]:
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def iter_companies(config: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    companies: list[tuple[str, dict[str, str]]] = []
    for group, rows in config.get("categories", {}).items():
        for row in rows:
            companies.append((group, row))
    return companies


def iter_unique_companies(config: dict[str, Any]) -> list[tuple[str, dict[str, str]]]:
    seen: set[str] = set()
    output: list[tuple[str, dict[str, str]]] = []
    for group, company in iter_companies(config):
        key = company.get("name") or ""
        if not key or key in seen:
            continue
        seen.add(key)
        output.append((group, company))
    limit = int(os.environ.get("AI_SEARCH_COMPANY_LIMIT", "0"))
    if limit > 0:
        return output[:limit]
    return output


def company_aliases(config: dict[str, Any]) -> dict[str, tuple[str, str, str]]:
    aliases: dict[str, tuple[str, str, str]] = {}
    for group, company in iter_companies(config):
        name = company.get("name", "")
        tier = company.get("tier", 3)
        if name:
            tier_name = TIER_NAMES.get(tier, "观察名单")
            aliases[name] = (name, tier_name, "")
            for alias in EXTRA_COMPANY_ALIASES.get(name, []):
                aliases[alias] = (name, tier_name, "")
    for alias_name, (canonical, group_name, _code) in STATIC_COMPANY_ALIASES.items():
        aliases[alias_name] = (canonical, group_name, "")
    return aliases


# ── Classification ─────────────────────────────────────────────────────

def classify_matrix(text: str) -> tuple[str, str, str] | None:
    lower = text.lower()
    best: tuple[int, str, str, str] | None = None
    for rule in MATRIX_RULES:
        score = sum(1 for keyword in rule["keywords"] if keyword.lower() in lower)
        if score > 0 and (best is None or score > best[0]):
            best = (score, rule["priority"], rule["dimension"], rule["label"])
    if best is None:
        return None
    return best[1], best[2], best[3]


def refine_classification(
    priority: str, dimension: str, label: str, text: str,
    company: str = "", scope: str = "third_party",
) -> tuple[str, str, str]:
    if priority in ("P0", "P1") and not is_business_action(text):
        return "P2", dimension, PRIORITY_NAMES["P2"]
    if priority in ("P0", "P1") and (not company or company in ("行业/综合", "行业")):
        return "P2", dimension, PRIORITY_NAMES["P2"]
    return priority, dimension, label


def normalize_priority(value: str) -> str:
    value = clean_text(value)
    # Count star emoji — handle any number of ⭐
    star_count = value.count("⭐")
    if star_count >= 3 or value in {"P0", "三星", "最高"}:
        return "P0"
    if star_count == 2 or value in {"P1", "二星", "中"}:
        return "P1"
    if star_count == 1 or value in {"P2", "一星", "低"}:
        return "P2"
    return ""


# ── RSS parsing ────────────────────────────────────────────────────────

def parse_rss_items(
    content: bytes, source: dict[str, str], tz: ZoneInfo,
    aliases: dict[str, tuple[str, str, str]], start: dt.date,
    lenient: bool = False,
) -> list[IntelItem]:
    items: list[IntelItem] = []
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return items

    source_name = source.get("name", "")

    def _build_item(title: str, url: str, summary: str, published: str) -> IntelItem | None:
        title = clean_text(title)
        url = normalize_source_url(url)
        summary = clean_text(summary)
        published = clean_text(published)
        if not title or not url:
            return None
        published_dt = parse_date(published, tz)
        if published_dt and published_dt.date() < start:
            return None
        company = infer_company(f"{title} {summary}", aliases, source_name)
        if not company:
            # In lenient mode for generic RSS, keep PE/VC-relevant news as 行业动态
            if not lenient:
                return None
            text_lower = (title + summary).lower()
            pevc_signals = ["融资", "基金", "募资", "创投", "股权投资", "私募", "IPO", "上市",
                          "并购", "GP", "LP", "VC", "PE", "投资", "退出", "首关", "终关"]
            if any(s in text_lower for s in pevc_signals):
                company = "行业/综合"
            else:
                return None
        text = f"{title} {summary}"
        classified = classify_matrix(text)
        if classified:
            priority, dimension, label = classified
        else:
            # Lenient mode: allow items through even without strong matrix keyword matches
            if not lenient:
                return None
            priority = "P2"
            dimension = "品牌与行业影响力"
            label = PRIORITY_NAMES.get("P2", "品牌声量与常态化市场监测")
        scope = source_scope(source_name)
        priority, dimension, label = refine_classification(
            priority, dimension, label, text, company, scope
        )
        company_group = aliases.get(company, (company, "行业动态", ""))[1]
        return IntelItem(
            title=title, url=url, source=source_name,
            published=published_dt.date().isoformat() if published_dt else published,
            summary=shorten(summary, 220),
            company=company, company_group=company_group,
            priority=priority, dimension=dimension, matrix_label=label,
            channel="RSS",
        )

    # Try Atom format first (standard for most RSSHub feeds)
    atom_ns = "http://www.w3.org/2005/Atom"
    atom_entries = list(root.iter(f"{{{atom_ns}}}entry"))
    if atom_entries:
        for entry in atom_entries:
            link_el = entry.find(f"{{{atom_ns}}}link")
            url = link_el.get("href", "") if link_el is not None else ""
            title = entry.findtext(f"{{{atom_ns}}}title", "")
            summary = (
                entry.findtext(f"{{{atom_ns}}}summary", "")
                or entry.findtext(f"{{{atom_ns}}}content", "")
            )
            published = entry.findtext(f"{{{atom_ns}}}published", "")
            item = _build_item(title, url, summary, published)
            if item:
                items.append(item)
        return items

    # Fallback to RSS 2.0 format (rss.xinhuanet.com etc.)
    for item in root.iter("item"):
        url = item.findtext("link", "")
        title = item.findtext("title", "")
        description = item.findtext("description", "")
        content_enc = item.findtext("{http://purl.org/rss/1.0/modules/content/}encoded", "")
        dc_date = item.findtext("{http://purl.org/dc/elements/1.1/}date", "")
        summary = content_enc or description
        pubdate = item.findtext("pubDate", "") or dc_date
        item_obj = _build_item(title, url, summary, pubdate)
        if item_obj:
            items.append(item_obj)

    return items


def infer_company(
    text: str, aliases: dict[str, tuple[str, str, str]],
    source_name: str = "",
) -> str:
    """Find the first matching company name from aliases in text."""
    text_lower = text.lower()
    # Score by longest match first
    candidates: list[tuple[int, str]] = []
    for alias, (canonical, _group, _code) in aliases.items():
        if alias.lower() in text_lower:
            candidates.append((len(alias), canonical))
    if not candidates:
        return ""
    candidates.sort(key=lambda x: (-x[0], x[1]))
    # Check exclusions: if canonical has exclusion patterns that match text, skip
    best_canonical = candidates[0][1]
    if best_canonical in COMPANY_EXCLUSION:
        text_lower = text.lower()
        for excl_pattern in COMPANY_EXCLUSION[best_canonical]:
            if excl_pattern.lower() in text_lower and best_canonical.lower() not in text_lower:
                if len(candidates) > 1:
                    return candidates[1][1]
                return ''
    return best_canonical


# ── RSS fetching ───────────────────────────────────────────────────────

def fetch_rss(config: dict[str, Any], start: dt.date, tz: ZoneInfo) -> tuple[list[IntelItem], list[str]]:
    items: list[IntelItem] = []
    failures: list[str] = []
    aliases = company_aliases(config)
    rsshub_base = _RSSHUB_BASE
    rsshub_fallback = _RSSHUB_FALLBACK
    # Determine if this source is RSSHub-based and needs primary/fallback URLs
    for src in config.get("rss_sources", RSS_SOURCES):
        url = src["url"]
        name = src["name"]
        # For RSSHub URLs, try primary then fallback
        fallback_url = None
        if rsshub_base in url and rsshub_fallback != rsshub_base:
            fallback_url = url.replace(rsshub_base, rsshub_fallback)
        try:
            raw = http_get_with_fallback(url, fallback_url=fallback_url, timeout=_HTTP_TIMEOUT, retries=_RSS_RETRIES)
            batch = parse_rss_items(raw, src, tz, aliases, start, lenient=True)
            items.extend(batch)
        except Exception as exc:
            failures.append(f"RSS（{name}）拉取失败：{exc}")
        time.sleep(1.5)  # avoid RSSHub rate-limit
    return items, failures


# ── Targeted company search (RSSHub-based) ────────────────────────────

def fetch_company_search(config: dict[str, Any], start: dt.date, tz: ZoneInfo) -> tuple[list[IntelItem], list[str]]:
    """Search for each tracked company using RSSHub eastmoney search."""
    if os.environ.get("ENABLE_TARGETED_RSS_SEARCH", "1") != "1":
        return [], []
    aliases = company_aliases(config)
    rsshub_base = _RSSHUB_BASE
    rsshub_fallback = _RSSHUB_FALLBACK
    targets = iter_unique_companies(config)
    limit = int(os.environ.get("TARGETED_RSS_COMPANY_LIMIT", "0"))
    if limit > 0:
        targets = targets[:limit]
    search_timeout = int(os.environ.get("SEARCH_HTTP_TIMEOUT", "30"))
    search_delay = int(os.environ.get("SEARCH_DELAY", "3"))
    items: list[IntelItem] = []
    failures: list[str] = []
    for idx, (_group, company) in enumerate(targets):
        name = company.get("name", "")
        if not name:
            continue
        keyword = urllib.parse.quote(name)
        url = f"{rsshub_base}/eastmoney/search/{keyword}"
        fallback_url = f"{rsshub_fallback}/eastmoney/search/{keyword}" if rsshub_fallback != rsshub_base else None
        source = {"name": f"东方财富搜索：{name}", "url": url}
        try:
            raw = http_get_with_fallback(url, fallback_url=fallback_url, timeout=search_timeout, retries=2)
            batch = parse_rss_items(raw, source, tz, aliases, start, lenient=True)
            for item in batch:
                # Accept if the item's text directly mentions the search target (or its aliases)
                # This handles cases where infer_company picks a different GP mentioned alongside
                text = f"{item.title} {item.summary}"
                target_aliases = {name} | {a for a, (c, _, _) in aliases.items() if c == name}
                mentioned_target = any(a.lower() in text.lower() for a in target_aliases if len(a) >= 2)
                if mentioned_target:
                    item.company = name  # force correct company
                    item.channel = "搜索RSS"
                    items.append(item)
                    continue
                if item.company == name:
                    pass  # exact match
                elif item.company in aliases:
                    canonical = aliases[item.company][0]
                    if canonical != name:
                        continue  # matched a different company
                else:
                    continue  # no match
                item.channel = "搜索RSS"
                items.append(item)
        except Exception as exc:
            failures.append(f"东方财富搜索RSS（{name}）拉取失败：{exc}")
        if idx < len(targets) - 1:
            time.sleep(search_delay)
    return items, failures


# ── Announcement content fetching ──────────────────────────────────────

def fetch_article_content(url: str, timeout: int = 15) -> str:
    """Fetch full article content from a URL and extract key information.

    Used for announcement-type articles to enrich summary with key details.
    """
    try:
        raw = http_get(url, timeout=timeout, retries=2)
        html_text = raw.decode("utf-8", errors="replace")
        # Strip HTML tags, keep text
        text = re.sub(r"<[^>]+>", " ", html_text)
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        # Try to extract meaningful content (skip nav, footer noise)
        # Take the middle portion of the page text (skip header/footer)
        if len(text) > 500:
            # Find the main content area - look for the longest continuous text block
            paragraphs = re.split(r"\n\s*\n", text)
            main_content = max(paragraphs, key=len).strip() if paragraphs else text
            # Limit to key info
            return main_content[:800].strip()
        return text[:800].strip()
    except Exception:
        return ""


def enrich_announcement_items(items: list[IntelItem]) -> list[IntelItem]:
    """For items from official/disclosure sources, fetch full content to enrich summaries."""
    if not os.environ.get("ENABLE_ANNOUNCEMENT_ENRICH", "1") == "1":
        return items
    enriched = []
    for item in items:
        scope = source_scope(item.source, item.channel)
        if scope == "official_disclosure" and item.url and len(item.url) > 10:
            full_text = fetch_article_content(item.url)
            if full_text:
                # Enrich summary with key details from full text
                existing = item.summary
                # If existing summary is short, append key info from full text
                if len(existing) < 100:
                    # Extract key sentences: look for amounts, dates, company names
                    key_sentences = []
                    for sent in re.split(r"[。！？\n]", full_text):
                        sent = sent.strip()
                        if any(kw in sent for kw in ["亿元", "万元", "融资", "投资", "收购",
                                                       "IPO", "上市", "基金", "募资",
                                                       "合伙人", "战略", "合作"]):
                            if len(sent) > 10 and sent not in existing:
                                key_sentences.append(sent)
                    if key_sentences:
                        extra = "；".join(key_sentences[:3])
                        item.summary = shorten(f"{existing} {extra}", 400)
            enriched.append(item)
        else:
            enriched.append(item)
    return enriched


# ── AI Search batch intel (DeepSeek Anthropic with web search) ──────────

def ai_search_chat(messages: list[dict[str, Any]], temperature: float = 0.3) -> dict[str, Any]:
    """Call DeepSeek via Anthropic Messages API — guarantees web_search execution."""
    api_key = os.environ.get("AI_SEARCH_API_KEY", "")
    base_url = os.environ.get("AI_SEARCH_BASE_URL", "https://api.deepseek.com/anthropic").rstrip("/")
    model = os.environ.get("AI_SEARCH_MODEL", "deepseek-v4-flash")
    if not api_key:
        raise RuntimeError("缺少 AI_SEARCH_API_KEY")
    url = f"{base_url}/v1/messages"
    timeout = int(os.environ.get("AI_HTTP_TIMEOUT", "3600"))

    # Convert to Anthropic format
    anthropic_msgs: list[dict[str, Any]] = []
    system_content = ""
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            system_content = content
        else:
            anthropic_msgs.append({"role": role, "content": content})

    body: dict[str, Any] = {
        "model": model,
        "messages": anthropic_msgs,
        "max_tokens": 16384,
        "tools": [{"name": "web_search", "type": "web_search_20250305"}],
    }
    if system_content:
        body["system"] = system_content

    def _do_request(msgs: list[dict], tool_use_ids_seen: set | None = None) -> dict:
        b = {**body, "messages": msgs}
        data = json.dumps(b).encode("utf-8")
        req = urllib.request.Request(
            url, data=data,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST",
        )
        resp_data: dict[str, Any] = {}
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp_data = json.loads(resp.read().decode("utf-8"))
        return resp_data

    # Up to 2 retries when model fails to use web_search
    for attempt in range(3):
        resp_data = _do_request(anthropic_msgs)
        content_blocks = resp_data.get("content", [])

        # Check for proper tool_use blocks (model actually using web_search)
        tool_uses = [b for b in content_blocks if b.get("type") == "tool_use"]

        if tool_uses:
            # Model properly used web_search — handle the tool_use → tool_result loop
            anthropic_msgs.append({"role": "assistant", "content": content_blocks})
            tool_results = []
            for tu in tool_uses:
                if tu.get("name") == "web_search":
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": tu.get("id", ""),
                        "content": tu.get("input", {}).get("query", ""),
                    })
            anthropic_msgs.append({"role": "user", "content": tool_results})
            # Get final answer with search results
            resp_data = _do_request(anthropic_msgs)
            break

        # Check for inline <tool_calls> text (model failed to execute)
        text_blocks = [b for b in content_blocks if b.get("type") == "text"]
        combined_text = "\n".join(b.get("text", "") for b in text_blocks)

        if "<tool_calls>" in combined_text or "<invoke" in combined_text:
            # Model described tool calls instead of executing — retry with stronger prompt
            if attempt < 2:
                anthropic_msgs[0]["content"] = (
                    "CRITICAL: You MUST use the web_search tool. Do NOT output <tool_calls> in text. "
                    "Execute the web_search tool to find real, verifiable information with URLs. "
                    + anthropic_msgs[0]["content"]
                )
                continue
        break  # Got real results or exhausted retries

    # Extract text from final response
    text_parts = []
    for block in resp_data.get("content", []):
        if block.get("type") == "text":
            text_parts.append(block.get("text", ""))
    full_text = "\n".join(text_parts)

    result = {
        "choices": [{"message": {"content": full_text}}],
        "usage": resp_data.get("usage", {}),
    }
    try:
        cost_tracker.log_api_call(model, resp_data.get("usage", {}), status="success")
    except Exception:
        pass
    return result


# ── DeepSeek for final writing / analysis (no web search) ──────────────

def deepseek_chat(messages: list[dict[str, Any]], temperature: float = 0.3) -> dict[str, Any]:
    """Call DeepSeek for text generation/analysis — no web search."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://ai.ctaigw.cn/v1").rstrip("/")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    if not api_key:
        raise RuntimeError("缺少 DEEPSEEK_API_KEY")
    url = f"{base_url}/chat/completions"
    timeout = int(os.environ.get("AI_HTTP_TIMEOUT", "3600"))
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": 8192,
        "stream": False,
    }
    try:
        result = http_post_json(url, body, {"Authorization": f"Bearer {api_key}"}, timeout=timeout)
        usage = result.get("usage", {}) if isinstance(result, dict) else {}
        cost_tracker.log_api_call(model, usage, status="success")
        return result
    except Exception as e:
        cost_tracker.log_api_call(model, {}, status="error", error_msg=str(e))
        raise


def make_batch_analysis_prompt(
    category: str,
    companies: list[dict[str, str]],
    start: dt.date,
    end: dt.date,
    provider: str,
) -> str:
    """Build a batch prompt for AI to analyze multiple companies at once."""
    names = "、".join(c["name"] for c in companies)
    start_cn = f"{start.year}年{start.month}月{start.day}日"
    end_cn = f"{end.year}年{end.month}月{end.day}日"
    return f"请全面搜索{names}在{start_cn}到{end_cn}的所有动态信息：投资事件、募资、基金设立、退出、人事变动、行业活动等。每条都要带原文链接。"


def ai_row_to_item(
    row: dict[str, Any],
    provider: str,
    category: str,
    batch_companies: list[dict[str, str]],
    start: dt.date,
    tz: ZoneInfo,
    aliases: dict[str, tuple[str, str, str]] | None = None,
) -> IntelItem | None:
    title = clean_text(str(row.get("title", "")))
    url = normalize_source_url(str(row.get("url", "")))
    summary = shorten(str(row.get("summary", "")), 220)
    published = clean_text(str(row.get("published", "")))
    company = clean_text(str(row.get("company", "")))
    dimension = clean_text(str(row.get("dimension", "")))
    source = clean_text(str(row.get("source", provider))) or provider
    if not company:
        return None
    # Check if company is in this batch
    batch_names = {c["name"] for c in batch_companies}
    if company not in batch_names:
        return None
    # Skip "no info" placeholders
    if "暂未发现公开动态" in title or "暂未发现" in title:
        return None
    if not title or not url:
        return None
    if not is_plausible_url(url):
        return None
    published_dt = parse_date(published, tz)
    if published_dt and published_dt.date() < start:
        return None
    text = f"{title} {summary} {dimension}"
    classified = classify_matrix(text)
    priority = normalize_priority(str(row.get("priority", "")))
    if classified:
        priority = priority or classified[0]
        dimension = dimension or classified[1]
        label = classified[2]
    else:
        if not priority:
            return None
        label = PRIORITY_NAMES.get(priority, "品牌声量与常态化市场监测")
    if dimension not in {rule["dimension"] for rule in MATRIX_RULES}:
        if classified:
            dimension = classified[1]
        else:
            return None
    priority, dimension, label = refine_classification(
        priority, dimension, label, text, company, "third_party"
    )
    # Map company to group
    if aliases is None:
        aliases = company_aliases(load_config())
    company_group = aliases.get(company, (company, "行业动态", ""))[1]
    return IntelItem(
        title=title, url=url, source=source,
        published=published_dt.date().isoformat() if published_dt else published,
        summary=summary, company=company,
        company_group=company_group,
        priority=priority, dimension=dimension,
        matrix_label=label, channel=provider,
    )


def fetch_ai_search_batch_intel(
    config: dict[str, Any], start: dt.date, end: dt.date, tz: ZoneInfo,
) -> tuple[list[IntelItem], list[str]]:
    """Batch intel retrieval via AI search — tier 1 only."""
    if not os.environ.get("AI_SEARCH_API_KEY"):
        return [], ["AI Search 跳过：缺少 AI_SEARCH_API_KEY"]
    items: list[IntelItem] = []
    failures: list[str] = []
    batch_size = int(os.environ.get("BATCH_SIZE", "8"))
    delay = int(os.environ.get("BATCH_SEARCH_DELAY", "2"))
    model_name = os.environ.get("AI_SEARCH_MODEL", "deepseek-v4-flash")

    targets = iter_unique_companies(config)
    # Only tier 1 (核心机构) uses AI; tier 2+ rely on RSS
    ai_targets = [(g, c) for g, c in targets if c.get("tier", 3) == 1]

    total = len(ai_targets)
    if not total:
        return [], []
    print(f"AI Search Batch: {total} 家公司（tier 1），批次大小 {batch_size}", flush=True)

    # Group by category then batch
    category_map: dict[str, list[dict[str, str]]] = {}
    for group, company in ai_targets:
        category_map.setdefault(group, []).append(company)

    batch_index = 0
    batch_aliases = company_aliases(config)
    for category, companies in category_map.items():
        for i in range(0, len(companies), batch_size):
            batch = companies[i:i + batch_size]
            batch_index += 1
            names_str = "、".join(c["name"] for c in batch)
            print(f"AI Search 批次 {batch_index}/{total // batch_size + 1}：{names_str}", flush=True)
            messages: list[dict[str, Any]] = [
                {"role": "user", "content": make_batch_analysis_prompt(category, batch, start, end, model_name)},
            ]
            try:
                completion = ai_search_chat(messages)
                content = completion["choices"][0]["message"].get("content", "")
                # Step 1: extract JSON from the narrative response
                json_text = None
                code_match = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", content)
                if code_match:
                    json_text = code_match.group(1)
                else:
                    json_match = re.search(r"\[[\s\S]*?\]", content)
                    if json_match:
                        json_text = json_match.group(0)

                # Step 2: if no JSON found, use telecom DeepSeek to convert narrative text to JSON
                if not json_text and content and len(content) > 50:
                    try:
                        conv_msgs = [
                            {"role": "system", "content": "把下面的搜索结果转换为JSON数组。字段：company/title/url/source/published/summary/dimension/priority。dimension选：基金募集动态/投资组合与交易动态/已投项目投后管理/组织与团队建设/品牌与行业影响力/战略动向与合作关系/合规与监管动态。priority选P0(募资投资)/P1(投后人事)/P2(品牌合规)。只输出```json代码块。"},
                            {"role": "user", "content": content[:8000]},
                        ]
                        conv_result = deepseek_chat(conv_msgs, temperature=0.1)
                        conv_content = conv_result["choices"][0]["message"].get("content", "")
                        code_match = re.search(r"```(?:json)?\s*(\[[\s\S]*?\])\s*```", conv_content)
                        if code_match:
                            json_text = code_match.group(1)
                        else:
                            json_match = re.search(r"\[[\s\S]*?\]", conv_content)
                            if json_match:
                                json_text = json_match.group(0)
                    except Exception as e:
                        failures.append(f"AI Search JSON转换失败（{names_str}）：{e}")
                        continue

                if not json_text:
                    failures.append(f"AI Search（{names_str}）未返回 JSON 数组")
                    continue
                try:
                    rows = json.loads(json_text)
                except json.JSONDecodeError:
                    failures.append(f"AI Search（{names_str}）JSON 解析失败")
                    continue
                accepted = 0
                for row in rows:
                    item = ai_row_to_item(row, "AI Search", category, batch, start, tz, batch_aliases)
                    if item:
                        items.append(item)
                        accepted += 1
                if rows and accepted == 0:
                    failures.append(f"AI Search（{names_str}）返回 {len(rows)} 条，但未通过校验")
            except Exception as exc:
                failures.append(f"AI Search（{names_str}）拉取失败：{exc}")
            time.sleep(delay)
    return items, failures


# ── AMAC filing check (Chinese fund industry association) ──────────────

def fetch_amac_filing(
    config: dict[str, Any], start: dt.date, tz: ZoneInfo,
) -> tuple[list[IntelItem], list[str]]:
    """Fetch filings/disciplines from AMAC (基金业协会).
    Default: returns empty, rely on AI search as fallback."""
    return [], ["AMAC 直接接口暂不可用，降级至 AI 联网检索补充"]


# ── Dedup & validation ─────────────────────────────────────────────────

def validate_item(item: IntelItem, aliases: dict[str, tuple[str, str, str]]) -> tuple[bool, str]:
    """Pre-dedupe gate: return (keep, reason)."""
    text = f"{item.title} {item.summary}"
    scope = source_scope(item.source, item.channel)
    # Self-broadcast without business action → discard
    if scope == "self_broadcast" and not is_business_action(text):
        return False, "自播发源缺乏业务信号词"
    # Discipline gossip with no business signal → discard
    if is_discipline_gossip(text) and not is_business_action(text):
        return False, "纪律审查类非经营动态"
    # URL must look plausible (not fabricated)
    if not is_plausible_url(item.url):
        return False, f"URL不可信或为占位符：{item.url[:60]}"
    # Company must be in aliases
    if item.company not in aliases and item.company not in {"行业/综合", "行业"}:
        return False, f"公司不在监控名单中：{item.company}"
    return True, ""


def dedupe(items: list[IntelItem]) -> list[IntelItem]:
    """Deduplicate and merge items that describe the same event.

    Strategy:
    1. Exact URL dedup (highest priority)
    2. Title similarity within same company (merge similar items, keep best info)
    3. Keep the item with the most complete summary when merging
    """
    # Phase 1: exact URL dedup
    seen_urls: dict[str, IntelItem] = {}
    for item in items:
        if item.url:
            existing = seen_urls.get(item.url)
            if existing:
                # Merge: keep longer summary and higher priority
                if len(item.summary) > len(existing.summary):
                    existing.summary = item.summary
                if PRIORITY_ORDER.get(item.priority, 9) < PRIORITY_ORDER.get(existing.priority, 9):
                    existing.priority = item.priority
                    existing.matrix_label = item.matrix_label
                continue
            seen_urls[item.url] = item

    # Phase 2: title similarity dedup within same company
    from difflib import SequenceMatcher

    def title_similarity(a: str, b: str) -> float:
        a_clean = clean_text(a)[:50]
        b_clean = clean_text(b)[:50]
        return SequenceMatcher(None, a_clean, b_clean).ratio()

    url_deduped = list(seen_urls.values())
    merged: list[IntelItem] = []
    used: set[int] = set()

    for i, a in enumerate(url_deduped):
        if i in used:
            continue
        # Look for similar items from the same company
        best = a
        used.add(i)
        for j, b in enumerate(url_deduped):
            if j in used or i == j:
                continue
            if a.company and b.company and a.company != b.company:
                continue
            if title_similarity(a.title, b.title) >= 0.80:
                used.add(j)
                # Merge: keep longer summary, higher priority, better credibility
                if len(b.summary) > len(best.summary):
                    best.summary = b.summary
                if PRIORITY_ORDER.get(b.priority, 9) < PRIORITY_ORDER.get(best.priority, 9):
                    best.priority = b.priority
                    best.matrix_label = b.matrix_label
                if b.credibility in ("高", "中高") and best.credibility not in ("高", "中高"):
                    best.credibility = b.credibility
                # Keep the more complete title
                if len(b.title) > len(best.title):
                    best.title = b.title
        merged.append(best)

    return merged


def sort_items(items: list[IntelItem]) -> list[IntelItem]:
    def sort_key(item: IntelItem) -> tuple:
        group_order = REPORT_GROUP_ORDER.index(report_group(item)) if report_group(item) in REPORT_GROUP_ORDER else 99
        prio = PRIORITY_ORDER.get(item.priority, 9)
        date_val = -date_sort_value(item.published)
        return (group_order, prio, date_val)
    return sorted(items, key=sort_key)


# ── Report building (HTML email) ───────────────────────────────────────

def start_lookback_days(config: dict[str, Any]) -> int:
    """Return the lookback window in days (from env or config)."""
    return int(os.environ.get("SEARCH_DAYS", str(config.get("lookback_days", 30))))



def generate_trend_analysis(items: list[IntelItem], failures: list[str]) -> str:
    """Use DeepSeek to analyze trends from collected items (analysis only, no web search)."""
    if not items or not os.environ.get("DEEPSEEK_API_KEY"):
        return ""
    # Build a condensed summary of all items for analysis
    lines = []
    for item in items[:50]:
        lines.append(f"[{item.company}] {item.dimension}: {item.title[:80]}")
    summary_text = "\n".join(lines)
    messages = [
        {"role": "system", "content": "你是私募基金竞争情报分析师。根据提供的情报条目，归纳3-5条趋势观察。每条不超过80字，直接输出要点，不要编号或解释。"},
        {"role": "user", "content": f"以下是本周采集的一级市场私募基金管理人竞争情报：\n{summary_text}\n\n请归纳3-5条趋势观察。"},
    ]
    try:
        completion = deepseek_chat(messages, temperature=0.3)
        return completion["choices"][0]["message"].get("content", "").strip()
    except Exception:
        return ""

SECTOR_KEYWORDS = {
    "AI / 大模型": ["AI", "人工智能", "大模型", "深度学习", "机器学习", "NLP", "ChatGPT"],
    "机器人 / 具身智能": ["机器人", "具身智能", "人形", "机械臂", "自动化"],
    "半导体 / 芯片": ["半导体", "芯片", "算力", "GPU", "集成电路", "光刻"],
    "硬科技 / 新材料": ["硬科技", "新材料", "钙钛矿", "纳米", "碳纤维"],
    "新能源 / 储能": ["新能源", "储能", "光伏", "锂电", "电池", "氢能", "风能"],
    "生物医药 / 医疗": ["生物医药", "医疗", "药", "基因", "蛋白", "细胞", "临床"],
    "量子计算": ["量子", "超导", "量子比特"],
    "低空经济 / 航天": ["低空", "航天", "卫星", "无人机", "飞行"],
    "消费 / 新零售": ["消费", "零售", "电商", "品牌", "连锁", "食品"],
    "企业服务 / SaaS": ["企业服务", "SaaS", "软件", "云", "数字化"],
}

def extract_sectors(items: list[IntelItem]) -> list[tuple[str, int]]:
    """Return top sectors by mention count."""
    combined = " ".join(f"{i.title} {i.summary}" for i in items)
    scores: list[tuple[str, int]] = []
    for sector, keywords in SECTOR_KEYWORDS.items():
        count = sum(1 for kw in keywords if kw.lower() in combined.lower())
        if count > 0:
            scores.append((sector, count))
    scores.sort(key=lambda x: -x[1])
    return scores[:5]

def extract_disclosed_amounts(items: list[IntelItem]) -> str:
    """Extract disclosed amounts from summaries, return approximate total."""
    amounts: list[float] = []
    for item in items:
        text = f"{item.title} {item.summary}"
        for m in re.finditer(r"(\d+\.?\d*)\s*(亿|万|亿美元|万美元|亿元|万元)", text):
            val = float(m.group(1))
            unit = m.group(2)
            if "亿" in unit:
                val *= 1e8
            elif "万" in unit:
                val *= 1e4
            if "美元" in unit:
                val *= 7
            amounts.append(val)
    if not amounts:
        return "未提取到明确金额"
    total = sum(amounts)
    if total >= 1e8:
        return f"约 {total/1e8:.0f} 亿元"
    elif total >= 1e4:
        return f"约 {total/1e4:.0f} 万元"
    return f"约 {total:.0f} 元"

def extract_spotlight(items: list[IntelItem], n: int = 3) -> list[IntelItem]:
    """Return top-N spotlight items: P0 + high credibility + recent."""
    candidates = [i for i in items if i.priority == "P0" and i.credibility in ("高", "中高")]
    if not candidates:
        candidates = [i for i in items if i.priority == "P0"]
    candidates.sort(key=lambda i: date_sort_value(i.published), reverse=True)
    return candidates[:n]

def top_companies(items: list[IntelItem], n: int = 5) -> list[tuple[str, int]]:
    """Return top-N companies by item count."""
    counts: dict[str, int] = {}
    for item in items:
        if item.company:
            counts[item.company] = counts.get(item.company, 0) + 1
    sorted_companies = sorted(counts.items(), key=lambda x: -x[1])
    return sorted_companies[:n]


def build_report(
    items: list[IntelItem], failures: list[str],
    config: dict[str, Any], tz: ZoneInfo, recipients: list[str],
) -> dict[str, Any]:
    now = dt.datetime.now(tz)
    today_cn = now.strftime("%Y年%m月%d日")
    lookback = start_lookback_days(config)
    start_date = now.date() - dt.timedelta(days=lookback)
    date_range = f"{start_date.isoformat()} 至 {now.date().isoformat()}"
    subject = f"中国TOP100私募股权GP动态周报（{today_cn}）"
    # Count unique companies WITHOUT the AI_SEARCH_COMPANY_LIMIT env var
    target_aliases = company_aliases(config)
    target_count = len(target_aliases)
    grouped: dict[str, list[IntelItem]] = {}
    for item in sort_items(items):
        grouped.setdefault(report_group(item), []).append(item)
    channel_counts: dict[str, int] = {}
    for item in items:
        channel_counts[item.channel] = channel_counts.get(item.channel, 0) + 1

    # ── Build HTML body ────────────────────────────────────────────────
    html_parts: list[str] = []
    text_parts: list[str] = []

    def esc(s: str) -> str:
        return html.escape(s, quote=False)

    def esc_url_attr(s: str) -> str:
        """Escape a URL for href attribute — only escape ", preserve & for email compatibility."""
        return s.replace('"', '&quot;')

    def fmt_published(pub: str) -> str:
        try:
            d = dt.date.fromisoformat(pub)
            return d.strftime("%m/%d")
        except Exception:
            return pub

    def cred_badge(level: str) -> str:
        colors = {"高": "#1a7f37", "中高": "#9a6700", "中": "#656d76"}
        c = colors.get(level, "#656d76")
        return f'<span style="display:inline-block;padding:1px 8px;border-radius:10px;font-size:11px;color:#fff;background:{c};">{esc(level)}</span>'

    # Generate trend analysis
    trend_text = generate_trend_analysis(items, failures)

    # One-line conclusion
    p0_items = [i for i in items if i.priority == "P0"]
    p1_items = [i for i in items if i.priority == "P1"]
    fund_items = [i for i in items if i.dimension == "基金募集动态"]
    invest_items = [i for i in items if i.dimension == "投资组合与交易动态"]
    conclusion_parts = []
    if fund_items:
        conclusion_parts.append(f"基金募集动态 {len(fund_items)} 条")
    if invest_items:
        conclusion_parts.append(f"投资交易 {len(invest_items)} 条")
    if p1_items:
        conclusion_parts.append(f"运营/生态情报 {len(p1_items)} 条")
    conclusion = "、".join(conclusion_parts) if conclusion_parts else "本周未采集到高可信度动态"

    intro = (
        f"检索 {date_range} 一级市场私募基金管理人动态，"
        f"按募集/投资/投后/组织/品牌/战略/合规 7 维度归类。"
    )
    stats = (
        f"覆盖 {target_count} 家管理人，收录 {len(items)} 条有效情报。"
    )

    # ── Build KPI dashboard ────────────────────────────────────────────
    p0_count = len([i for i in items if i.priority == "P0"])
    p1_count = len([i for i in items if i.priority == "P1"])
    p2_count = len([i for i in items if i.priority == "P2"])
    fund_count = len([i for i in items if i.dimension == "基金募集动态"])
    invest_count = len([i for i in items if i.dimension == "投资组合与交易动态"])
    postmgmt_count = len([i for i in items if i.dimension == "已投项目投后管理"])
    people_count = len([i for i in items if i.dimension == "组织与团队建设"])
    brand_count = len([i for i in items if i.dimension == "品牌与行业影响力"])
    strategy_count = len([i for i in items if i.dimension == "战略动向与合作关系"])
    compliance_count = len([i for i in items if i.dimension == "合规与监管动态"])
    active_companies = {i.company for i in items if i.company}
    sectors = extract_sectors(items)
    amount_str = extract_disclosed_amounts(items)
    top5 = top_companies(items, n=5)

    # Sectors string
    sectors_str = " / ".join(f"{esc(s)}({c})" for s, c in sectors) if sectors else "暂无明确赛道信号"

    # Top companies string
    top_co_str = "、".join(f"{esc(n)}({c}条)" for n, c in top5) if top5 else "暂无"

    # Conclusion text — prefer AI trend analysis, fall back to simple conclusion
    analysis_text = trend_text if trend_text else conclusion

    # ── HTML header ────────────────────────────────────────────────────
    html_parts.append("""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#F5F5F2;font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue','PingFang SC','Microsoft YaHei',Arial,sans-serif;color:#222;-webkit-text-size-adjust:100%%;">
<table role="presentation" width="100%%" cellpadding="0" cellspacing="0" style="padding:16px 0;background:#F5F5F2;"><tr><td>
<div style="width:100%%;max-width:760px;margin:0 auto;background:#fff;border:1px solid #e8e6df;border-radius:10px;overflow:hidden;">
<div style="padding:28px 24px 22px;background:#fbfaf7;border-bottom:1px solid #e8e6df;">
<div style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#888;font-weight:700;">PE/VC Weekly Intel</div>
<h1 style="font-size:25px;line-height:1.3;margin:8px 0 18px;color:#111;letter-spacing:0;">%s</h1>
<table role="presentation" cellpadding="0" cellspacing="0" width="100%%" style="margin-bottom:16px;">
<tr>
<td style="width:33%%;padding:8px 12px 8px 0;vertical-align:top;"><div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.05em;">募资动态</div><div style="font-size:22px;font-weight:700;color:#1a7f37;">%d</div><div style="font-size:11px;color:#999;">条</div></td>
<td style="width:33%%;padding:8px 12px;vertical-align:top;"><div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.05em;">投资交易</div><div style="font-size:22px;font-weight:700;color:#1a5276;">%d</div><div style="font-size:11px;color:#999;">条</div></td>
<td style="width:33%%;padding:8px 0 8px 12px;vertical-align:top;"><div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.05em;">披露规模估算</div><div style="font-size:16px;font-weight:700;color:#9a6700;">%s</div></td>
</tr>
<tr>
<td style="padding:8px 12px 8px 0;vertical-align:top;"><div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.05em;">人事变动</div><div style="font-size:22px;font-weight:700;color:#656d76;">%d</div><div style="font-size:11px;color:#999;">条</div></td>
<td style="padding:8px 12px;vertical-align:top;"><div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.05em;">投后/合规</div><div style="font-size:22px;font-weight:700;color:#656d76;">%d</div><div style="font-size:11px;color:#999;">条</div></td>
<td style="padding:8px 0 8px 12px;vertical-align:top;"><div style="font-size:11px;color:#888;text-transform:uppercase;letter-spacing:.05em;">活跃机构数</div><div style="font-size:22px;font-weight:700;color:#111;">%d</div><div style="font-size:11px;color:#999;">家</div></td>
</tr>
</table>
<div style="font-size:13px;color:#444;line-height:1.8;margin-bottom:4px;"><strong>活跃赛道：</strong>%s</div>
<div style="font-size:13px;color:#444;line-height:1.8;margin-bottom:4px;"><strong>最活跃机构：</strong>%s</div>
<div style="font-size:13px;color:#444;line-height:1.8;margin-bottom:14px;"><strong>P0 %d 条 / P1 %d 条 / P2 %d 条</strong> · 覆盖 %d 家管理人</div>
""" % (esc(subject), fund_count, invest_count, esc(amount_str),
       people_count, postmgmt_count + compliance_count, len(active_companies),
       esc(sectors_str), esc(top_co_str),
       p0_count, p1_count, p2_count, target_count))

    # Trend analysis / conclusion
    if analysis_text:
        html_parts.append('<div style="padding:12px 16px;background:#f0ede4;border-radius:6px;font-size:13px;color:#444;line-height:1.8;">'
                          '<strong style="color:#111;">📝 趋势分析与结论</strong><br>%s'
                          '</div>' % esc(analysis_text))

    html_parts.append('</div>')

    text_parts.extend([subject, "=" * 60, "",
                       f"募资 {fund_count}条 | 投资 {invest_count}条 | 人事 {people_count}条 | 投后/合规 {postmgmt_count+compliance_count}条",
                       f"披露规模: {amount_str} | 活跃赛道: {sectors_str}",
                       f"活跃机构: {top_co_str} | P0:{p0_count} P1:{p1_count} P2:{p2_count}",
                       ""])

    # ── All items as a flat list, grouped by company ─────────────────
    if items:
        html_parts.append('<div style="padding:24px;">')
        # Build company map from all items (flat, no tier grouping)
        company_map: dict[str, list[IntelItem]] = {}
        for it in items:
            company_map.setdefault(it.company, []).append(it)
        for company_name in sorted(company_map.keys(), key=lambda x: (0 if x == "德同资本" else 1, x)):
            company_items = company_map[company_name]
            html_parts.append(
                '<div style="margin:18px 0 6px;font-size:15px;font-weight:700;color:#1a5276;">'
                '%s <span style="font-size:12px;color:#888;font-weight:400;">(%d 条)</span></div>'
                % (esc(company_name), len(company_items)))
            text_parts.append(f"  【{company_name}】{len(company_items)} 条")
            for item in company_items:
                url_href = esc_url_attr(item.url)
                url_display = esc(item.url)
                title = esc(item.title)
                source = esc(item.source or "")
                summary = bold_company(esc(item.summary or ""), esc(item.company))
                pub_str = fmt_published(item.published)
                prio = item.priority
                dim = esc(item.dimension)
                label = esc(item.matrix_label)
                cred = item.credibility
                verif = esc(item.verification_notes)

                star = "⭐⭐⭐" if prio == "P0" else ("⭐⭐" if prio == "P1" else "⭐")
                badge = cred_badge(cred)

                verif_html = ""
                if verif:
                    verif_html = f'<div style="margin-top:6px;font-size:12px;color:#b35900;">⚠ 待核实：{verif}</div>'

                html_parts.append(
                    '<div style="background:#fbfaf7;border:1px solid #e8e6df;border-radius:8px;padding:14px 16px;margin-bottom:10px;">'
                    '<div style="font-size:13px;color:#888;margin-bottom:4px;">%s · %s %s</div>'
                    '<div style="font-size:15px;font-weight:700;margin-bottom:4px;">'
                    '<a href="%s" target="_blank" rel="noopener noreferrer" style="color:#1a5276;text-decoration:none;">%s</a></div>'
                    '<div style="font-size:13px;color:#555;line-height:1.65;">%s</div>'
                    '<div style="margin-top:8px;display:inline-block;background:#f0ede4;border-radius:4px;padding:2px 10px;font-size:12px;color:#777;">%s %s %s</div>'
                    '%s'
                    '</div>'
                    % (pub_str, esc(source), badge, url_href, title, summary, star, dim, label, verif_html))

                text_parts.append(
                    f"     {star} {title}\n"
                    f"       {source} · {pub_str} · 可信度:{cred}\n"
                    f"       {summary}\n"
                    f"       {url_display}\n")
        html_parts.append("</div>")
    else:
        html_parts.append(
            '<div style="padding:26px 24px;font-size:15px;line-height:1.7;color:#555;">'
            '本次未采集到符合 7 维矩阵关键词和目标名单的有效动态。</div>')
        text_parts.append("本次未采集到有效动态。")

    channel_line = "、".join(
        f"{name} {count} 条" for name, count in sorted(channel_counts.items())
    ) or "暂无有效来源条目"
    # ── Stats footer ────────────────────────────────────────────────
    html_parts.append(
        '<div style="padding:20px 24px;background:#fbfaf7;font-size:12px;line-height:1.7;color:#888;">'
        '<strong style="color:#222;">数据口径</strong><br>%s %s'
        '<br><br><strong style="color:#222;">本次实际来源</strong><br>%s'
        '</div>'
        % (esc(intro), esc(stats), esc(channel_line)))
    text_parts.append("")
    text_parts.append(f"数据口径：{intro} {stats}")
    text_parts.append(f"实际来源：{channel_line}")
    if failures:
        text_parts.append(f"采集备注：{len(failures)} 条")
        for f in failures[:8]:
            text_parts.append(f"  - {f}")

    # ── Close HTML
    html_parts.append("</div></td></tr></table></body></html>")

    return {
        "subject": subject,
        "recipients": recipients,
        "html_body": "\n".join(html_parts),
        "plain_text": "\n".join(text_parts),
        "summary": f"覆盖 {target_count} 家管理人，本次去重后收录 {len(items)} 条有效情报 ({date_range})。",
        "channel_counts": channel_counts,
        "items": [asdict(item) for item in items],
        "failures": failures,
        "generated_at": now.isoformat(),
    }


# ── Email sending ─────────────────────────────────────────────────────

def send_email(report: dict[str, Any], attach_html: bool = False) -> None:
    required = ["SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"缺少 SMTP 配置：{', '.join(missing)}")
    message = email.message.EmailMessage()
    message["Subject"] = report["subject"]
    message["From"] = os.environ["SMTP_FROM"]
    message["To"] = ", ".join(report["recipients"])
    if attach_html:
        channel_counts = report.get("channel_counts") or {}
        channel_line = "、".join(f"{name} {count} 条" for name, count in sorted(channel_counts.items()))
        summary_text = "\n".join([
            report.get("subject", REPORT_NAME),
            report.get("summary", ""),
            f"实际来源：{channel_line or '暂无有效来源条目'}",
            f"采集备注：{len(report.get('failures', []))} 条",
            "", "完整 HTML 报告见附件。",
        ])
        message.set_content(summary_text, subtype="plain", charset="utf-8")
        message.add_attachment(
            report["html_body"], subtype="html",
            filename="pe_vc_weekly_report.html",
        )
    else:
        message.set_content(report["plain_text"], subtype="plain", charset="utf-8")
        message.add_alternative(report["html_body"], subtype="html", charset="utf-8")
    host = os.environ["SMTP_HOST"]
    port = int(os.environ["SMTP_PORT"])
    context = ssl.create_default_context()
    errors: list[str] = []
    for attempt in range(1, 4):
        try:
            if port in {465, 994}:
                with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as smtp:
                    smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
                    smtp.send_message(message)
            else:
                with smtplib.SMTP(host, port, timeout=60) as smtp:
                    smtp.ehlo()
                    smtp.starttls(context=context)
                    smtp.ehlo()
                    smtp.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
                    smtp.send_message(message)
            return
        except Exception as exc:
            errors.append(f"attempt {attempt}: {exc}")
            if attempt < 3:
                time.sleep(8)
    raise RuntimeError("SMTP 发送失败；" + " | ".join(errors))


def send_report_with_fallback(report: dict[str, Any]) -> dict[str, Any]:
    mode = os.environ.get("EMAIL_DELIVERY_MODE", "auto").strip().lower()
    if mode == "attachment":
        send_email(report, attach_html=True)
        report["delivery_mode"] = "html_attachment"
        return report
    if mode == "inline":
        send_email(report, attach_html=False)
        report["delivery_mode"] = "inline_html"
        return report
    try:
        send_email(report, attach_html=False)
        report["delivery_mode"] = "inline_html"
    except RuntimeError as inline_error:
        report["inline_send_error"] = str(inline_error)
        send_email(report, attach_html=True)
        report["delivery_mode"] = "html_attachment"
    return report


def send_item_batches(
    items: list[IntelItem], failures: list[str],
    config: dict[str, Any], tz: ZoneInfo, recipients: list[str],
) -> list[dict[str, Any]]:
    if os.environ.get("EMAIL_SPLIT_MODE", "0") != "1":
        report = build_report(items, failures, config, tz, recipients)
        return [send_report_with_fallback(report)]
    chunk_size = int(os.environ.get("EMAIL_CHUNK_SIZE", "25"))
    if chunk_size <= 0 or len(items) <= chunk_size:
        report = build_report(items, failures, config, tz, recipients)
        return [send_report_with_fallback(report)]
    sent_reports: list[dict[str, Any]] = []
    total_parts = (len(items) + chunk_size - 1) // chunk_size
    for index in range(total_parts):
        chunk = items[index * chunk_size:(index + 1) * chunk_size]
        report = build_report(chunk, failures if index == total_parts - 1 else [], config, tz, recipients)
        report["subject"] = f"{report['subject']} 第 {index + 1}/{total_parts} 部分"
        sent_reports.append(send_report_with_fallback(report))
        time.sleep(3)
    return sent_reports


def write_report_files(report: dict[str, Any], allow_empty: bool = False) -> None:
    if not report.get("items") and OUT_JSON.exists() and not allow_empty:
        raise RuntimeError("本轮未采集到有效条目，已阻止覆盖上一份报告；如需允许空报告，设置 ALLOW_EMPTY_REPORT=1")
    if OUT_JSON.exists():
        shutil.copy2(OUT_JSON, OUT_JSON_BAK)
    if OUT_HTML.exists():
        shutil.copy2(OUT_HTML, OUT_HTML_BAK)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_HTML.write_text(report["html_body"], encoding="utf-8")


def load_existing_items(report: dict[str, Any]) -> list[IntelItem]:
    items: list[IntelItem] = []
    for row in report.get("items", []):
        if not isinstance(row, dict):
            continue
        try:
            items.append(IntelItem(
                title=clean_text(str(row.get("title", ""))),
                url=clean_text(str(row.get("url", ""))),
                source=clean_text(str(row.get("source", ""))),
                published=clean_text(str(row.get("published", ""))),
                summary=clean_text(str(row.get("summary", ""))),
                company=clean_text(str(row.get("company", ""))),
                company_group=clean_text(str(row.get("company_group", ""))),
                priority=normalize_priority(str(row.get("priority", ""))) or "P2",
                dimension=clean_text(str(row.get("dimension", ""))) or "品牌与行业影响力",
                matrix_label=clean_text(str(row.get("matrix_label", ""))) or "品牌声量与常态化市场监测",
                channel=clean_text(str(row.get("channel", ""))) or "未知来源",
                credibility=clean_text(str(row.get("credibility", ""))) or "中",
                verification_notes=clean_text(str(row.get("verification_notes", ""))),
            ))
        except Exception:
            continue
    return items


# ── Main ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--send", action="store_true", help="send generated report")
    parser.add_argument("--send-existing", action="store_true", help="rebuild and send the latest generated report without collecting again")
    parser.add_argument("--dry-run", action="store_true", help="generate without sending")
    parser.add_argument("--recipients", default=",".join(DEFAULT_RECIPIENTS), help="comma-separated recipient emails")
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--max-items", type=int, default=int(os.environ.get("MAX_ITEMS", "0")), help="0 means no truncation")
    args = parser.parse_args()

    load_dotenv(ENV_PATH)
    config = load_config()
    tz = ZoneInfo(config.get("timezone", "Asia/Shanghai"))
    recipients = [value.strip() for value in args.recipients.split(",") if value.strip()]
    if not recipients:
        env_recipients = os.environ.get("RECIPIENTS", "")
        recipients = [value.strip() for value in env_recipients.split(",") if value.strip()]
    if args.send_existing:
        if not OUT_JSON.exists():
            raise RuntimeError(f"找不到已生成的报告：{OUT_JSON}")
        existing = json.loads(OUT_JSON.read_text(encoding="utf-8"))
        items = load_existing_items(existing)
        report = build_report(items, existing.get("failures", []), config, tz, recipients)
        write_report_files(report, allow_empty=True)
        sent_reports = send_item_batches(items, existing.get("failures", []), config, tz, recipients)
        print(json.dumps({
            "ok": True, "sent": True, "used_existing_report": True,
            "items": len(items), "emails": len(sent_reports),
            "json": str(OUT_JSON), "html": str(OUT_HTML),
        }, ensure_ascii=False, indent=2))
        return 0

    # Override lookback days if SEARCH_DAYS env var is set
    lookback = int(os.environ.get("SEARCH_DAYS", str(args.lookback_days)))
    today = dt.datetime.now(tz).date()
    start = today - dt.timedelta(days=lookback)

    # Phase 1: RSS sources
    rss_items, rss_failures = fetch_rss(config, start, tz)
    # Phase 2: Targeted company search via RSSHub
    targeted_items, targeted_failures = fetch_company_search(config, start, tz)
    # Phase 3: AI Search batch intel (DeepSeek official)
    ai_search_items, ai_search_failures = fetch_ai_search_batch_intel(config, start, today, tz)
    # Phase 4: AMAC filing (fallback to AI Search)
    amac_items, amac_failures = fetch_amac_filing(config, start, tz)

    items = sort_items(dedupe(rss_items + targeted_items + ai_search_items + amac_items))
    failures = rss_failures + targeted_failures + ai_search_failures + amac_failures

    # Fill credibility & verification notes
    for item in items:
        item.credibility = infer_credibility(item)
        item.verification_notes = infer_verification(item)

    # Validate
    aliases = company_aliases(config)
    validated: list[IntelItem] = []
    validate_log: list[str] = []
    for item in items:
        keep, reason = validate_item(item, aliases)
        if keep:
            validated.append(item)
        else:
            validate_log.append(f"丢弃 [{item.company}] {item.title[:60]} — {reason}")
    if validate_log:
        failures.append(f"validate_item 过滤 {len(validate_log)} 条：" + "; ".join(validate_log[:15]))
    items = validated

    # Validate URLs are reachable
    url_invalid: list[str] = []
    url_validated: list[IntelItem] = []
    for item in items:
        if validate_url(item.url):
            url_validated.append(item)
        else:
            url_invalid.append(f"URL不可访问 [{item.company}] {item.title[:50]} — {item.url}")
    if url_invalid:
        failures.append(f"URL验证过滤 {len(url_invalid)} 条：" + "; ".join(url_invalid[:10]))
    items = url_validated

    # Enrich announcement items with full content
    items = enrich_announcement_items(items)

    if args.max_items > 0:
        items = items[: args.max_items]
    report = build_report(items, failures, config, tz, recipients)

    write_report_files(report, allow_empty=os.environ.get("ALLOW_EMPTY_REPORT") == "1")

    if args.send:
        sent_reports = send_item_batches(items, failures, config, tz, recipients)
        report["sent_emails"] = len(sent_reports)
        write_report_files(report, allow_empty=True)

    print(json.dumps({
        "ok": True,
        "sent": bool(args.send),
        "items": len(items),
        "failures": failures[:20],
        "json": str(OUT_JSON),
        "html": str(OUT_HTML),
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())