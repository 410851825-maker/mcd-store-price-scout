"""离线演示运行器：用脱敏样例数据跑出完整决策卡。

目的：
1. 让评委不申请 Token、不联网也能看到工具输出长什么样
2. 作为自测的一部分
3. README 里的示例输出由它生成，保证文档与代码一致

运行：python sample_runner.py
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from price_scout import (  # noqa: E402
    HistoryProfile,
    PriceScout,
    ProductVerdict,
    StoreSnapshot,
    _days_left,
    _to_float,
    render_card,
)

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_PATH = os.path.join(HERE, "sample_data", "sample_data.json")


def load_sample() -> dict:
    with open(SAMPLE_PATH, encoding="utf-8") as fh:
        return json.load(fh)


def build_result(sample: dict, product_keyword: str | None = None) -> dict:
    """从样例构造与真实运行同构的结果对象。"""
    scout = PriceScout(client=None, verbose=False)

    snaps: list[StoreSnapshot] = []
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

    verdicts = scout.compare_products(snaps, name_filter=product_keyword, top=8)

    # 券的适用范围
    from price_scout import CouponScope
    scopes: list[CouponScope] = []
    for coupon in sample["coupons"]:
        virtual: list[str] = []
        for snap in snaps:
            for cp in snap.coupons:
                if cp.get("couponId") == coupon["id"]:
                    for p in cp.get("products", []) or []:
                        if p["productCode"] not in virtual:
                            virtual.append(p["productCode"])
        if not virtual:
            continue
        scope = CouponScope(
            coupon_id=coupon["id"],
            title=coupon["title"],
            face_value=_to_float(coupon.get("discountInfo", {}).get("discountValue")),
            real_discount=7.1,
            expire_at=coupon.get("tradeEndDate", ""),
            days_left=_days_left(coupon.get("tradeEndDate")),
        )
        scope.virtual_codes = virtual
        # 展开：样例中虚拟编码映射到菜单里的 900000003
        scope.real_codes = ["900000003"]
        scope.real_names = ["示例冰饮中杯（冰/热）"]
        scope.usable_stores = [s.store_code for s in snaps if any(
            cp.get("couponId") == coupon["id"] for cp in s.coupons)]
        scope.unusable_stores = [s.store_code for s in snaps
                                 if s.store_code not in scope.usable_stores]
        scope.trap_set_names = ["示例套餐A"]
        scopes.append(scope)

    # 历史画像
    prof = HistoryProfile()
    for o in sample["order_history"]:
        prof.total_orders += 1
        prof.total_spend += _to_float(o.get("realTotalAmount"))
        store = o.get("storeName", "")
        prof.stores[store] = prof.stores.get(store, 0) + 1
        for p in o.get("orderProductList", []) or []:
            rec = prof.items.setdefault(p["productName"], {"times": 0, "qty": 0, "stores": set()})
            rec["times"] += 1
            rec["qty"] += int(p.get("quantity", 1))
            rec["stores"].add(store)

    # 跨片区对比
    cross = {
        "areas": {
            "示例·社区片区": {
                "discounted": sum(s.discounted_count for s in snaps[:2]),
                "total": sum(s.total_count for s in snaps[:2]),
                "rate": round(sum(s.discounted_count for s in snaps[:2]) /
                              max(sum(s.total_count for s in snaps[:2]), 1), 4),
                "stores": [{"store_code": s.store_code, "store_name": s.store_name,
                            "distance_m": s.distance_m, "discounted": s.discounted_count,
                            "total": s.total_count} for s in snaps[:2]],
            },
            "示例·枢纽片区": {
                "discounted": snaps[2].discounted_count,
                "total": snaps[2].total_count,
                "rate": round(snaps[2].discount_rate, 4),
                "stores": [{"store_code": snaps[2].store_code,
                            "store_name": snaps[2].store_name,
                            "distance_m": snaps[2].distance_m,
                            "discounted": snaps[2].discounted_count,
                            "total": snaps[2].total_count}],
            },
        },
        "verdicts": scout.compare_products(snaps, name_filter=product_keyword, top=5),
    }

    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "query": {"city": "示例市", "keyword": "全城", "product": product_keyword},
        "stores": snaps,
        "verdicts": verdicts,
        "coupon_scopes": scopes,
        "history": prof,
        "cross_area": cross,
    }


def run_sample(product_keyword: str | None = None) -> str:
    """生成离线演示卡片文本。"""
    sample = load_sample()
    result = build_result(sample, product_keyword)
    return render_card(result)


if __name__ == "__main__":
    kw = sys.argv[1] if len(sys.argv) > 1 else None
    print(run_sample(kw))
