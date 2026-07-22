from __future__ import annotations

import asyncio
import base64
import json
import random
import re
from urllib.parse import quote, urlparse
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any

from .config import Settings
from .enums import CalculationStatus, CollectionStatus, PriceStatus
from .errors import CollectorAccessError
from .pricing import parse_price
from .schemas import BrowserSession, CollectionResult, CollectionTaskSpec, EvidenceBundle, SearchHit
from .smoke_plan import normalize_shop_name
from .catalog import normalize_spec


CHALLENGE_MARKERS = ("验证码", "滑块", "安全验证", "京东验证", "captcha")
RATE_LIMIT_MARKERS = ("频控页", "访问过于频繁", "pc-frequent-pro", "rate limit")
LOGIN_MARKERS = ("未登录", "登录失效", "not logged", "login required")
BLOCKED_MARKERS = ("阻断弹窗", "采购活动ID不能为空", "请选择要下单的连锁总部")
HOMEPAGE_MARKERS = ("京东(jd.com)-", "轻松购物", "淘宝网 - 淘！我喜欢")


def detect_access_state(title: str | None, url: str | None, stderr: str | None) -> CollectionStatus | None:
    """Classify access failures once; callers must hand them to the incident flow.

    OpenCLI reports failures inconsistently between stderr, page title, and the
    final URL, so inspect all three.  This deliberately does *not* retry a
    login, CAPTCHA, or rate-limit failure.
    """
    text = " ".join(str(value or "").lower() for value in (title, url, stderr))
    if any(marker.lower() in text for marker in CHALLENGE_MARKERS):
        return CollectionStatus.CHALLENGE_DETECTED
    if any(marker.lower() in text for marker in RATE_LIMIT_MARKERS):
        return CollectionStatus.RATE_LIMITED
    if any(marker.lower() in text for marker in LOGIN_MARKERS):
        return CollectionStatus.LOGIN_REQUIRED
    if any(marker.lower() in text for marker in BLOCKED_MARKERS):
        return CollectionStatus.PAGE_CHANGED
    return None


def is_valid_detail_page(platform: str, *, title: str | None, url: str | None, product_id: str) -> bool:
    text = str(title or "").lower()
    if any(marker in text for marker in HOMEPAGE_MARKERS):
        return False
    final_url = str(url or "")
    if platform == "jd":
        return product_id in final_url and any(host in final_url for host in ("jd.com", "jingdonghealth.cn"))
    if platform == "taobao":
        return product_id in final_url and any(host in final_url for host in ("taobao.com", "tmall.com"))
    if platform == "yaoshibang":
        return product_id in final_url and "ysbang.cn" in final_url
    return False


def parse_detail_fields(data: Any) -> dict[str, Any]:
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        return {
            str(item["field"]): item.get("value")
            for item in data
            if isinstance(item, dict) and "field" in item
        }
    return {}


class ComputerUseCollector(ABC):
    @abstractmethod
    async def health_check(self, session: BrowserSession) -> CollectionResult: ...

    @abstractmethod
    async def collect_fixed(self, task: CollectionTaskSpec, session: BrowserSession) -> CollectionResult: ...

    @abstractmethod
    async def search(self, query: str, session: BrowserSession, *, limit: int = 20) -> list[SearchHit]: ...

    async def search_store(self, task: CollectionTaskSpec, session: BrowserSession) -> list[SearchHit]:
        """Run a store-bound search; never silently downgrade it to global search."""
        raise CollectorAccessError(
            "store search is not implemented for this collector",
            collection_status=CollectionStatus.PAGE_CHANGED.value,
            details={"manual_required": True},
        )

    def last_search_evidence(self, session: BrowserSession) -> EvidenceBundle:
        return EvidenceBundle()

    @abstractmethod
    async def inspect_candidate(self, task: CollectionTaskSpec, session: BrowserSession) -> CollectionResult: ...

    @abstractmethod
    async def resume_incident(self, incident_id: str, session: BrowserSession) -> CollectionResult: ...


class OpenCLIComputerUseCollector(ComputerUseCollector):
    """Persistent-browser Computer Use adapter backed by the installed OpenCLI.

    It never attempts CAPTCHA interaction. A challenge result is returned to the
    orchestrator, which creates a centralized human incident.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._search_evidence: dict[str, EvidenceBundle] = {}

    async def _ysb_page_dwell(self) -> None:
        """Allow the foreground Vue page to settle like a normal page review.

        This is deliberately only a short reading/SPA-render dwell. It is not a
        CAPTCHA workaround and is never invoked after an access failure.
        """
        await asyncio.sleep(random.uniform(1.5, 3.0))

    async def _run(self, *arguments: str, timeout: int = 180) -> tuple[int, str, str, Any]:
        process = await asyncio.create_subprocess_exec(
            self.settings.opencli_bin,
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return -1, "", "TIMEOUT", None
        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            data = None
        return process.returncode or 0, stdout, stderr, data

    async def _capture_current_tab(self, session: BrowserSession) -> str | None:
        # Site adapters expose their persistent tab under the platform alias.
        # Screenshotting is read-only and remains allowed after a challenge.
        code, stdout, _, data = await self._run("browser", session.alias, "screenshot", timeout=45)
        if code != 0:
            return None
        encoded = data.get("base64") if isinstance(data, dict) else stdout
        if not encoded:
            return None
        encoded = str(encoded).strip()
        if "," in encoded and encoded.startswith("data:image"):
            encoded = encoded.split(",", 1)[1]
        try:
            base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            return None
        return encoded

    async def health_check(self, session: BrowserSession) -> CollectionResult:
        code, _, stderr, data = await self._run(
            session.platform,
            "whoami",
            "-f",
            "json",
            "--site-session",
            "persistent",
            "--keep-tab",
            "true",
            timeout=60,
        )
        logged_in = isinstance(data, dict) and bool(data.get("logged_in"))
        access = detect_access_state(None, None, stderr)
        status = access or (CollectionStatus.SUCCESS if code == 0 and logged_in else CollectionStatus.LOGIN_REQUIRED)
        return CollectionResult(
            collection_status=status,
            error_code=None if status == CollectionStatus.SUCCESS else status.value,
            error_detail=None if status == CollectionStatus.SUCCESS else stderr[:1000],
            evidence=EvidenceBundle(raw_fields={"logged_in": logged_in}, collector_version="opencli-1.8.6"),
        )

    @staticmethod
    def _shop_search_url(shop_home_url: str, query: str) -> str | None:
        """Return a safe Taobao/Tmall in-shop search URL from a verified home URL."""
        parsed = urlparse(shop_home_url)
        if parsed.scheme not in {"http", "https"} or not any(
            host in parsed.netloc.lower() for host in ("taobao.com", "tmall.com")
        ):
            return None
        base = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        return f"{base}/search.htm?q={quote(query)}"

    @staticmethod
    def _json_list(*sources: Any) -> list[Any]:
        for source in sources:
            if isinstance(source, list):
                return source
            if isinstance(source, str):
                match = re.search(r"\[[\s\S]*\]", source)
                if match:
                    try:
                        value = json.loads(match.group(0))
                        if isinstance(value, list):
                            return value
                    except json.JSONDecodeError:
                        pass
        return []

    async def resolve_taobao_store_home(self, task: CollectionTaskSpec, session: BrowserSession) -> dict[str, str] | None:
        """Find a real shop URL only from an exact-name global-search hit."""
        query, shop_name = str(task.query or "").strip(), str(task.shop_name or "").strip()
        if not query or not shop_name:
            return None
        hits = await self.search(query, session)
        for hit in hits:
            if normalize_shop_name(hit.shop_name) != normalize_shop_name(shop_name) or not hit.url:
                continue
            code, _, stderr, _ = await self._run("browser", session.alias, "open", str(hit.url), "--window", "foreground", timeout=60)
            if code != 0 or detect_access_state(None, str(hit.url), stderr):
                continue
            js = "JSON.stringify(Array.from(document.querySelectorAll('a[href]')).slice(0,200).map(a=>({text:(a.innerText||a.textContent||'').trim(),href:a.href})))"
            code, stdout, stderr, data = await self._run("browser", session.alias, "eval", js, timeout=45)
            if code != 0 or detect_access_state(None, str(hit.url), stderr):
                continue
            normalized_shop = normalize_shop_name(shop_name)
            for link in self._json_list(data, stdout):
                if not isinstance(link, dict) or normalized_shop not in normalize_shop_name(link.get("text")):
                    continue
                home = str(link.get("href") or "").strip()
                parsed = urlparse(home)
                match = re.search(r"shop(\d+)\.(?:taobao|tmall)\.com", parsed.netloc.lower())
                if not match or home.rstrip("/") == "https://shop.tmall.com":
                    continue
                code, _, stderr, _ = await self._run("browser", session.alias, "open", home, "--window", "foreground", timeout=60)
                if code != 0 or detect_access_state(None, home, stderr):
                    continue
                verify_js = f"JSON.stringify({{shop_name_present:document.body.innerText.includes({json.dumps(shop_name)})}})"
                code, _, stderr, verified = await self._run("browser", session.alias, "eval", verify_js, timeout=45)
                if code == 0 and isinstance(verified, dict) and verified.get("shop_name_present"):
                    return {"shop_home_url": home, "platform_store_key": match.group(1), "source_product_id": str(hit.product_id or "")}
        return None

    async def _taobao_shop_search(self, task: CollectionTaskSpec, session: BrowserSession) -> tuple[CollectionStatus | None, dict[str, Any]]:
        """Route 1: enter a known shop home and search only that shop.

        The homepage must be supplied by the store profile/task metadata; no
        store name is turned into a guessed URL.  Browser extraction is kept in
        the collector so it uses the same persistent OpenCLI session as detail.
        """
        home = str(task.metadata.get("shop_home_url") or "").strip()
        query = str(task.query or task.metadata.get("shop_query") or task.drug_name or task.generic_name or "").strip()
        if not home or not query:
            return CollectionStatus.PAGE_CHANGED, {"route": "shop_home", "reason": "missing_shop_home_url_or_query"}
        search_url = self._shop_search_url(home, query)
        if not search_url:
            return CollectionStatus.PAGE_CHANGED, {"route": "shop_home", "reason": "invalid_shop_home_url"}
        code, _, stderr, _ = await self._run("browser", session.alias, "open", home, "--window", "foreground", timeout=60)
        access = detect_access_state(None, home, stderr)
        if access:
            return access, {"route": "shop_home", "shop_home_url": home}
        if code != 0:
            return CollectionStatus.NETWORK_ERROR, {"route": "shop_home", "shop_home_url": home, "stderr": stderr[:1000]}
        code, _, stderr, _ = await self._run("browser", session.alias, "open", search_url, "--window", "foreground", timeout=60)
        access = detect_access_state(None, search_url, stderr)
        if access:
            return access, {"route": "shop_home", "shop_home_url": home, "search_url": search_url}
        if code != 0:
            return CollectionStatus.NETWORK_ERROR, {"route": "shop_home", "shop_home_url": home, "search_url": search_url, "stderr": stderr[:1000]}
        # The browser command returns JSON encoded by different OpenCLI builds;
        # detail remains the price authority, this only verifies discovery.
        js = "JSON.stringify(Array.from(document.querySelectorAll('a[href*=' + '\"item.htm\"' + ']')).slice(0,50).map(a=>a.href))"
        code, stdout, stderr, data = await self._run("browser", session.alias, "eval", js, timeout=45)
        access = detect_access_state(None, search_url, stderr)
        if access:
            return access, {"route": "shop_home", "shop_home_url": home, "search_url": search_url}
        candidates: list[str] = []
        for source in (data, stdout, stderr):
            if isinstance(source, list):
                candidates = [str(value) for value in source]
                break
            if isinstance(source, str):
                match = re.search(r"\[[\s\S]*\]", source)
                if match:
                    try:
                        parsed = json.loads(match.group(0))
                        if isinstance(parsed, list):
                            candidates = [str(value) for value in parsed]
                            break
                    except json.JSONDecodeError:
                        pass
        product_id = str(task.product_id or "")
        if code != 0 or not any(product_id and product_id in candidate for candidate in candidates):
            return CollectionStatus.PAGE_CHANGED, {
                "route": "shop_home", "shop_home_url": home, "search_url": search_url,
                "candidate_count": len(candidates),
            }
        return None, {"route": "shop_home", "shop_home_url": home, "search_url": search_url, "candidate_count": len(candidates)}

    async def _yaoshibang_shop_profile(self, task: CollectionTaskSpec, session: BrowserSession) -> tuple[CollectionStatus | None, dict[str, Any]]:
        """Route 1 for 药师帮: verify the supplier profile before its detail page.

        药师帮 does not expose a public storefront independent of login. Its
        provider profile is therefore the stable store entry point.
        """
        provider_id = str(task.metadata.get("provider_id") or "").strip()
        if not provider_id:
            return CollectionStatus.PAGE_CHANGED, {"route": "provider_profile", "reason": "missing_provider_id"}
        code, _, stderr, data = await self._run(
            "yaoshibang", "shop", provider_id, "-f", "json",
            "--window", "foreground", "--site-session", "persistent", "--keep-tab", "true", timeout=90,
        )
        access = detect_access_state(None, None, stderr)
        if access:
            return access, {"route": "provider_profile", "provider_id": provider_id}
        if code != 0:
            return CollectionStatus.NETWORK_ERROR if "TIMEOUT" in stderr else CollectionStatus.PAGE_CHANGED, {
                "route": "provider_profile", "provider_id": provider_id, "stderr": stderr[:1000],
            }
        profile = data[0] if isinstance(data, list) and data else data
        if not isinstance(profile, dict) or str(profile.get("provider_id") or "") != provider_id:
            return CollectionStatus.PAGE_CHANGED, {"route": "provider_profile", "provider_id": provider_id, "reason": "provider_profile_unverified"}
        return None, {"route": "provider_profile", "provider_id": provider_id, "provider_profile": profile}

    async def _detail(self, task: CollectionTaskSpec, session: BrowserSession) -> CollectionResult:
        product_id = task.product_id
        if not product_id:
            return CollectionResult(
                collection_status=CollectionStatus.PARSE_ERROR,
                error_code="missing_product_id",
            )
        if session.platform == "yaoshibang" and not (task.metadata or {}).get("provider_id"):
            return CollectionResult(
                collection_status=CollectionStatus.PAGE_CHANGED,
                error_code="missing_provider_id",
                error_detail="药师帮详情必须提供 provider_id；请从搜索结果或商家档案补充后人工重排。",
                evidence=EvidenceBundle(raw_fields={"manual_required": True}),
            )
        route_fields: dict[str, Any] = {}
        if session.platform == "taobao" and task.metadata.get("route") == "shop_home" and not task.product_id:
            route_status, route_fields = await self._taobao_shop_search(task, session)
            if route_status:
                return CollectionResult(
                    collection_status=route_status,
                    error_code=route_status.value,
                    error_detail=str(route_fields.get("reason") or "店铺主页路线未完成"),
                    evidence=EvidenceBundle(raw_fields=route_fields),
                )
        if session.platform == "yaoshibang" and task.metadata.get("route") == "provider_profile":
            route_status, route_fields = await self._yaoshibang_shop_profile(task, session)
            if route_status:
                return CollectionResult(
                    collection_status=route_status,
                    error_code=route_status.value,
                    error_detail=str(route_fields.get("reason") or "供应商档案路线未完成"),
                    evidence=EvidenceBundle(raw_fields={**route_fields, "manual_required": route_status == CollectionStatus.PAGE_CHANGED}),
                )
        detail_args = [
            session.platform,
            "detail",
            product_id,
            "-f",
            "json",
            "--trace",
            "retain-on-failure",
            "--window",
            "foreground",
            "--site-session",
            "persistent",
            "--keep-tab",
            "true",
        ]
        # 药师帮需要 provider_id 参数
        if session.platform == "yaoshibang":
            provider_id = (task.metadata or {}).get("provider_id") or ""
            if provider_id:
                detail_args.extend(["--provider_id", provider_id])
            await self._ysb_page_dwell()
        code, _, stderr, data = await self._run(*detail_args)
        fields = parse_detail_fields(data)
        title = fields.get("商品名称") or fields.get("商品标题") or fields.get("title")
        final_url = fields.get("链接") or fields.get("url") or task.url
        screenshot_b64 = await self._capture_current_tab(session)
        access = detect_access_state(title, final_url, stderr)
        if access:
            return CollectionResult(
                collection_status=access,
                page_title=title,
                final_url=final_url,
                error_code=access.value,
                error_detail=stderr[:1000],
                evidence=EvidenceBundle(final_url=final_url, page_title=title, raw_fields={**fields, **route_fields}, screenshot_bytes_b64=screenshot_b64, collector_version="opencli-1.8.6"),
            )
        if code != 0:
            status = CollectionStatus.NETWORK_ERROR if "TIMEOUT" in stderr else CollectionStatus.PARSE_ERROR
            return CollectionResult(collection_status=status, error_detail=stderr[:1000])
        if not fields:
            return CollectionResult(
                collection_status=CollectionStatus.PAGE_CHANGED,
                error_code="empty_structured_result",
                error_detail=stderr[:1000],
            )
        page_shop = fields.get("店铺") or fields.get("shop") or fields.get("供应商名称")
        # OpenCLI's Taobao detail schema currently omits shop identity.  Read it
        # from the rendered page before treating the price as formal evidence.
        if session.platform == "taobao" and task.shop_name and not page_shop:
            expected = normalize_shop_name(task.shop_name)
            shop_js = """JSON.stringify(Array.from(document.querySelectorAll('a[href]')).map(a=>({text:(a.innerText||a.textContent||'').trim(),href:a.href})).filter(x=>/shop\\d+\\.(taobao|tmall)\\.com/i.test(x.href)).slice(0,100))"""
            shop_code, shop_stdout, shop_stderr, shop_data = await self._run("browser", session.alias, "eval", shop_js, timeout=45)
            if shop_code == 0 and not detect_access_state(None, final_url, shop_stderr):
                for link in self._json_list(shop_data, shop_stdout):
                    if isinstance(link, dict) and expected in normalize_shop_name(link.get("text")):
                        page_shop = task.shop_name
                        fields["店铺"] = page_shop
                        fields["店铺主页"] = str(link.get("href") or "")
                        break
        price_raw = fields.get("价格") or fields.get("price")
        selected_spec = fields.get("规格") or fields.get("selected_spec")
        normalized_title = str(title or "").replace("：", ":").replace("×", "*")
        target_spec = normalize_spec(task.spec)
        actual_spec = normalize_spec(selected_spec)
        if not is_valid_detail_page(
            session.platform,
            title=title,
            url=final_url,
            product_id=product_id,
        ):
            status = CollectionStatus.PAGE_CHANGED
        elif task.shop_name and not page_shop:
            # 店铺是正式价格证据的必填字段；无法读取时不得把任务标记成功。
            status = CollectionStatus.STORE_UNVERIFIED
        elif task.shop_name and page_shop and normalize_shop_name(task.shop_name) != normalize_shop_name(page_shop):
            status = CollectionStatus.STORE_MISMATCH
        elif title and any((task.drug_name, task.generic_name)) and not any(
            name and name in title for name in (task.drug_name, task.generic_name)
        ):
            status = CollectionStatus.SKU_MISMATCH
        elif target_spec and actual_spec and target_spec != actual_spec:
            status = CollectionStatus.SKU_MISMATCH
        elif target_spec and not actual_spec and target_spec not in normalized_title:
            status = CollectionStatus.SKU_MISMATCH
        elif not price_raw:
            status = CollectionStatus.PRICE_AMBIGUOUS
        else:
            status = CollectionStatus.SUCCESS
        if status == CollectionStatus.SUCCESS and not screenshot_b64:
            status = CollectionStatus.PAGE_CHANGED
        box_count = parse_price(fields.get("起购数量") or fields.get("minimum_purchase") or fields.get("min_purchase"))
        match = re.search(r"(\d+)\s*盒(?:装|起购|包邮)", str(title or ""))
        if box_count is None and match:
            box_count = Decimal(match.group(1))
        return CollectionResult(
            collection_status=status,
            calculation_status=CalculationStatus.NOT_APPLICABLE,
            price_status=PriceStatus.NOT_EVALUATED,
            page_title=title,
            final_url=final_url,
            page_shop=page_shop,
            selected_spec=selected_spec,
            page_price_raw=price_raw,
            page_price_value=parse_price(price_raw),
            sale_box_count=box_count,
            error_code=(
                None
                if status == CollectionStatus.SUCCESS
                else "missing_screenshot_evidence"
                if status == CollectionStatus.PAGE_CHANGED and not screenshot_b64
                else status.value
            ),
            evidence=EvidenceBundle(
                final_url=final_url,
                page_title=title,
                raw_fields={**fields, **route_fields},
                screenshot_bytes_b64=screenshot_b64,
                collector_version="opencli-1.8.6",
                captured_at=datetime.now(),
            ),
        )

    async def collect_fixed(self, task: CollectionTaskSpec, session: BrowserSession) -> CollectionResult:
        return await self._detail(task, session)

    async def inspect_candidate(self, task: CollectionTaskSpec, session: BrowserSession) -> CollectionResult:
        return await self._detail(task, session)

    async def search(self, query: str, session: BrowserSession, *, limit: int = 20) -> list[SearchHit]:
        args = [
            session.platform,
            "search",
            query,
            "-f",
            "json",
            "--limit",
            str(limit),
        ]
        if session.platform == "taobao":
            args.extend(["--sort", "default"])
        args.extend(["--window", "foreground", "--site-session", "persistent", "--keep-tab", "true"])
        if session.platform == "yaoshibang":
            await self._ysb_page_dwell()
        code, _, stderr, data = await self._run(*args)
        screenshot_b64 = await self._capture_current_tab(session)
        self._search_evidence[session.alias] = EvidenceBundle(
            screenshot_bytes_b64=screenshot_b64,
            raw_fields={"query": query},
            collector_version="opencli-1.8.6",
        )
        access = detect_access_state(None, None, stderr)
        if access:
            raise CollectorAccessError(
                f"{session.platform} search access failure",
                collection_status=access.value,
                details={"stderr": stderr[:1000], "screenshot_bytes_b64": screenshot_b64},
            )
        if code != 0 or not isinstance(data, list):
            raise CollectorAccessError(
                f"{session.platform} search failed",
                collection_status=(CollectionStatus.NETWORK_ERROR if "TIMEOUT" in stderr else CollectionStatus.PARSE_ERROR).value,
                details={"stderr": stderr[:1000], "screenshot_bytes_b64": screenshot_b64},
            )
        hits = []
        for item in data:
            if not isinstance(item, dict):
                continue
            # 药师帮用 wholesale_id + provider_id 作为商品唯一标识
            product_id = str(item.get("sku") or item.get("item_id") or item.get("wholesale_id") or "") or None
            hits.append(
                SearchHit(
                    platform=session.platform,
                    query=query,
                    rank=item.get("rank"),
                    title=str(item.get("title") or ""),
                    url=item.get("url"),
                    product_id=product_id,
                    shop_name=item.get("shop"),
                    list_price_raw=item.get("price"),
                    raw=item,
                )
            )
        return hits

    async def search_store(self, task: CollectionTaskSpec, session: BrowserSession) -> list[SearchHit]:
        """Route 1 through the Python collector, with no global fallback."""
        if session.platform == "yaoshibang":
            provider_id = str(task.metadata.get("provider_id") or "")
            if not provider_id:
                provider_id, resolution = await self._resolve_yaoshibang_provider(task, session)
                if not provider_id:
                    raise CollectorAccessError(
                        "药师帮店铺搜索未能唯一确认 provider_id",
                        collection_status=CollectionStatus.PAGE_CHANGED.value,
                        details={"manual_required": True, "provider_resolution": resolution},
                    )
                task.metadata["provider_id"] = provider_id
                task.metadata["provider_resolution"] = resolution
            status, profile = await self._yaoshibang_shop_profile(task, session)
            if status:
                raise CollectorAccessError("药师帮供应商档案不可用", collection_status=status.value, details=profile)
            hits = [
                hit for hit in await self.search(task.query or "", session, limit=int(task.metadata.get("search_limit", 20)))
                if str(hit.raw.get("provider_id") or "") == provider_id
            ]
            evidence = self._search_evidence.get(session.alias, EvidenceBundle())
            evidence.raw_fields["provider_resolution"] = task.metadata.get("provider_resolution")
            self._search_evidence[session.alias] = evidence
            return hits
        if session.platform != "taobao":
            raise CollectorAccessError("unsupported store-search platform", collection_status=CollectionStatus.PAGE_CHANGED.value)
        home = str(task.metadata.get("shop_home_url") or "")
        query = str(task.query or "").strip()
        search_url = self._shop_search_url(home, query) if home and query else None
        if not search_url or home.rstrip("/") == "https://shop.tmall.com":
            raise CollectorAccessError(
                "淘宝店铺主页尚未验证",
                collection_status=CollectionStatus.PAGE_CHANGED.value,
                details={"manual_required": True},
            )
        # Store-search interaction belongs to the Taobao OpenCLI adapter.  It
        # uses native CDP typing/clicking and reports an explicit non-success
        # row when the page drops the query or escapes to global search.
        code, stdout, stderr, data = await self._run(
            "taobao", "shop-search", query, "--shop_home_url", home, "--expected_shop_name", str(task.shop_name or ""), "--limit", "20",
            "-f", "json", "--window", "foreground", "--site-session", "persistent", "--keep-tab", "true", timeout=120,
        )
        access = detect_access_state(None, home, stderr)
        if access:
            raise CollectorAccessError("淘宝店铺搜索访问受限", collection_status=access.value, details={"stderr": stderr[:1000]})
        rows = data if isinstance(data, list) else self._json_list(data, stdout)
        first = rows[0] if rows and isinstance(rows[0], dict) else {}
        if code != 0 or not rows:
            raise CollectorAccessError("淘宝店内搜索适配器执行失败", collection_status=CollectionStatus.PARSE_ERROR.value, details={"stderr": stderr[:1000]})
        if first.get("status") != "success":
            raise CollectorAccessError(
                "淘宝店内搜索未形成有效结果",
                collection_status=CollectionStatus.NOT_FOUND.value,
                details={"query": query, "query_verified": bool(first.get("query_verified")),
                         "reason": first.get("reason") or "store_search_unknown", "final_url": first.get("current_url"),
                         "shop_name": first.get("shop_name")},
            )
        self._search_evidence[session.alias] = EvidenceBundle(
            raw_fields={"query": query, "query_verified": True, "route": "shop_home", "shop_home_url": home,
                        "current_url": first.get("current_url"), "adapter": "taobao/shop-search"},
            collector_version="opencli-1.8.6",
        )
        return [
            SearchHit(platform="taobao", query=query, rank=item.get("rank"), title=str(item.get("title") or ""),
                      url=item.get("url"), product_id=str(item.get("item_id") or "") or None,
                      shop_name=str(item.get("shop_name") or task.shop_name or ""), list_price_raw=None,
                      raw={"route": "shop_home", "shop_home_url": home, "adapter": "taobao/shop-search"})
            for item in rows if isinstance(item, dict) and item.get("status") == "success" and item.get("item_id")
        ]
        code, _, stderr, _ = await self._run("browser", session.alias, "open", home, "--window", "foreground", timeout=60)
        access = detect_access_state(None, home, stderr)
        if access:
            raise CollectorAccessError("淘宝店铺搜索访问受限", collection_status=access.value, details={"stderr": stderr[:1000]})
        if code != 0:
            raise CollectorAccessError("淘宝店铺页面无法打开", collection_status=CollectionStatus.NETWORK_ERROR.value, details={"stderr": stderr[:1000]})
        interaction = f"""(() => {{
          const q={json.dumps(query)};
          const input=[...document.querySelectorAll('input')].find(x=>/搜索|search/i.test(x.placeholder||'')||x.type==='search');
          if(!input) return {{ok:false,reason:'search_input_not_found'}};
          input.focus(); input.value=q; input.dispatchEvent(new Event('input',{{bubbles:true}})); input.dispatchEvent(new Event('change',{{bubbles:true}}));
          const form=input.closest('form');
          // The AliHealth shop page also contains Tmall's global search form.
          // Restrict the click to the local input's sibling/ancestor, never a
          // document-wide “搜索” control.
          const button=input.parentElement?.querySelector('.search-local,button,[type=submit]') ||
            input.closest('.header-extra')?.querySelector('.search-local');
          if(button) button.click(); else if(form) form.requestSubmit?.(); else input.dispatchEvent(new KeyboardEvent('keydown',{{key:'Enter',bubbles:true}}));
          return {{ok:true,value:input.value}};
        }})()"""
        code, stdout, stderr, data = await self._run("browser", session.alias, "eval", interaction, timeout=45)
        if code != 0 or not isinstance(data, dict) or data.get("value") != query:
            raise CollectorAccessError("淘宝店铺搜索词未生效", collection_status=CollectionStatus.NOT_FOUND.value, details={"query": query, "raw": data or stdout[:500]})
        await asyncio.sleep(random.uniform(1.5, 3.0))
        state_js = "JSON.stringify({url:location.href,title:document.title,local_value:(document.querySelector('input[placeholder=\\\"搜索本店商品\\\"]')||{}).value||''})"
        state_code, state_stdout, state_stderr, state_data = await self._run("browser", session.alias, "eval", state_js, timeout=45)
        state = state_data if isinstance(state_data, dict) else {}
        if state_code != 0 or state.get("local_value") != query:
            raise CollectorAccessError(
                "淘宝店内搜索跳转后未保持搜索词",
                collection_status=CollectionStatus.NOT_FOUND.value,
                details={"query": query, "query_verified": False, "reason": "store_search_navigation_lost",
                         "final_url": state.get("url"), "page_title": state.get("title"), "raw": state or state_stdout[:500]},
            )
        js = "JSON.stringify(Array.from(document.querySelectorAll('a[href*=\"item.htm\"]')).slice(0,50).map(a=>({title:(a.innerText||a.textContent||'').trim(),url:a.href,product_id:((a.href.match(/[?&]id=(\\d+)/)||[])[1]||'')})))"
        code, stdout, stderr, _ = await self._run("browser", session.alias, "eval", js, timeout=45)
        access = detect_access_state(None, home, stderr)
        if access:
            raise CollectorAccessError("淘宝店铺搜索访问受限", collection_status=access.value, details={"stderr": stderr[:1000]})
        try:
            match = re.search(r"\[[\s\S]*\]", stdout if code == 0 else stderr)
            rows = json.loads(match.group(0)) if match else []
        except json.JSONDecodeError:
            rows = []
        self._search_evidence[session.alias] = EvidenceBundle(raw_fields={"query": query, "query_verified": True, "route": "shop_home", "shop_home_url": home})
        hits = [
            SearchHit(platform="taobao", query=task.query or "", rank=index, title=str(row.get("title") or ""),
                      url=row.get("url"), product_id=str(row.get("product_id") or "") or None,
                      shop_name=task.shop_name, raw={"route": "shop_home", "shop_home_url": home})
            for index, row in enumerate(rows, 1)
            if isinstance(row, dict) and str(row.get("title") or "").strip() and str(row.get("product_id") or "").strip()
        ]
        if not hits:
            raise CollectorAccessError(
                "淘宝店铺搜索无有效候选",
                collection_status=CollectionStatus.NOT_FOUND.value,
                details={"query": query, "query_verified": True, "candidate_count": len(rows)},
            )
        target_terms = (str(task.drug_name or ""), str(task.generic_name or ""))
        matched = [hit for hit in hits if all(term and term in hit.title for term in target_terms)]
        if not matched:
            raise CollectorAccessError("淘宝店铺未找到品牌和通用名匹配候选", collection_status=CollectionStatus.NOT_FOUND.value, details={"query": query, "query_verified": True, "candidate_count": len(hits)})
        return matched

    async def _resolve_yaoshibang_provider(
        self, task: CollectionTaskSpec, session: BrowserSession
    ) -> tuple[str | None, list[dict[str, Any]]]:
        """Resolve only a unique exact provider match; leave ambiguity to humans."""
        shop_name = str(task.shop_name or "").strip()
        if not shop_name:
            return None, []
        code, _, stderr, data = await self._run(
            "yaoshibang", "resolve-provider", shop_name, "-f", "json",
            "--window", "foreground", "--site-session", "persistent", "--keep-tab", "true", timeout=120,
        )
        access = detect_access_state(None, None, stderr)
        if access:
            raise CollectorAccessError("药师帮供应商解析访问受限", collection_status=access.value, details={"stderr": stderr[:1000]})
        candidates = [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        exact_ids = {str(item.get("provider_id") or "") for item in candidates if item.get("match_confidence") == "exact" and item.get("provider_id")}
        if code == 0 and len(exact_ids) == 1:
            return next(iter(exact_ids)), candidates
        return None, candidates

    def last_search_evidence(self, session: BrowserSession) -> EvidenceBundle:
        return self._search_evidence.get(session.alias, EvidenceBundle())

    async def resume_incident(self, incident_id: str, session: BrowserSession) -> CollectionResult:
        # Incident resolution first revalidates login. The original task is requeued
        # by IncidentService and collected through collect_fixed/inspect_candidate.
        return await self.health_check(session)
