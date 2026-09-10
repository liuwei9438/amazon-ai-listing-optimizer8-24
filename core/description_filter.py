"""详情描述本地过滤（总后台「详情描述参与AI优化」= 关时使用）。

目的：把和产品本身无关的内容整行删掉，只留产品相关文字，
完全不调用 AI、不消耗 token。结果表格里的简介列 = 过滤后的原文。

删除的内容类别（命中任意一条即整行删除）：
  1. 卖家自夸 / 公司介绍    「我们是一个优质的卖家」、专业厂家、工厂直销…
  2. 服务承诺               24小时客服、退换货、质保、联系我们…
  3. 物流配送               免运费、发货时间、shipping…
  4. 促销 / 店铺            优惠券、折扣、关注店铺、好评返现…
  5. 评价                   买家评论、5 stars…
  6. 参数噪音               ASIN / UPC / Manufacturer / BSR / 上架日期
  7. 网页杂项               链接、邮箱、社交账号、HTML 标签、See more…
  8. 乱码 / 纯符号 / 连续重复行

材质、尺寸、重量、功能、适用场景等产品本身的描述全部保留。
要补规则：在 _DROP_PATTERNS 里加一条正则即可。
"""

from __future__ import annotations

import re

# 行内 HTML：块级标签当换行处理，其余标签直接剥掉
_HTML_BLOCK_RE = re.compile(
    r"<\s*(?:br|/p|/div|/li|/tr|/td|/h[1-6])[^>]*>", re.I
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_ENTITY_MAP = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<",
    "&gt;": ">", "&quot;": '"', "&#39;": "'", "&rsquo;": "'",
}
_ENTITY_NUM_RE = re.compile(r"&#(\d+);")

# 无关内容规则（忽略大小写）。命中任意一条 → 整行删除。
_DROP_PATTERNS = [re.compile(p, re.I) for p in [
    # --- 1. 卖家自夸 / 公司介绍 ---
    r"我们是一?个?的?(优质|优秀|专业|可靠|值得信赖|实力)",
    r"(本厂|我厂|我司|本公司|我们工厂|本店)",
    r"(工厂|厂家)直销|源头(工厂|厂家)|直接从工厂",
    r"专业(生产|制造|厂家|供应商|销售|贸易)",
    r"多年(生产|制造|经营|经验)|经验丰富|诚信(经营|商家|卖家|企业)",
    r"优质(卖家|商家|供应商|厂家|服务)",
    r"专注.{0,14}(领域|行业|研发|制造|生产)",
    r"we\s+are\s+(?:a\s+|an\s+)?(?:professional|leading|reliable|trusted|top|biggest|largest|experienced)",
    r"(?:professional|leading|reliable|trusted|experienced)\s+(?:manufacturer|seller|supplier|factory|store|team|company)",
    r"our\s+(?:factory|company|workshop|team|store)",
    r"factory\s+direct|direct\s+from\s+factory",
    r"(?:founded|established)\s+in\s+\d{4}",
    # --- 2. 服务承诺 / 售后 ---
    r"客服|退换货?|包退包换|换货|质保|保修|售后|维修服务",
    r"满意保证|无理由退|放心购买|欢迎(咨询|联系|垂询)|随时联系|请联系|联系我们",
    r"customer\s+service|after[- ]?sales|warranty|return(?:s|\s+policy)?|money[- ]?back|refund",
    r"contact\s+(?:us|me|our)|get\s+in\s+touch|feel\s+free\s+to\s+(?:contact|ask|email|reach)",
    r"e-?mail\s*(?:us|to)|email\s*[:：]",
    r"(?:reply|response|respond)\s+within|\d+\s*(?:小时|hours?|days?)\s*(?:内)?(?:回复|reply|response|online)",
    r"24\s*/\s*7",
    # --- 3. 物流配送 ---
    r"运费|免邮|包邮|发货|物流|快递|仓库发货|到货时间",
    r"free\s+shipping|shipping\s+(?:cost|fee|time|policy)|fast\s+delivery",
    r"delivery\s+(?:time|in\s+\d)|shipped\s+from|dispatch|handling\s+(?:time|fee)|lead\s+time",
    # --- 4. 促销 / 店铺 ---
    r"优惠券|优惠活动|折扣|促销|秒杀|满减|满赠|店铺|关注我们|收藏(本店|店铺|我们)",
    r"好评返现|留评|五星好评|晒图返|追加好评",
    r"coupon|discount\s+code|promo(?:tion)?\s+code|follow\s+(?:our|us|store)|add\s+to\s+(?:cart|wishlist)",
    r"visit\s+our|our\s+(?:store|shop|storefront)|shop\s+now|order\s+now|buy\s+now|limited\s+time",
    # --- 5. 评价 ---
    r"买家(评|秀|反馈)|评论|好评|差评|客户评价",
    r"customer\s+reviews?|\breviews?\b|\b5[- ]?stars?\b|five\s+star|rating",
    # --- 6. 参数噪音（亚马逊页面残留）---
    r"\basin\b|\bisbn\b|\bupc\b|best\s+sellers?\s+rank|date\s+first\s+available",
    r"manufacturer|model\s+(?:no\.?|number)|part\s+number|item\s+model",
    r"商品编号|商品编码|货号[:：]|上架时间|国别关税",
    # --- 7. 网页杂项 / 联系方式 ---
    r"https?://|www\.|\.com\b|\.net\b|\.org\b",
    r"@(?:gmail|qq|163|126|outlook|hotmail|foxmail)",
    r"(?:whatsapp|wechat|facebook|instagram|twitter|tiktok|youtube)",
    r"微信|QQ号|联系 电话|联系电话|电话[:：]\s*\+?\d|手機|手机号",
    r"(?:tel|phone|mobile|fax)\s*[:：]",
    r"see\s+more|learn\s+more|click\s+here|^ai\s+overview|^overview[:：]?\s*$",
    r"下单|购买前(请|联系)|如有疑问",
]]

# 纯符号 / 乱码行
_SYMBOLIC_LINE_RE = re.compile(r"^[\W_\d\s]+$")
# “数字/UPC 式”长编号行（如 B08XYZ1234、100023456789012）
_CODE_LINE_RE = re.compile(r"^[A-Z0-9\-]{10,}$", re.I)

DEFAULT_LIMIT = 2000


def _strip_html(text: str) -> str:
    text = _HTML_BLOCK_RE.sub("\n", text)
    text = _HTML_TAG_RE.sub(" ", text)
    for entity, char in _ENTITY_MAP.items():
        text = text.replace(entity, char)
    return _ENTITY_NUM_RE.sub(lambda m: chr(int(m.group(1))), text)


def _is_noise(line: str) -> bool:
    stripped = line.strip()
    if len(stripped) <= 1:
        return True
    if _SYMBOLIC_LINE_RE.match(stripped):
        return True
    if _CODE_LINE_RE.match(stripped):
        return True
    return any(p.search(stripped) for p in _DROP_PATTERNS)


def clean_description(text, limit: int = DEFAULT_LIMIT) -> str:
    """清洗详情描述：删掉与产品无关的行，只留产品相关内容。

    text:  原始简介/详情描述（可含 HTML 标签和换行）
    limit: 清洗后最多保留的字符数（默认 2000，按整行截断）
    返回:  过滤后的纯文本；没有任何产品相关内容时返回空字符串
    """
    if not text:
        return ""
    if not isinstance(text, str):
        text = str(text)

    lines = _strip_html(text).splitlines()

    kept: list[str] = []
    seen: set[str] = set()
    used = 0
    for raw_line in lines:
        line = raw_line.strip()
        if _is_noise(line):
            continue
        if line in seen:  # 连续重复的模板行只留一条
            continue
        seen.add(line)
        if limit and used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1

    return "\n".join(kept).strip()
