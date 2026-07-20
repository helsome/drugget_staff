from price_specialist.enums import CandidateType
from price_specialist.schemas import SearchHit
from price_specialist.search import SearchClassifier, canonical_url, deduplicate_hits, weekly_search_cohort
from price_specialist.smoke_plan import normalize_shop_name


def test_shop_cleanup_url_canonicalization_and_deduplication() -> None:
    assert normalize_shop_name("38人种草好评99%包邮阿里健康大药房") == "阿里健康大药房"
    assert canonical_url("https://detail.tmall.com/item.htm?id=648878452873&skuId=1") == "https://item.taobao.com/item.htm?id=648878452873"
    hits = [
        SearchHit(platform="taobao", query="托妥", rank=2, title="托妥", product_id="1", url="https://item.taobao.com/item.htm?id=1"),
        SearchHit(platform="taobao", query="托妥", rank=1, title="托妥", product_id="1", url="https://item.taobao.com/item.htm?id=1&x=2"),
    ]
    deduped = deduplicate_hits(hits)
    assert len(deduped) == 1
    assert deduped[0].rank == 1


def test_search_candidate_is_not_a_formal_price() -> None:
    classifier = SearchClassifier(
        fixed_product_ids=set(),
        fixed_urls=set(),
        fixed_stores={"阿里健康大药房": "store-1"},
        known_stores={},
    )
    hit = SearchHit(
        platform="taobao",
        query="托妥 瑞舒伐他汀钙片",
        title="新托妥 10mg*28片/盒 瑞舒伐他汀钙片",
        product_id="1059532384066",
        url="https://item.taobao.com/item.htm?id=1059532384066",
        shop_name="阿里健康大药房",
        list_price_raw="¥13.8",
    )
    candidate = classifier.classify(hit, target_brand="托妥", target_spec="10mg*28片")
    assert candidate.candidate_type == CandidateType.NEW_LINK_SAME_STORE
    assert candidate.is_formal_price is False


def test_weekly_cohort_keeps_high_risk_and_alternates_remaining() -> None:
    brands = ["a", "b", "c", "d", "e"]
    first = weekly_search_cohort(brands, week_number=1, high_risk={"a"})
    second = weekly_search_cohort(brands, week_number=2, high_risk={"a"})
    assert "a" in first and "a" in second
    assert set(first + second) == set(brands)

