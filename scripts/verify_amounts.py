"""金额精度验证：用真实数据核对程序输出的每一个金额。

验证三件事：
1. 菜单价（元）与算价结果（分→元）是否一致
2. 折扣额计算是否精确到分
3. 券的适用性判定是否基于整数比较而非浮点
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcd_client import McdClient, calculate_price, get_menu, get_store_coupons  # noqa: E402
from price_scout import _to_float  # noqa: E402

PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  [PASS] {name}" + (f"  ({detail})" if detail else ""))
    else:
        FAILED += 1
        print(f"  [FAIL] {name}  {detail}")


def main() -> int:
    client = McdClient(verbose=False)
    store = "1400147"

    print("=" * 62)
    print(" 金额精度验证（真实数据）")
    print("=" * 62)

    print("\n[1] 菜单价（元）vs 算价结果（分→元）")
    menu = get_menu(client, store)
    targets = [c for c in ("1100", "515946", "1106") if c in menu]
    for code in targets:
        item = menu[code]
        menu_price = _to_float(item.get("currentPrice"))
        res = calculate_price(client, store, items=[{"productCode": code, "quantity": 1}])
        calc = _to_float(res.get("price"))
        orig = _to_float(res.get("originalPrice"))
        check(f"{code} {item.get('name')} 算价与菜单现价一致",
              abs(calc / 100 - menu_price) < 0.005,
              f"算价 {calc}/100={calc/100:.2f} vs 菜单 {menu_price}")
        check(f"{code} 算价原价与菜单原价一致",
              abs(orig / 100 - _to_float(item.get("originalPrice"))) < 0.005,
              f"{orig/100:.2f} vs {_to_float(item.get('originalPrice'))}")
        check(f"{code} 优惠额 = 原价 - 现价",
              abs((orig - calc) / 100 - round(_to_float(item.get("originalPrice")) - menu_price, 2)) < 0.005,
              f"{(orig-calc)/100:.2f}")

    print("\n[2] 折扣额精度（原价 - 现价，保留两位）")
    bad = 0
    for code, item in menu.items():
        cur = _to_float(item.get("currentPrice"))
        orig = _to_float(item.get("originalPrice"))
        if orig == cur:
            continue
        diff = orig - cur
        if abs(diff - round(diff, 2)) > 1e-9:
            bad += 1
    check("所有折扣额均为两位小数（无浮点尾数）", bad == 0, f"异常 {bad} 项")

    print("\n[3] 券适用性判定基于整数比较")
    coupons = get_store_coupons(client, store)
    if coupons:
        cid = coupons[0]["couponId"]
        # 单点真实编码 → 券应生效
        res_ok = calculate_price(client, store,
                                 items=[{"productCode": "515946", "quantity": 1}],
                                 coupon_id=cid)
        disc = res_ok.get("discount")
        check("discount 为整数（分），可直接判 0",
              isinstance(disc, int) and disc > 0, f"discount={disc} → ¥{disc/100:.2f}")
        check("实付 = 原价 - 优惠（分单位自洽）",
              res_ok["price"] == res_ok["originalPrice"] - disc,
              f"{res_ok['price']} == {res_ok['originalPrice']} - {disc}")

        # 套餐 → 券不生效
        res_set = calculate_price(client, store,
                                  items=[{"productCode": "9900005466", "quantity": 1}],
                                  coupon_id=cid)
        check("套餐场景 discount 为 0（券不生效，判定正确）",
              res_set.get("discount") == 0, f"discount={res_set.get('discount')}")
    else:
        print("  （该店当前无券，跳过）")

    print("\n[4] 金额单位换算表（写入文档用）")
    print(f"  {'接口':<26} {'字段':<24} {'原始值':<12} 单位")
    print(f"  {'query-meals':<26} {'currentPrice':<24} {'26.5':<12} 元")
    print(f"  {'query-meals':<26} {'originalPrice':<24} {'26.5':<12} 元")
    print(f"  {'query-my-coupons':<26} {'discountInfo.value':<24} {'13.9':<12} 元")
    print(f"  {'order-list':<26} {'realTotalAmount':<24} {'27.5':<12} 元")
    print(f"  {'calculate-price':<26} {'price':<24} {'2650':<12} 分 ← 需 /100")
    print(f"  {'calculate-price':<26} {'discount':<24} {'710':<12} 分 ← 需 /100")

    print("\n" + "=" * 62)
    print(f" 结果：{PASSED} passed, {FAILED} failed")
    print("=" * 62)
    return 0 if FAILED == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
