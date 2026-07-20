from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


DRUG_MAP: dict[str, str] = {
    "马来酸阿伐曲泊帕片": "晴安欣",
    "甲磺酸仑伐替尼胶囊": "泽万欣",
    "来特莫韦片": "晴普宁",
    "罗沙司他胶囊": "希诺彤",
    "氯苯唑酸葡胺软胶囊": "翊安",
    "培唑帕尼片": "赛维可",
    "瑞戈非尼片": "晴万瑞",
    "磷酸特地唑胺片": "抗立平",
    "阿瑞匹坦胶囊": "安多林",
    "托伐普坦片": "欣速安",
    "芦比前列酮软胶囊": "畅凡",
    "西格列汀二甲双胍缓释片": "品多",
    "马昔腾坦片": "晴乐安",
    "枸橼酸托法替布片": "唯捷",
    "米拉贝隆缓释片": "晴诺舒",
    "碳酸镧咀嚼片": "迈诺恩",
    "替格瑞洛片": "倍利舒",
    "厄贝沙坦氢氯噻嗪片": "依伦平",
    "瑞舒伐他汀钙片": "托妥",
    "地奥司明片": "葛泰",
    "硫酸氢氯吡格雷片": "优立维",
    "奥美沙坦酯氨氯地平片": "天舒平",
    "氨氯地平阿托伐他汀钙片": "天依宁",
    "奥美沙坦酯片": "希佳",
    "恩替卡韦胶囊": "甘泽",
    "泽桂癃爽胶囊": "泽桂癃爽",
    "利伐沙班片": "晴瑞欣",
    "磷酸西格列汀片": "品定",
    "归柏化瘀胶囊": "晴必舒",
    "胆舒软胶囊": "舒贝尼",
}
BRAND_TO_GENERIC = {brand: generic for generic, brand in DRUG_MAP.items()}
BRAND_ALIASES = {"新托妥": "托妥"}
GENERIC_VARIANTS = {
    "厄贝沙坦氢氯噻唛片": "依伦平",
    "托妥瑞舒伐他汀钙片": "托妥",
    "芦比前列酮胶囊": "畅凡",
}
SPEC_NORMALIZE = {
    "欣速安7": "15mg*7片",
    "畅凡10": "24μg*10粒",
    "畅凡16": "24μg*16粒",
    "畅凡28": "24μg*28粒",
    "品多16": "0.1g:1g*16片",
    "依伦平14": "0.15g:12.5mg*14片",
    "依伦平28": "0.15g:12.5mg*28片",
    "葛泰20": "0.45g*20片",
    "葛泰24": "0.45g*24片",
    "希佳7": "20mg*7片",
    "希佳14": "20mg*14片",
    "天舒平7": "20mg:5mg*7片",
    "天舒平14": "20mg:5mg*14片",
    "天依宁7": "10mg*7片",
    "天依宁14": "5mg:10mg*14片",
    "优立维36": "75mg*36片",
    "优立维48": "75mg*48片",
    "甘泽12": "0.5mg*12粒",
    "甘泽24": "0.5mg*24粒",
    "甘泽48": "0.5mg*48粒",
    "晴瑞欣7": "10mg*7片",
    "晴诺舒20": "50mg*20片",
    "新托妥14": "10mg*14片",
    "新托妥28": "10mg*28片",
}


def normalize_brand(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return BRAND_ALIASES.get(text, text) or None


def normalize_spec(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip().replace("：", ":").replace("×", "*")
    return SPEC_NORMALIZE.get(text, text) or None


def find_brand(*values: object) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value)
        for brand in BRAND_TO_GENERIC:
            if brand in text:
                return brand
        for alias, brand in BRAND_ALIASES.items():
            if alias in text:
                return brand
        for variant, brand in GENERIC_VARIANTS.items():
            if variant in text:
                return brand
        for generic, brand in DRUG_MAP.items():
            if generic in text:
                return brand
    return None


def find_target_brand(*values: object) -> str | None:
    """Match an official branded product without assigning a competitor by generic name."""
    for value in values:
        if value is None:
            continue
        text = str(value)
        for brand in BRAND_TO_GENERIC:
            if brand in text:
                return brand
        for alias, brand in BRAND_ALIASES.items():
            if alias in text:
                return brand
        for variant, brand in GENERIC_VARIANTS.items():
            if variant in text:
                return brand
    return None


def parse_package_units(spec: object) -> tuple[Decimal | None, str | None]:
    """Parse explicit package components without converting measurement units.

    `80mg*2粒+125mg*1粒` returns 3粒. Mixed minimum units are rejected.
    """
    if not spec:
        return None, None
    components = re.findall(r"(?:\d+(?:\.\d+)?\s*(?:mg|g|μg|ug|ml)?\s*[*xX])\s*(\d+)\s*(片|粒|袋|支|丸|胶囊)", str(spec))
    if not components:
        components = re.findall(r"(?:^|[^\d])(\d+)\s*(片|粒|袋|支|丸|胶囊)", str(spec))
    if not components:
        return None, None
    normalized = [(Decimal(count), "粒" if unit == "胶囊" else unit) for count, unit in components]
    units = {unit for _, unit in normalized}
    if len(units) != 1:
        return None, None
    return sum((count for count, _ in normalized), Decimal("0")), normalized[0][1]


def infer_min_unit(generic_name: str) -> str:
    return "粒" if "胶囊" in generic_name else "片"


@dataclass(frozen=True)
class ControlPriceEntry:
    brand: str
    generic_name: str
    spec_key: str | None
    price: Decimal
    min_unit: str
    source_line: str


def parse_control_prices(path: Path) -> list[ControlPriceEntry]:
    entries: list[ControlPriceEntry] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines()[3:]:
        line = raw_line.strip()
        if not line or line in {"甲类", "乙类", "丙类"}:
            continue
        line = re.sub(r"^(甲类|乙类|丙类)", "", line)
        brand = next((name for name in BRAND_TO_GENERIC if name in line), None)
        if not brand:
            continue
        generic = BRAND_TO_GENERIC[brand]
        brand_pos = line.index(brand)
        price_match = re.search(r"(\d+(?:\.\d+)?)", line[brand_pos + len(brand) :])
        if not price_match:
            continue
        between = line[line.index(generic) + len(generic) : brand_pos].strip()
        entries.append(
            ControlPriceEntry(
                brand=brand,
                generic_name=generic,
                spec_key=between or None,
                price=Decimal(price_match.group(1)),
                min_unit=infer_min_unit(generic),
                source_line=line,
            )
        )
    return entries
