import pytest

from price_specialist.collector import OpenCLIComputerUseCollector, detect_access_state, is_valid_detail_page, parse_detail_fields
from price_specialist.config import Settings
from price_specialist.enums import CollectionStatus, TaskType
from price_specialist.schemas import BrowserSession, CollectionTaskSpec


def test_access_state_precedence_and_detail_page_validation() -> None:
    assert detect_access_state("请完成滑块验证", None, None) == CollectionStatus.CHALLENGE_DETECTED
    assert detect_access_state("PC频控页 -京东商城", "https://pc-frequent-pro.pf.jd.com", None) == CollectionStatus.RATE_LIMITED
    assert not is_valid_detail_page(
        "jd",
        title="京东(JD.COM)-正品低价、品质保障、配送及时、轻松购物！",
        url="https://www.jd.com/?c",
        product_id="10020343903147",
    )
    assert is_valid_detail_page(
        "jd",
        title="[希佳]奥美沙坦酯片 20mg*7片 2盒装",
        url="https://item.jingdonghealth.cn/10128645470447.html",
        product_id="10128645470447",
    )


def test_parse_detail_fields_accepts_opencli_field_list() -> None:
    assert parse_detail_fields([{"field": "价格", "value": "¥10"}]) == {"价格": "¥10"}


def test_access_state_includes_login_and_rate_limit() -> None:
    assert detect_access_state(None, None, "登录失效，请重新登录") == CollectionStatus.LOGIN_REQUIRED
    assert detect_access_state(None, "https://pc-frequent-pro.pf.jd.com", None) == CollectionStatus.RATE_LIMITED


def test_taobao_shop_search_url_never_guesses_store_from_name() -> None:
    assert OpenCLIComputerUseCollector._shop_search_url("https://shop123.taobao.com/", "托妥 瑞舒伐他汀") == "https://shop123.taobao.com/search.htm?q=%E6%89%98%E5%A6%A5%20%E7%91%9E%E8%88%92%E4%BC%90%E4%BB%96%E6%B1%80"
    assert OpenCLIComputerUseCollector._shop_search_url("阿里健康大药房", "托妥") is None


@pytest.mark.asyncio
async def test_yaoshibang_missing_provider_is_manual_not_detail_attempt() -> None:
    collector = OpenCLIComputerUseCollector(Settings.from_env())
    task = CollectionTaskSpec(
        task_id="ysb-1", run_id="run-1", platform="yaoshibang", task_type=TaskType.FIXED_CORE,
        session_alias="ysb-p0", product_id="1246632606",
    )
    result = await collector.collect_fixed(task, BrowserSession(platform="yaoshibang", alias="ysb-p0"))
    assert result.collection_status == CollectionStatus.PAGE_CHANGED
    assert result.error_code == "missing_provider_id"
    assert result.evidence.raw_fields["manual_required"] is True


def test_detect_access_state_blocked_modal_returns_page_changed() -> None:
    """供应商拦截弹窗 stderr 应映射为 PAGE_CHANGED，而非 PARSE_ERROR。"""
    # detail.js 抛 CommandExecutionError 时 stderr 含 "阻断弹窗" 标记
    assert detect_access_state(
        None, None, "Error: yaoshibang detail 阻断弹窗: 请选择要下单的连锁总部"
    ) == CollectionStatus.PAGE_CHANGED
    # 直接含拦截弹窗原文也应识别
    assert detect_access_state(None, None, "采购活动ID不能为空") == CollectionStatus.PAGE_CHANGED
    assert detect_access_state("请选择要下单的连锁总部", None, None) == CollectionStatus.PAGE_CHANGED
    # 非拦截弹窗的普通错误仍返回 None（由后续 code!=0 走 PARSE_ERROR）
    assert detect_access_state(None, None, "未找到商品 wholesaleId=123") is None


class _StubRunCollector(OpenCLIComputerUseCollector):
    """OpenCLIComputerUseCollector 子类，覆写 _run 返回固定结果。

    用于在不启动子进程的情况下测试 _detail 的错误码映射。
    """

    def __init__(self, settings, *, run_result):
        super().__init__(settings)
        self._run_result = run_result

    async def _run(self, *args, timeout=180):
        return self._run_result

    async def _capture_current_tab(self, session):
        return None

    async def _ysb_page_dwell(self):
        return None


@pytest.mark.asyncio
async def test_detail_blocked_modal_returns_page_changed_not_parse_error() -> None:
    """detail.js 抛 CommandExecutionError(stderr 含"阻断弹窗") -> PAGE_CHANGED。

    验证 _detail 不会把供应商拦截误判为 PARSE_ERROR；这是 Part 2D+2E 的关键
    集成点：detect_access_state 在 code!=0 检查之前调用。
    """
    settings = Settings.from_env()
    # code=1, stdout="", stderr 含 "阻断弹窗" 标记, data=None
    collector = _StubRunCollector(
        settings,
        run_result=(1, "", "Error: yaoshibang detail 阻断弹窗: 请选择要下单的连锁总部", None),
    )
    task = CollectionTaskSpec(
        task_id="ysb-blocked", run_id="run-blocked", platform="yaoshibang",
        task_type=TaskType.INSPECT_CANDIDATE, session_alias="ysb-p0",
        product_id="1246632606", drug_name="托妥", generic_name="瑞舒伐他汀钙片",
        spec="10mg*28片", shop_name="测试店铺",
        metadata={"provider_id": "provider-1"},
    )
    result = await collector.inspect_candidate(
        task, BrowserSession(platform="yaoshibang", alias="ysb-p0")
    )
    assert result.collection_status == CollectionStatus.PAGE_CHANGED, \
        f"拦截弹窗应映射为 PAGE_CHANGED，实际 {result.collection_status}"
    assert "阻断弹窗" in (result.error_detail or "")
