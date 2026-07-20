from __future__ import annotations

import re
from collections.abc import Iterable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from .catalog import BRAND_TO_GENERIC, find_brand, normalize_spec
from .enums import CandidateType
from .schemas import ClassifiedCandidate, SearchHit
from .smoke_plan import normalize_shop_name


def canonical_url(url: str | None) -> str | None:
    if not url or not str(url).startswith(("http://", "https://")):
        return None
    parsed = urlparse(str(url))
    query = parse_qs(parsed.query)
    if "jd.com" in parsed.netloc:
        match = re.search(r"/(\d+)\.html", parsed.path)
        return f"https://item.jd.com/{match.group(1)}.html" if match else None
    if "ysbang.cn" in parsed.netloc:
        # 药师帮 SPA uses `#/druginfo?...`; URL parameters live in the
        # fragment rather than parsed.query.
        if not query and "?" in parsed.fragment:
            query = parse_qs(parsed.fragment.split("?", 1)[1])
        wholesale_id = query.get("wholesaleId", [None])[0] or query.get("wholesale_id", [None])[0]
        provider_id = query.get("providerId", [None])[0] or query.get("provider_id", [None])[0]
        if wholesale_id and provider_id:
            return f"https://dian.ysbang.cn/#/druginfo?{urlencode({'wholesaleId': wholesale_id, 'providerId': provider_id})}"
        return None
    item_id = query.get("id", [None])[0]
    if item_id:
        return urlunparse(("https", "item.taobao.com", "/item.htm", "", urlencode({"id": item_id}), ""))
    return None


def deduplicate_hits(hits: Iterable[SearchHit]) -> list[SearchHit]:
    """Deduplicate within one run without turning list prices into observations."""
    selected: dict[tuple[str, str], SearchHit] = {}
    for hit in hits:
        url = canonical_url(hit.url)
        identity = hit.product_id or url
        if not identity:
            identity = f"invalid:{hit.query}:{hit.rank}:{hit.title}"
        key = (hit.platform, identity)
        previous = selected.get(key)
        if previous is None or (hit.rank or 10**9) < (previous.rank or 10**9):
            selected[key] = hit.model_copy(update={"url": url or hit.url})
    return list(selected.values())


def weekly_search_cohort(brands: list[str], *, week_number: int, high_risk: set[str]) -> list[str]:
    """Run high-risk drugs weekly and alternate the remaining drugs over two weeks."""
    high = [brand for brand in brands if brand in high_risk]
    normal = [brand for brand in brands if brand not in high_risk]
    parity = week_number % 2
    return high + [brand for index, brand in enumerate(normal) if index % 2 == parity]


class SearchClassifier:
    def __init__(
        self,
        *,
        fixed_product_ids: set[str],
        fixed_urls: set[str],
        fixed_stores: dict[str, str],
        known_stores: dict[str, str],
    ):
        self.fixed_product_ids = fixed_product_ids
        self.fixed_urls = {value for url in fixed_urls if (value := canonical_url(url))}
        self.fixed_stores = {normalize_shop_name(key): value for key, value in fixed_stores.items()}
        self.known_stores = {normalize_shop_name(key): value for key, value in known_stores.items()}

    def classify(self, hit: SearchHit, *, target_brand: str, target_spec: str | None) -> ClassifiedCandidate:
        url = canonical_url(hit.url)
        shop = normalize_shop_name(hit.shop_name)
        matched_brand = find_brand(hit.title)
        normalized_target_spec = normalize_spec(target_spec)
        normalized_title = str(hit.title).replace("：", ":").replace("×", "*")

        if not url or not hit.product_id:
            candidate_type, reason = CandidateType.INVALID_LINK, "缺少有效商品ID或详情链接"
        elif hit.product_id in self.fixed_product_ids or url in self.fixed_urls:
            candidate_type, reason = CandidateType.KNOWN_TARGET, "与固定监控商品相同"
        elif matched_brand and matched_brand != target_brand:
            candidate_type, reason = CandidateType.NOT_MATCH, "同通用名或相似商品，但不是目标品牌"
        elif target_brand not in normalized_title and BRAND_TO_GENERIC[target_brand] not in normalized_title:
            candidate_type, reason = CandidateType.NOT_MATCH, "标题不包含目标品牌或通用名"
        elif normalized_target_spec and normalized_target_spec not in normalized_title:
            candidate_type, reason = CandidateType.POSSIBLE_MATCH, "药品匹配但规格缺失或不一致"
        elif shop in self.fixed_stores:
            candidate_type, reason = CandidateType.NEW_LINK_SAME_STORE, "固定责任店的新链接"
        elif shop in self.known_stores:
            candidate_type, reason = CandidateType.KNOWN_NON_FIXED_STORE, "责任档案已有但未纳入固定线"
        else:
            candidate_type, reason = CandidateType.NEW_STORE, "责任档案未匹配的新店"

        payload = hit.model_dump()
        payload.update(
            {
                "url": url or hit.url,
                "shop_name": shop or hit.shop_name,
                "candidate_type": candidate_type,
                "target_brand": target_brand,
                "target_spec": normalized_target_spec,
                "matched_brand": matched_brand,
                "matched_store_id": self.fixed_stores.get(shop) or self.known_stores.get(shop),
                "reason": reason,
            }
        )
        return ClassifiedCandidate(**payload)
