"""
品牌词扫描公共模块。

analyze.py（离线批量分析）和 qc.py（投放前预检）都要在 OCR 文本 / whisper 转写文本里
找品牌词命中，逻辑完全一样，所以抽成这一个模块，两边 import 复用，不允许各自复制一份。
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import NamedTuple

from config import BRANDS

# 大小写不敏感；品牌词里的 "." 等符号按字面匹配（不当正则特殊字符），
# 用 re.escape 转义后拼 alternation，一次扫描命中所有品牌词。
_PATTERN = re.compile(
    "|".join(re.escape(b) for b in BRANDS),
    re.IGNORECASE,
)


class BrandHit(NamedTuple):
    brand: str
    matched_text: str


@lru_cache(maxsize=None)
def _pattern_for(brands: tuple[str, ...]) -> re.Pattern:
    return re.compile("|".join(re.escape(b) for b in brands), re.IGNORECASE)


def find_brand_hits(text: str, brands: list[str] | None = None) -> list[BrandHit]:
    """在一段文本里找出所有品牌词命中，返回 (brand, matched_text) 列表。

    brands 默认是 config.BRANDS（移动端竞品）；按产品扫描时传该产品的词表，空列表表示不扫。
    brand 是词表里的标准写法（用于分组/统计）；matched_text 是原文里实际匹配到的片段
    （大小写可能跟品牌词表不同，保留原文方便人工复核）。
    """
    if brands is not None and not brands:
        return []
    if not text:
        return []
    brands = BRANDS if brands is None else brands
    pattern = _PATTERN if brands is BRANDS else _pattern_for(tuple(brands))

    hits: list[BrandHit] = []
    lowered_brands = {b.lower(): b for b in brands}
    for m in pattern.finditer(text):
        matched = m.group(0)
        canonical = lowered_brands.get(matched.lower(), matched)
        hits.append(BrandHit(brand=canonical, matched_text=matched))
    return hits


def has_brand_hit(text: str) -> bool:
    return bool(_PATTERN.search(text or ""))
