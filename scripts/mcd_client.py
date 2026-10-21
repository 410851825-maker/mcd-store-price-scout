"""麦当劳 MCP Server 客户端（零依赖，仅用 Python 标准库）。

设计要点
--------
1. 只用标准库 urllib，不引入任何第三方包，方便评委离线跑通。
2. Token 只从环境变量 MCD_MCP_TOKEN 读取，绝不硬编码、绝不写进仓库。
3. 统一封装 tools/call，返回结构化 dict，失败时抛 McdError 而非静默返回空。
4. 内置限流保护（每分钟 600 次），避免触发 429。

作者：麦门门店价差官 mcd-store-price-scout
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Any

MCP_URL = os.environ.get("MCD_MCP_URL", "https://mcp.mcd.cn")
TOKEN_ENV = "MCD_MCP_TOKEN"

# 官方限流：每个 Token 每分钟最多 600 次请求
RATE_LIMIT_PER_MIN = 600


class McdError(RuntimeError):
    """MCP 调用失败。message 里保留官方错误码，便于排查。"""


class McdClient:
    """麦当劳 MCP Server 的轻量客户端。"""

    def __init__(self, token: str | None = None, url: str | None = None, verbose: bool = False):
        self.token = token or os.environ.get(TOKEN_ENV, "")
        self.url = url or MCP_URL
        self.verbose = verbose
        self._call_times: list[float] = []
        self._seq = 0
        self.call_count = 0          # 累计工具调用次数（用于观测接口用量）
        self.tool_counts: dict[str, int] = {}  # 各工具调用次数

        if not self.token:
            raise McdError(
                f"未找到 MCP Token。请先申请 Token（https://open.mcd.cn/mcp），"
                f"然后设置环境变量 {TOKEN_ENV}，例如：\n"
                f"  PowerShell: $env:{TOKEN_ENV}='你的Token'\n"
                f"  Bash:       export {TOKEN_ENV}='你的Token'"
            )

    # ------------------------------------------------------------------ 内部

    def _throttle(self) -> None:
        """滑动窗口限流，逼近上限时主动等待，避免 429。"""
        now = time.monotonic()
        self._call_times = [t for t in self._call_times if now - t < 60.0]
        if len(self._call_times) >= RATE_LIMIT_PER_MIN:
            sleep_for = 60.0 - (now - self._call_times[0]) + 0.5
            if self.verbose:
                print(f"[mcp] 接近限流上限，等待 {sleep_for:.1f}s ...")
            time.sleep(max(sleep_for, 0.1))
            now = time.monotonic()
            self._call_times = [t for t in self._call_times if now - t < 60.0]
        self._call_times.append(now)

    def _rpc(self, method: str, params: dict[str, Any], timeout: int = 40) -> dict[str, Any]:
        self._seq += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._seq,
            "method": method,
            "params": params,
        }
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )

        self._throttle()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise McdError(f"HTTP {exc.code}：{detail}") from exc
        except urllib.error.URLError as exc:
            raise McdError(f"网络请求失败：{exc.reason}") from exc

        # streamablehttp 可能返回 SSE 前缀，剥掉 "data: " 行
        text = raw
        if "data:" in raw:
            chunks = [ln[len("data:"):].strip() for ln in raw.splitlines() if ln.startswith("data:")]
            text = chunks[-1] if chunks else raw

        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise McdError(f"响应不是合法 JSON：{text[:200]}") from exc

        if "error" in data:
            err = data["error"]
            raise McdError(f"MCP 返回错误：{err.get('message', err)}")
        return data.get("result", {})

    # ------------------------------------------------------------------ 公开

    def call(self, tool: str, **arguments: Any) -> Any:
        """调用一个 MCP 工具，返回 structuredContent.data。

        失败时抛 McdError，调用方需自行决定是否降级。
        """
        self.call_count += 1
        self.tool_counts[tool] = self.tool_counts.get(tool, 0) + 1
        result = self._rpc("tools/call", {"name": tool, "arguments": arguments})
        if result.get("isError"):
            text = "".join(
                c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"
            )
            raise McdError(f"工具 {tool} 返回错误：{text[:300]}")

        sc = result.get("structuredContent")
        if sc is None:
            text = "".join(
                c.get("text", "") for c in result.get("content", []) if c.get("type") == "text"
            )
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return text

        # 官方约定：success=False 时 message 里带原因
        if sc.get("success") is False:
            raise McdError(f"工具 {tool} 失败[{sc.get('code')}]：{sc.get('message')}")
        return sc.get("data")

    def call_safe(self, tool: str, **arguments: Any) -> Any:
        """调用失败时返回 None 而不抛异常，用于「可选数据源」场景。"""
        try:
            return self.call(tool, **arguments)
        except McdError as exc:
            if self.verbose:
                print(f"[mcp] {tool} 调用失败，忽略：{exc}")
            return None

    def list_tools(self) -> list[dict[str, Any]]:
        """列出服务端所有工具（自检用）。"""
        result = self._rpc("tools/list", {})
        return result.get("tools", [])


# ------------------------------------------------------------------ 便捷封装
# 统一封装常用工具的调用路径与已知的前置约束，减少调用方的心智负担。


def get_menu(client: McdClient, store_code: str, order_type: int = 1, be_type: int = 1,
             be_code: str | None = None) -> dict[str, dict[str, Any]]:
    """拉取门店菜单，返回 {productCode: {name, currentPrice, originalPrice, ...}}。

    order_type=1(到店) / be_type=1(到店自取) 时不需要 beCode；
    外送场景(order_type=2) 必须传 beCode，否则服务端报 600048。
    """
    args: dict[str, Any] = {"storeCode": store_code, "orderType": order_type, "beType": be_type}
    if be_code:
        args["beCode"] = be_code
    data = client.call("query-meals", **args)
    return (data or {}).get("meals", {}) or {}


def get_store_coupons(client: McdClient, store_code: str, order_type: int = 1, be_type: int = 1,
                      be_code: str | None = None) -> list[dict[str, Any]]:
    """拉取指定门店当前可用的优惠券列表（含 products 适用范围）。"""
    args: dict[str, Any] = {"storeCode": store_code, "orderType": order_type, "beType": be_type}
    if be_code:
        args["beCode"] = be_code
    data = client.call_safe("query-store-coupons", **args)
    return data if isinstance(data, list) else []


def get_nearby_stores(client: McdClient, city: str, keyword: str, be_type: int = 1,
                      search_type: int = 2) -> list[dict[str, Any]]:
    """按城市+关键词搜索门店。be_type=1 到店自取 / 5 得来速（得来速返回 beCode）。"""
    data = client.call_safe(
        "query-nearby-stores",
        searchType=search_type, beType=be_type, city=city, keyword=keyword,
    )
    return data if isinstance(data, list) else []


def get_order_history(client: McdClient) -> list[dict[str, Any]]:
    """历史订单（非商城）。用于提取用户复购偏好。"""
    data = client.call_safe("order-list")
    if isinstance(data, dict):
        return data.get("list", []) or []
    return []


def get_my_coupons(client: McdClient) -> list[dict[str, Any]]:
    """用户卡包中的优惠券资产。"""
    data = client.call_safe("query-my-coupons")
    if isinstance(data, dict):
        return data.get("coupons", []) or []
    return []


def get_my_account(client: McdClient) -> dict[str, Any]:
    """用户麦享会积分账户。"""
    data = client.call_safe("query-my-account")
    return data if isinstance(data, dict) else {}


def get_meal_detail(client: McdClient, store_code: str, code: str, order_type: int = 1,
                     be_type: int = 1, be_code: str | None = None) -> dict[str, Any]:
    """餐品详情。关键用途：展开券指向的「虚拟编码」为真实商品编码。

    结果按 (storeCode, code) 缓存——同一套餐详情可能被多张券、多个环节重复查询，
    缓存可显著减少接口调用。菜单在不同门店间高度相似，缓存命中率很高。
    """
    key = (store_code, code, order_type, be_type, be_code or "")
    cache = getattr(client, "_detail_cache", None)
    if cache is None:
        cache = {}
        client._detail_cache = cache  # type: ignore[attr-defined]
    if key in cache:
        return cache[key]

    args: dict[str, Any] = {"storeCode": store_code, "orderType": order_type, "beType": be_type, "code": code}
    if be_code:
        args["beCode"] = be_code
    data = client.call_safe("query-meal-detail", **args)
    result = data if isinstance(data, dict) else {}
    cache[key] = result
    return result


def calculate_price(client: McdClient, store_code: str, items: list[dict[str, Any]],
                    coupon_id: str | None = None, coupon_code: str | None = None,
                    order_type: int = 1, be_type: int = 1, be_code: str | None = None) -> dict[str, Any]:
    """价格计算。

    重要：items 里的商品字段名是 productCode（不是 code）；券要传在顶层而非 items 内；
    返回金额单位是「分」。
    """
    args: dict[str, Any] = {"storeCode": store_code, "orderType": order_type, "beType": be_type, "items": items}
    if be_code:
        args["beCode"] = be_code
    if coupon_id:
        args["couponId"] = coupon_id
    if coupon_code:
        args["couponCode"] = coupon_code
    data = client.call_safe("calculate-price", **args)
    return data if isinstance(data, dict) else {}


if __name__ == "__main__":
    # 自检：验证 Token 有效且服务端可达
    cli = McdClient(verbose=True)
    tools = cli.list_tools()
    print(f"连接成功，麦当劳 MCP Server 共 {len(tools)} 个工具")
    try:
        acct = get_my_account(cli)
        if acct:
            print(f"积分账户：可用 {acct.get('availablePoint')}，累计 {acct.get('accumulativePoint')}")
    except McdError as exc:
        print(f"账号查询失败：{exc}")
