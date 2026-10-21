"""运行过程模拟：打印每一步的 MCP 调用与数据处理，让流程完全可观测。

用法：python trace_run.py --city 佛山 --keyword 禅城区
"""

import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import mcd_client as mc  # noqa: E402
from price_scout import PriceScout, render_card  # noqa: E402

STEP = [0]
T0 = time.monotonic()


def banner(title: str) -> None:
    STEP[0] += 1
    print()
    print("━" * 64)
    print(f" 步骤 {STEP[0]}  {title}")
    print("━" * 64)


def trace(original):
    """包装 MCP 客户端的 call，打印每次调用的工具与关键参数。"""
    def wrapper(self, tool: str, **arguments):
        STEP[0] += 0
        args = {k: v for k, v in arguments.items() if k in
                ("storeCode", "code", "productCode", "couponId", "city", "keyword", "beType", "orderType")}
        arg_str = "  ".join(f"{k}={v}" for k, v in args.items()) or "-"
        print(f"   → {tool:<22} {arg_str}")
        res = original(self, tool, **arguments)
        if res is None:
            print(f"     ↳ 降级返回 None")
        elif isinstance(res, list):
            print(f"     ↳ 列表 {len(res)} 项")
        elif isinstance(res, dict):
            if "meals" in res:
                print(f"     ↳ 菜单 {len(res['meals'])} 项")
            elif "rounds" in res:
                n = sum(len(r.get("choices") or []) for r in res["rounds"])
                print(f"     ↳ 详情 rounds={len(res['rounds'])} choices={n}")
            elif "price" in res:
                print(f"     ↳ price={res.get('price')} 分  discount={res.get('discount')} 分")
            else:
                print(f"     ↳ 字段 {len(res)} 个")
        return res
    return wrapper


def main() -> int:
    city = sys.argv[1] if len(sys.argv) > 1 else "佛山"
    keyword = sys.argv[2] if len(sys.argv) > 2 else "禅城区"
    compare = [("广州", "广州塔"), ("广州", "南站"), ("佛山", "禅城区")]

    banner("初始化 MCP 客户端")
    client = mc.McdClient(verbose=False)
    print(f"   接入地址 {client.url}")
    print(f"   Token 来源 环境变量 MCD_MCP_TOKEN（长度 {len(client.token)}）")
    print(f"   限流阈值 {mc.RATE_LIMIT_PER_MIN} 次/分钟")

    # 打开调用追踪
    mc.McdClient.call = trace(mc.McdClient.call)

    banner("第 1 步 · 定位门店并拉取菜单")
    print("   调用 query-nearby-stores 找门店 → 对每家店调 query-meals")
    scout = PriceScout(client, verbose=False)
    snaps = scout.build_snapshots(city, keyword, limit=4)
    print(f"   小计：{len(snaps)} 家门店，"
          f"共 {sum(len(s.menu) for s in snaps)} 个商品条目")

    banner("第 2 步 · 拉取各门店可用券")
    for s in snaps:
        s.coupons = mc.get_store_coupons(client, s.store_code)

    banner("第 3 步 · 跨门店比价与折扣覆盖率")
    verdicts = scout.compare_products(snaps, top=5)
    for v in verdicts[:3]:
        print(f"   {v.product_name}: 极差 ¥{v.spread}（{v.spread_pct}%）")
    print("   折扣覆盖率：")
    for s in sorted(snaps, key=lambda x: x.discount_rate, reverse=True):
        print(f"     {s.store_name[:20]:<22} {s.discounted_count:>3}/{s.total_count:<4} "
              f"{s.discount_rate*100:>5.1f}%")

    banner("第 4 步 · 判定券的适用范围（展开虚拟编码 → 实测算价）")
    my_coupons = mc.get_my_coupons(client)
    scopes = []
    for cp in my_coupons:
        sc = scout.analyze_coupon(cp, snaps, check_price=True)
        if sc and sc.virtual_codes:
            scopes.append(sc)
            print(f"   券「{sc.title}」")
            print(f"     虚拟编码 {sc.virtual_codes} → 真实编码 {sc.real_codes}")
            print(f"     可兑 {len(sc.usable_stores)}/{len(snaps)} 店，"
                  f"实际优惠 ¥{sc.real_discount}")

    banner("第 5 步 · 提取历史消费画像")
    hist = scout.build_history()
    print(f"   {hist.total_orders} 单，合计 ¥{hist.total_spend:.2f}，"
          f"客单价 ¥{hist.total_spend/max(hist.total_orders,1):.2f}")
    for name, rec in sorted(hist.items.items(), key=lambda kv: kv[1]["qty"], reverse=True)[:4]:
        tag = "（一次多份）" if rec["times"] == 1 and rec["qty"] > 1 else ""
        print(f"     {name} ×{rec['qty']}（{rec['times']} 次）{tag}")

    banner("第 6 步 · 跨片区定价体系对比")
    reuse = {s.store_code: s for s in snaps}
    cross = scout.compare_across_areas(compare, reuse=reuse)
    for name, stat in sorted(cross["areas"].items(), key=lambda kv: kv[1]["rate"], reverse=True):
        print(f"   {name}: 折扣覆盖 {stat['discounted']}/{stat['total']} = {stat['rate']*100:.1f}%")

    banner("第 7 步 · 组装与渲染决策卡")
    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "query": {"city": city, "keyword": keyword, "product": None},
        "stores": snaps,
        "verdicts": verdicts,
        "coupon_scopes": scopes,
        "history": hist,
        "cross_area": cross,
    }
    print(render_card(result))

    banner("接口用量汇总")
    print(f"   总调用 {client.call_count} 次")
    for tool, n in sorted(client.tool_counts.items(), key=lambda kv: -kv[1]):
        print(f"     {tool:<24} {n:>3} 次")
    print(f"\n   耗时 {time.monotonic() - T0:.1f} 秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())
