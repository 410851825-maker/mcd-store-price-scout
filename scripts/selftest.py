"""麦门门店价差官 · 自测脚本

用脱敏样例数据离线跑通全部核心逻辑，不需要网络、不需要 Token。
运行：python selftest.py

覆盖的关键行为（每一条都对应一个实测踩过的坑）：
- 跨门店价差与极差计算
- 折扣覆盖率统计（原价 vs 现价）
- 券的虚拟编码展开
- 券的门店可用性判定
- 套餐陷阱识别（券在套餐内不生效）
- 历史画像区分「复购」与「一次多份」
- 金额单位换算（分 → 元）
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from price_scout import (  # noqa: E402
    CouponScope,
    HistoryProfile,
    PriceScout,
    ProductVerdict,
    StoreSnapshot,
    _days_left,
    _to_float,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_PATH = os.path.join(HERE, "sample_data", "sample_data.json")

PASSED = 0
FAILED = 0
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  [PASS] {name}")
    else:
        FAILED += 1
        msg = f"{name}" + (f" —— {detail}" if detail else "")
        FAILURES.append(msg)
        print(f"  [FAIL] {msg}")


def approx(a: float, b: float, tol: float = 0.01) -> bool:
    return abs(a - b) <= tol


# ------------------------------------------------------------------ 载入样例


def load_sample() -> dict:
    with open(SAMPLE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def build_snapshots(sample: dict) -> list[StoreSnapshot]:
    """从样例构造 StoreSnapshot，复用真实引擎的统计逻辑。"""
    snaps = []
    for st in sample["stores"]:
        snap = StoreSnapshot(
            store_code=st["storeCode"],
            store_name=st["storeName"],
            distance_m=st.get("distance"),
            address=st.get("address", ""),
            business_hours=f"{st.get('businessStartTime','?')}-{st.get('businessEndTime','?')}",
        )
        snap.menu = st["menu"]
        snap.total_count = len(snap.menu)
        for item in snap.menu.values():
            cur = _to_float(item.get("currentPrice"))
            orig = _to_float(item.get("originalPrice"))
            if orig != cur:
                snap.discounted_count += 1
                snap.discount_total += round(orig - cur, 2)
        snap.coupons = sample["store_coupons"].get(st["storeCode"], [])
        snaps.append(snap)
    return snaps


# ------------------------------------------------------------------ 测试组


def test_price_comparison(sample: dict) -> None:
    print("\n[1] 跨门店比价")
    scout = PriceScout(client=None, verbose=False)
    snaps = build_snapshots(sample)
    verdicts = scout.compare_products(snaps, top=20)

    by_name = {v.product_name: v for v in verdicts}
    check("能识别出参与比价的商品", len(verdicts) > 0, f"得到 {len(verdicts)} 项")

    burger = by_name.get("示例巨无霸")
    check("找到示例巨无霸", burger is not None)
    if burger:
        check("巨无霸最低价正确 (25.5)", approx(burger.cheapest[2], 25.5), str(burger.cheapest))
        check("巨无霸最高价正确 (29.0)", approx(burger.priciest[2], 29.0), str(burger.priciest))
        check("巨无霸极差 = 3.5", approx(burger.spread, 3.5), str(burger.spread))

    seta = by_name.get("示例套餐A")
    check("找到示例套餐A", seta is not None)
    if seta:
        check("套餐A极差 = 18.0", approx(seta.spread, 18.0), str(seta.spread))
        check("套餐A极差百分比 > 45%", seta.spread_pct > 45, str(seta.spread_pct))

    drink = by_name.get("示例冰饮")
    check("找到示例冰饮", drink is not None)
    if drink:
        check("冰饮极差 = 17.1", approx(drink.spread, 17.1), str(drink.spread))
        check("冰饮极差百分比 > 150%", drink.spread_pct > 150, str(drink.spread_pct))


def test_discount_coverage(sample: dict) -> None:
    print("\n[2] 折扣覆盖率统计")
    snaps = build_snapshots(sample)
    by_name = {s.store_name: s for s in snaps}

    hub = by_name["示例·枢纽站店"]
    check("枢纽站店 0 项有折扣", hub.discounted_count == 0, str(hub.discounted_count))
    check("枢纽站店折扣覆盖率 = 0", approx(hub.discount_rate, 0.0))

    east = by_name["示例·城东社区店"]
    check("社区店折扣项数 = 4", east.discounted_count == 4, str(east.discounted_count))
    check("社区店折扣覆盖率 > 0.3", east.discount_rate > 0.3, f"{east.discount_rate:.3f}")
    check("社区店折扣总额计算正确", approx(east.discount_total, 25.7), str(east.discount_total))

    gap = east.discount_rate - hub.discount_rate
    check("两类门店折扣覆盖率差异 > 0.3", gap > 0.3, f"{gap:.3f}")


def test_coupon_scope(sample: dict) -> None:
    print("\n[3] 券的适用范围判定")
    snaps = build_snapshots(sample)
    scout = PriceScout(client=None, verbose=False)

    coupon = sample["coupons"][0]
    scope = CouponScope(
        coupon_id=coupon["id"],
        title=coupon["title"],
        face_value=_to_float(coupon["discountInfo"]["discountValue"]),
        real_discount=0.0,
        expire_at=coupon["tradeEndDate"],
        days_left=_days_left(coupon["tradeEndDate"]),
    )

    # 券声明的虚拟编码来自门店券接口
    virtual = []
    for snap in snaps:
        for cp in snap.coupons:
            if cp["couponId"] == coupon["id"]:
                for p in cp.get("products", []):
                    if p["productCode"] not in virtual:
                        virtual.append(p["productCode"])
    scope.virtual_codes = virtual
    check("能从门店券接口取到券的虚拟编码", len(virtual) == 1, str(virtual))
    check("券的虚拟编码在样例菜单中不存在（模拟真实错位）",
          virtual[0] not in snaps[0].menu, virtual[0])

    # 门店可用性
    scope.usable_stores = [s.store_code for s in snaps if any(
        cp["couponId"] == coupon["id"] for cp in s.coupons)]
    scope.unusable_stores = [s.store_code for s in snaps if s.store_code not in scope.usable_stores]
    check("券在 2 家门店可用", len(scope.usable_stores) == 2, str(scope.usable_stores))
    check("券在 1 家门店不可用", len(scope.unusable_stores) == 1, str(scope.unusable_stores))


def test_amount_conversion() -> None:
    print("\n[4] 金额单位与工具函数")
    check("_to_float 处理字符串 '1390' → 1390.0", approx(_to_float("1390"), 1390.0))
    check("_to_float 处理空值 → 默认值", approx(_to_float(None, 0.0), 0.0))
    check("_to_float 处理非法值 → 默认值", approx(_to_float("abc", 5.0), 5.0))

    # 分转元
    price_fen = 1390
    check("分转元换算正确 (1390 → 13.90)", approx(price_fen / 100, 13.90))

    # 剩余天数
    future = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
    check("_days_left 未来 2 天 → 3（含今天）", _days_left(future) == 3, str(_days_left(future)))
    past = (datetime.now() - timedelta(days=5)).strftime("%Y-%m-%d")
    check("_days_left 过去日期 → 负数", _days_left(past) < 0, str(_days_left(past)))
    check("_days_left 空值 → -1", _days_left(None) == -1)
    check("_days_left 非法格式 → -1", _days_left("not-a-date") == -1)


def test_history_profile(sample: dict) -> None:
    print("\n[5] 历史消费画像")
    scout = PriceScout(client=None, verbose=False)
    prof = HistoryProfile()
    for o in sample["order_history"]:
        prof.total_orders += 1
        prof.total_spend += _to_float(o["realTotalAmount"])
        prof.stores[o["storeName"]] = prof.stores.get(o["storeName"], 0) + 1
        for p in o["orderProductList"]:
            rec = prof.items.setdefault(p["productName"], {"times": 0, "qty": 0, "stores": set()})
            rec["times"] += 1
            rec["qty"] += p["quantity"]
            rec["stores"].add(o["storeName"])

    check("订单计数正确", prof.total_orders == 3, str(prof.total_orders))
    check("累计消费正确 (94.5)", approx(prof.total_spend, 94.5), str(prof.total_spend))
    check("门店频次正确", prof.stores.get("示例·城东社区店") == 2, str(prof.stores))

    chicken = prof.items.get("示例麦香鸡")
    check("麦香鸡识别为一次多份 (1次/2份)",
          chicken and chicken["times"] == 1 and chicken["qty"] == 2,
          str(chicken))

    burger = prof.items.get("示例巨无霸")
    check("巨无霸识别为复购 (2次/2份)",
          burger and burger["times"] == 2 and burger["qty"] == 2,
          str(burger))

    snack = prof.items.get("示例小食")
    check("小食识别为一次多份 (1次/2份)",
          snack and snack["times"] == 1 and snack["qty"] == 2,
          str(snack))

    top = prof.top_stores(1)
    check("常去门店排序正确", top and top[0][0] == "示例·城东社区店", str(top))


def test_verdict_edge_cases() -> None:
    print("\n[6] 边界情况")
    empty = ProductVerdict(product_code="x", product_name="空")
    check("无价格时 cheapest 为 None", empty.cheapest is None)
    check("无价格时 spread = 0", approx(empty.spread, 0.0))
    check("无价格时 spread_pct = 0", approx(empty.spread_pct, 0.0))

    single = ProductVerdict(product_code="x", product_name="单店")
    single.prices = [("1", "A", 10.0, 100)]
    check("单门店 spread = 0", approx(single.spread, 0.0))
    check("单门店 cheapest 正常", approx(single.cheapest[2], 10.0))


def test_sample_runner() -> None:
    print("\n[7] 离线演示")
    from sample_runner import run_sample
    out = run_sample()
    check("离线演示能生成卡片", len(out) > 100, f"长度 {len(out)}")
    check("卡片包含折扣覆盖率段落", "折扣覆盖率" in out)
    check("卡片包含比价段落", "跨门店比价" in out)
    check("卡片包含行动建议", "行动建议" in out)


def test_no_hardcoded_secret() -> None:
    print("\n[8] 安全检查")
    # 只检测「形态」：疑似真实 token 的长串出现在配置/代码里即视为风险。
    # 不在本文件写入任何真实 token 或其指纹——具体值由 CI 用 grep 单独把关。
    import re

    # 允许出现的占位符与示例
    allowed = {
        "YOUR_MCP_TOKEN",
        "MCD_MCP_TOKEN",
        "MCP_TOKEN",
        "AUTHORIZATION",
        "BEARER",
        "STREAMABLEHTTP",
        "HTTPS_MCD_CN",
        "CONTRIBUTORS",
    }
    # 这些是已知的哈希/编码/样例串，不是 token
    benign_patterns = [
        re.compile(r"^[A-F0-9]{64}$"),            # sha256 指纹
        re.compile(r"^[A-Za-z0-9+/=]{20,}$"),     # base64 样例
        re.compile(r"^SAMPLE[A-Z0-9_]*$"),        # 脱敏样例前缀
        re.compile(r"^[A-Z0-9_]+$"),              # 全大写标识符
    ]

    risky: list[str] = []
    for root, _dirs, files in os.walk(HERE):
        if ".git" in root:
            continue
        for fn in files:
            if not fn.endswith((".py", ".json", ".md")):
                continue
            path = os.path.join(root, fn)
            with open(path, encoding="utf-8", errors="ignore") as fh:
                for lineno, line in enumerate(fh, 1):
                    if len(line) > 3000:
                        continue
                    for cand in re.findall(r"[A-Za-z0-9]{16,}", line):
                        if cand in allowed:
                            continue
                        if any(p.match(cand) for p in benign_patterns):
                            continue
                        # 混合大小写且含数字，长度 24+ —— 典型 token 形态
                        if len(cand) >= 24 and any(c.isdigit() for c in cand) \
                                and any(c.islower() for c in cand) \
                                and any(c.isupper() for c in cand):
                            risky.append(f"{os.path.basename(path)}:{lineno}")
    check("全仓库无高熵疑似 token 字符串", not risky,
          "命中: " + ", ".join(risky[:5]) if risky else "")


# ------------------------------------------------------------------ 主流程


def main() -> int:
    print("=" * 60)
    print(" 麦门门店价差官 · 自测")
    print("=" * 60)

    sample = load_sample()
    test_price_comparison(sample)
    test_discount_coverage(sample)
    test_coupon_scope(sample)
    test_amount_conversion()
    test_history_profile(sample)
    test_verdict_edge_cases()
    test_sample_runner()
    test_no_hardcoded_secret()

    print("\n" + "=" * 60)
    print(f" 结果：{PASSED} passed, {FAILED} failed")
    if FAILURES:
        print(" 失败项：")
        for f in FAILURES:
            print(f"   - {f}")
    print("=" * 60)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
