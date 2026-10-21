"""麦门门店价差官 · 核心决策引擎

回答一个问题：**我想买某个产品，现在该怎么做？**

四个维度，全部来自麦当劳 MCP 的真实调用，没有一处是模型估算：

  ① 门店价格    query-meals  × N 家门店  → 同品跨店价差
  ② 活动折扣    originalPrice vs currentPrice → 各店折扣覆盖率
  ③ 券可用性    query-store-coupons × N 家门店 + calculate-price 验算
  ④ 历史偏好    order-list → 复购画像

技术要点（均为实测得出，详见 references/mcp-tools.md）：
- 券声明的 productCode 常常是「虚拟组合项」，菜单里查不到，
  必须先经 query-meal-detail 展开成真实编码才能算价。
- 券在套餐内不生效（discount=0），只能靠实测算价判定，不能靠字段推断。
- calculate-price 的商品字段名是 productCode，传 code 会静默返回全 0。
- 返回金额单位是「分」。

作者：麦门门店价差官 mcd-store-price-scout
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcd_client import (  # noqa: E402
    McdClient,
    McdError,
    calculate_price,
    get_meal_detail,
    get_my_coupons,
    get_nearby_stores,
    get_order_history,
    get_store_coupons,
)

# ---------------------------------------------------------------- 数据结构


@dataclass
class StoreSnapshot:
    """一家门店的价格与活动快照。"""

    store_code: str
    store_name: str
    distance_m: int | None = None
    address: str = ""
    business_hours: str = ""
    menu: dict[str, dict[str, Any]] = field(default_factory=dict)
    coupons: list[dict[str, Any]] = field(default_factory=list)

    # 折扣统计
    discounted_count: int = 0
    total_count: int = 0
    discount_total: float = 0.0

    def price_of(self, product_code: str) -> float | None:
        item = self.menu.get(product_code)
        if not item:
            return None
        try:
            return float(item.get("currentPrice"))
        except (TypeError, ValueError):
            return None

    def original_price_of(self, product_code: str) -> float | None:
        item = self.menu.get(product_code)
        if not item:
            return None
        try:
            return float(item.get("originalPrice"))
        except (TypeError, ValueError):
            return None

    def is_discounted(self, product_code: str) -> bool:
        cur = self.price_of(product_code)
        orig = self.original_price_of(product_code)
        return cur is not None and orig is not None and orig != cur

    @property
    def discount_rate(self) -> float:
        """折扣覆盖率：有折扣的商品占比。"""
        if not self.total_count:
            return 0.0
        return self.discounted_count / self.total_count


@dataclass
class ProductVerdict:
    """一个产品的跨门店比价结论。"""

    product_code: str
    product_name: str
    prices: list[tuple[str, str, float, int | None]] = field(default_factory=list)
    # (store_code, store_name, price, distance)

    @property
    def values(self) -> list[float]:
        return [p[2] for p in self.prices]

    @property
    def cheapest(self) -> tuple[str, str, float, int | None] | None:
        return min(self.prices, key=lambda x: x[2]) if self.prices else None

    @property
    def priciest(self) -> tuple[str, str, float, int | None] | None:
        return max(self.prices, key=lambda x: x[2]) if self.prices else None

    @property
    def spread(self) -> float:
        """最高价与最低价的差。"""
        if len(self.prices) < 2:
            return 0.0
        return round(max(self.values) - min(self.values), 2)

    @property
    def spread_pct(self) -> float:
        """极差占最低价的百分比。"""
        if not self.prices:
            return 0.0
        lo = min(self.values)
        return round((max(self.values) - lo) / lo * 100, 1) if lo else 0.0


@dataclass
class CouponScope:
    """一张券的适用范围判定结果。"""

    coupon_id: str
    title: str
    face_value: float
    real_discount: float
    expire_at: str
    days_left: int
    virtual_codes: list[str] = field(default_factory=list)
    real_codes: list[str] = field(default_factory=list)
    real_names: list[str] = field(default_factory=list)
    real_defaults: list[int] = field(default_factory=list)  # 1=该规格是门店默认
    usable_stores: list[str] = field(default_factory=list)
    unusable_stores: list[str] = field(default_factory=list)
    # 包含该商品但券不生效的套餐（真实陷阱）
    trap_set_names: list[str] = field(default_factory=list)


@dataclass
class HistoryProfile:
    """用户历史消费画像。"""

    items: dict[str, dict[str, Any]] = field(default_factory=dict)
    stores: dict[str, int] = field(default_factory=dict)
    total_orders: int = 0
    total_spend: float = 0.0

    def top_stores(self, n: int = 3) -> list[tuple[str, int]]:
        return sorted(self.stores.items(), key=lambda kv: kv[1], reverse=True)[:n]


# ---------------------------------------------------------------- 核心引擎


class PriceScout:
    """麦门门店价差官。"""

    def __init__(self, client: McdClient, verbose: bool = True):
        self.c = client
        self.verbose = verbose

    def log(self, msg: str) -> None:
        if self.verbose:
            print(f"  {msg}")

    # ---------------------------------------------------------- ① 门店快照

    def build_snapshots(self, city: str, keyword: str, limit: int = 12,
                        be_type: int = 1) -> list[StoreSnapshot]:
        """拉取附近门店并构建价格快照。"""
        raw = get_nearby_stores(self.c, city, keyword, be_type=be_type)
        if not raw:
            raise McdError(f"未找到门店：{city} / {keyword}")

        snaps: list[StoreSnapshot] = []
        for st in raw[:limit]:
            code = str(st.get("storeCode", ""))
            if not code:
                continue
            snap = StoreSnapshot(
                store_code=code,
                store_name=st.get("storeName", code),
                distance_m=st.get("distance"),
                address=st.get("address", ""),
                business_hours=(
                    f"{st.get('businessStartTime', '?')}-{st.get('businessEndTime', '?')}"
                    if st.get("businessStartTime")
                    else ""
                ),
            )
            snap.menu = get_menu_safe(self.c, code)
            snap.total_count = len(snap.menu)
            for item in snap.menu.values():
                try:
                    cur = float(item.get("currentPrice"))
                    orig = float(item.get("originalPrice"))
                except (TypeError, ValueError):
                    continue
                if orig != cur:
                    snap.discounted_count += 1
                    snap.discount_total += round(orig - cur, 2)
            snaps.append(snap)
            self.log(f"{snap.store_name} ({code}) 菜单 {snap.total_count} 项，"
                     f"折扣 {snap.discounted_count} 项")
        return snaps

    # ---------------------------------------------------------- ② 跨店比价

    def compare_products(self, snaps: list[StoreSnapshot],
                         name_filter: str | None = None,
                         top: int = 8) -> list[ProductVerdict]:
        """跨门店比价。name_filter 为空则返回极差最大的若干商品。"""
        agg: dict[str, dict[str, Any]] = {}
        for snap in snaps:
            for code, item in snap.menu.items():
                try:
                    price = float(item.get("currentPrice"))
                except (TypeError, ValueError):
                    continue
                rec = agg.setdefault(code, {"name": item.get("name", code), "points": []})
                rec["points"].append((snap.store_code, snap.store_name, price, snap.distance_m))

        verdicts: list[ProductVerdict] = []
        for code, rec in agg.items():
            if name_filter and name_filter not in rec["name"]:
                continue
            if len(rec["points"]) < 2:
                continue
            v = ProductVerdict(product_code=code, product_name=rec["name"])
            v.prices = sorted(rec["points"], key=lambda x: x[2])
            verdicts.append(v)

        verdicts.sort(key=lambda v: v.spread, reverse=True)
        return verdicts[:top] if not name_filter else verdicts

    # ---------------------------------------------------------- ③ 券范围判定

    def analyze_coupon(self, coupon: dict[str, Any], snaps: list[StoreSnapshot],
                       check_price: bool = True) -> CouponScope | None:
        """判定一张券的适用范围：展开虚拟编码、实测算价验算、匹配门店。

        这里刻意不依赖 appliedCategory / multipleProducts 等字段做推断——
        实测证明那些字段不可靠，只有实测算价比对 discount 才准确。
        """
        cid = coupon.get("id")
        title = coupon.get("title", "")
        if not cid:
            return None

        scope = CouponScope(
            coupon_id=cid,
            title=title,
            face_value=_to_float(coupon.get("discountInfo", {}).get("discountValue"), 0.0),
            real_discount=0.0,
            expire_at=coupon.get("tradeEndDate", ""),
            days_left=_days_left(coupon.get("tradeEndDate")),
        )

        # 第一步：找到券声明的虚拟编码（需要门店券接口才有 products）
        virtual_codes: list[str] = []
        for snap in snaps:
            for cp in snap.coupons:
                if cp.get("couponId") == cid:
                    for p in cp.get("products", []) or []:
                        pc = p.get("productCode")
                        if pc and pc not in virtual_codes:
                            virtual_codes.append(pc)
        if not virtual_codes:
            return None
        scope.virtual_codes = virtual_codes

        # 第二步：展开虚拟编码 → 真实商品编码
        # 收集时记录 isDefault，默认项排前面（试算优先用默认项，它是门店主推规格）
        # 探针门店优先选「券可用」的那家，避免在无此商品的店里展开失败
        probe_store = ""
        for cand in snaps:
            if any(cp.get("couponId") == cid for cp in cand.coupons):
                probe_store = cand.store_code
                break
        if not probe_store and snaps:
            probe_store = snaps[0].store_code
        for vcode in virtual_codes:
            detail = get_meal_detail(self.c, probe_store, vcode)
            for r in detail.get("rounds", []) or []:
                for ch in r.get("choices", []) or []:
                    cc = ch.get("code")
                    if not cc or cc in scope.real_codes:
                        continue
                    is_default = 1 if ch.get("isDefault") == 1 else 0
                    scope.real_codes.append(cc)
                    scope.real_names.append(ch.get("name", cc))
                    scope.real_defaults.append(is_default)
            if not detail.get("rounds"):
                # 本身就是真实编码
                if vcode not in scope.real_codes:
                    scope.real_codes.append(vcode)
                    scope.real_names.append(title)
                    scope.real_defaults.append(1)

        if not scope.real_codes:
            return None

        # 第三步：逐店判定该券能否使用
        for snap in snaps:
            usable = any(cp.get("couponId") == cid for cp in snap.coupons)
            if usable:
                scope.usable_stores.append(snap.store_code)
            else:
                scope.unusable_stores.append(snap.store_code)

        # 第四步：实测算价验算真实优惠金额
        #
        # ★ 关键：必须挑「该店菜单里真实存在」且「isDefault=1」的编码来试算。
        # 实测教训：券展开后会得到多个规格编码（如 515946 冰 / 515945 热），
        # 但并非每家店都同时供应全部规格——只卖冰饮的门店没有 515945，
        # 用它算价会报 60006「商品已售罄或不可售」，
        # 进而让优惠金额被误判为 0。
        if check_price and scope.real_codes:
            # ★ 探针门店的选择很关键：
            #   必须是「这张券实际可用」的门店。若随便取 snaps[0]（往往只是最近的那家），
            #   而那家恰好不供应券指定的商品，就会算不出优惠（discount 为 0），
            #   把「券能用」误判成「券没有优惠」。
            #   实测教训：最近的高科大厦店菜单里根本没有黑巧，券自然用不了。
            probe = None
            for cand in snaps:
                if cand.store_code in scope.usable_stores:
                    probe = cand
                    break
            if probe is None:
                probe = snaps[0]
            # 排序规则：① 门店菜单里有售 ② 门店默认规格
            order = sorted(
                range(len(scope.real_codes)),
                key=lambda i: (
                    0 if scope.real_codes[i] in probe.menu else 1,
                    0 if (i < len(scope.real_defaults) and scope.real_defaults[i]) else 1,
                ),
            )
            for idx in order[:1]:
                rc = scope.real_codes[idx]
                price_data = calculate_price(
                    self.c, probe.store_code,
                    items=[{"productCode": rc, "quantity": 1}],
                    coupon_id=cid,
                )
                if price_data:
                    disc = _to_float(price_data.get("discount"), 0.0)
                    scope.real_discount = round(disc / 100, 2)
                    if scope.real_discount == 0:
                        # 券可能不适用于该商品，改用原价对比
                        orig = _to_float(price_data.get("originalPrice"), 0.0)
                        now = _to_float(price_data.get("price"), 0.0)
                        if orig and now:
                            scope.real_discount = round((orig - now) / 100, 2)

        # 第五步：找出「套餐陷阱」——含该商品但券不生效的套餐
        scope.trap_set_names = self._find_set_traps(scope, snaps)
        return scope

    def _find_set_traps(self, scope: CouponScope, snaps: list[StoreSnapshot]) -> list[str]:
        """找出包含券指定商品、但券并不生效的套餐。

        这是最容易让用户误判的地方：套餐里明明有黑巧，但券只认单点。
        """
        traps: list[str] = []
        if not scope.real_codes or not snaps:
            return traps
        probe = snaps[0]
        targets = set(scope.real_codes)

        # 第一步：定位目标商品本身（券指定的虚拟项展开后有几个真实编码）
        target_price = 0.0
        for rc in scope.real_codes:
            p = probe.price_of(rc)
            if p is not None:
                target_price = max(target_price, p)

        # 第二步：只保留「可能是含饮品的套餐」候选
        # 实测：29 个套餐全部价格都 ≥ 目标商品价，按价格筛不出效果。
        # 真正的过滤发生在第三步——rounds 里"选择套餐内饮料"组的 choices 才含目标编码。
        candidates: list[tuple[str, str]] = []
        for code, item in probe.menu.items():
            name = item.get("name", "")
            if not any(k in name for k in ("套餐", "三件套", "四宫格", "单人餐", "全家桶")):
                continue
            try:
                price = float(item.get("currentPrice"))
            except (TypeError, ValueError):
                continue
            if target_price > 0 and price < target_price:
                continue
            candidates.append((code, name))

        # 第三步：对候选套餐查详情，确认是否真的含目标商品
        # 实测：29 个套餐中 19 个真的把该饮品列为可选，无法预先筛掉，
        # 因此 query-meal-detail 的调用量是必要的。
        # 但 calculate-price（验算券是否生效）可以限量——
        # 最多验算 MAX_TRAP_CHECKS 个，足够给用户提示且大幅降低接口用量。
        MAX_TRAP_CHECKS = 6
        checked = 0
        for code, name in candidates:
            if checked >= MAX_TRAP_CHECKS:
                break
            detail = get_meal_detail(self.c, probe.store_code, code)
            contains = False
            for r in detail.get("rounds", []) or []:
                for ch in r.get("choices", []) or []:
                    if ch.get("code") in targets:
                        contains = True
                        break
                if contains:
                    break
            if not contains:
                continue
            # 含该商品，再用实测算价确认券不生效
            checked += 1
            price_data = calculate_price(
                self.c, probe.store_code,
                items=[{"productCode": code, "quantity": 1}],
                coupon_id=scope.coupon_id,
            )
            disc = _to_float((price_data or {}).get("discount"), 0.0)
            if disc == 0:
                traps.append(name)
        return traps[:6]

    # ---------------------------------------------------------- ④ 历史画像

    def build_history(self) -> HistoryProfile:
        """从历史订单提取复购偏好。"""
        orders = get_order_history(self.c)
        prof = HistoryProfile(total_orders=len(orders))
        for o in orders:
            try:
                prof.total_spend += float(o.get("realTotalAmount", 0) or 0)
            except (TypeError, ValueError):
                pass
            store = o.get("storeName", "")
            if store:
                prof.stores[store] = prof.stores.get(store, 0) + 1
            for p in o.get("orderProductList", []) or []:
                name = p.get("productName", "")
                if not name:
                    continue
                rec = prof.items.setdefault(name, {"times": 0, "qty": 0, "stores": set()})
                rec["times"] += 1
                rec["qty"] += int(p.get("quantity", 1) or 1)
                if store:
                    rec["stores"].add(store)
        return prof

    # ---------------------------------------------------------- 决策输出

    def advise(self, city: str, keyword: str, product_keyword: str | None = None,
               store_limit: int = 12, check_price: bool = True,
               compare_cities: list[tuple[str, str]] | None = None) -> dict[str, Any]:
        """主流程：产出完整决策卡。

        compare_cities: 额外的对照城市/区域，形如 [("广州", "广州塔"), ("广州", "南站")]。
            用于跨片区定价对比——实测同片区价差仅 ¥1，跨片区才有 ¥18 级别差异。
        """
        snaps = self.build_snapshots(city, keyword, limit=store_limit)
        if not snaps:
            raise McdError("没有可用门店")

        # 拉取各店门店券（券的适用范围只在门店券接口里）
        for snap in snaps:
            snap.coupons = get_store_coupons(self.c, snap.store_code)

        verdicts = self.compare_products(snaps, name_filter=product_keyword)

        # 用户卡包里的券
        my_coupons = get_my_coupons(self.c)
        scopes: list[CouponScope] = []
        for cp in my_coupons:
            sc = self.analyze_coupon(cp, snaps, check_price=check_price)
            if sc and sc.virtual_codes:
                scopes.append(sc)

        history = self.build_history()

        # 跨片区对照：复用主查询已拉取的门店，避免重复调用接口
        reuse = {s.store_code: s for s in snaps}
        cross = self.compare_across_areas(compare_cities or [], product_keyword, reuse=reuse)

        return {
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "query": {"city": city, "keyword": keyword, "product": product_keyword},
            "stores": snaps,
            "verdicts": verdicts,
            "coupon_scopes": scopes,
            "history": history,
            "cross_area": cross,
        }

    def compare_across_areas(self, areas: list[tuple[str, str]],
                             product_keyword: str | None = None,
                             max_per_area: int = 3,
                             reuse: dict[str, StoreSnapshot] | None = None) -> dict[str, Any]:
        """跨片区/跨城对比：不同定价体系的门店放在同一坐标系里比。

        实测发现：同片区门店价差约 ¥1，跨片区（社区店 vs 交通枢纽）可达 ¥18。
        这个维度是本工具真正的价值所在，因此单独提供。

        reuse: 主查询已拉取的门店快照（store_code -> snapshot）。
            复用可避免同一片区被重复拉取，减少接口调用并保证两处数据一致。
        """
        if not areas:
            return {}

        reuse = reuse or {}
        area_snaps: dict[str, list[StoreSnapshot]] = {}
        for city, keyword in areas:
            got: list[StoreSnapshot] = []
            # 先只取门店列表（不拉菜单），已拉过的门店直接复用快照
            try:
                raw_stores = get_nearby_stores(self.c, city, keyword, be_type=1)
            except McdError:
                raw_stores = []
            for st in raw_stores[:max_per_area]:
                code = str(st.get("storeCode", ""))
                if not code:
                    continue
                if code in reuse:
                    got.append(reuse[code])
                    continue
                snap = StoreSnapshot(
                    store_code=code,
                    store_name=st.get("storeName", code),
                    distance_m=st.get("distance"),
                    address=st.get("address", ""),
                    business_hours=(
                        f"{st.get('businessStartTime', '?')}-{st.get('businessEndTime', '?')}"
                        if st.get("businessStartTime") else ""
                    ),
                )
                snap.menu = get_menu_safe(self.c, code)
                snap.total_count = len(snap.menu)
                for item in snap.menu.values():
                    try:
                        cur = float(item.get("currentPrice"))
                        orig = float(item.get("originalPrice"))
                    except (TypeError, ValueError):
                        continue
                    if orig != cur:
                        snap.discounted_count += 1
                        snap.discount_total += round(orig - cur, 2)
                reuse[code] = snap
                got.append(snap)
            if got:
                area_snaps[f"{city}·{keyword}"] = got

        if not area_snaps:
            return {}

        # 汇总每个商品在各片区的价格（同一 storeCode 只计一次）
        seen: set[str] = set()
        all_stores: list[StoreSnapshot] = []
        for group in area_snaps.values():
            for s in group:
                if s.store_code not in seen:
                    seen.add(s.store_code)
                    all_stores.append(s)
        verdicts = self.compare_products(all_stores, name_filter=product_keyword, top=10)

        # 各片区自身的折扣覆盖率
        area_stats = {}
        for name, group in area_snaps.items():
            total_d = sum(s.discounted_count for s in group)
            total_n = sum(s.total_count for s in group)
            area_stats[name] = {
                "discounted": total_d,
                "total": total_n,
                "rate": round(total_d / total_n, 4) if total_n else 0.0,
                "stores": [{"store_code": s.store_code, "store_name": s.store_name,
                            "distance_m": s.distance_m, "discounted": s.discounted_count,
                            "total": s.total_count} for s in group],
            }

        return {"areas": area_stats, "verdicts": verdicts}


# ---------------------------------------------------------------- 工具函数


def get_menu_safe(client: McdClient, store_code: str) -> dict[str, dict[str, Any]]:
    """拉菜单，失败返回空 dict 而不是中断整体流程。"""
    from mcd_client import get_menu
    try:
        return get_menu(client, store_code)
    except McdError:
        return {}


def _to_float(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _days_left(expire_date: str | None) -> int:
    """距失效日还有几天（含今天）。"""
    if not expire_date:
        return -1
    try:
        end = datetime.strptime(expire_date, "%Y-%m-%d").replace(
            hour=23, minute=59, second=59)
        now = datetime.now()
        return (end - now).days + 1
    except ValueError:
        return -1


# ---------------------------------------------------------------- 渲染


def render_card(result: dict[str, Any]) -> str:
    """把决策结果渲染成给人看的文本卡。"""
    lines: list[str] = []
    A = lines.append

    q = result["query"]
    A("=" * 62)
    A(f" 麦门门店价差官 · 购买决策卡")
    A(f" {q['city']} / {q['keyword']}"
      + (f" / 目标产品：{q['product']}" if q.get("product") else ""))
    A(f" 生成时间：{result['generated_at']}")
    A("=" * 62)

    stores = result["stores"]

    # ---- 券的可用性（放第一位：零距离成本，最能直接用）
    A("")
    A("【一】你的券 · 能不能用、在哪能用")
    A("  " + "-" * 56)
    scopes = result["coupon_scopes"]
    if not scopes:
        A("  （卡包中暂无券，或券无商品适用范围信息）")
    for sc in scopes:
        A(f"  ▸ {sc.title}")
        A(f"      面值 ¥{sc.face_value}　实际优惠 ¥{sc.real_discount}"
          + (f"　剩余 {sc.days_left} 天（{sc.expire_at} 失效）" if sc.days_left >= 0 else ""))
        if sc.real_names:
            A(f"      适用商品：{'、'.join(sc.real_names[:3])}")
            if sc.virtual_codes and sc.virtual_codes[0] not in sc.real_codes:
                A(f"      （券声明编码 {sc.virtual_codes[0]} 为虚拟项，已展开为 "
                  f"{', '.join(sc.real_codes[:2])}）")
        if sc.usable_stores:
            names = [s.store_name for s in stores if s.store_code in sc.usable_stores]
            A(f"      可兑门店 {len(sc.usable_stores)}/{len(stores)}："
              + "、".join(names[:4]))
        if sc.unusable_stores:
            names = [s.store_name for s in stores if s.store_code in sc.unusable_stores]
            A(f"      不可兑 {len(sc.unusable_stores)} 家：" + "、".join(names[:4]))
        if sc.trap_set_names:
            A(f"      ⚠ 陷阱：这些套餐含该商品但券不生效 → "
              + "、".join(sc.trap_set_names[:3]))
        A("")

    # ---- 折扣覆盖率：结构性差异
    A("【二】门店活动折扣覆盖率")
    A("  " + "-" * 56)
    ranked = sorted(stores, key=lambda s: s.discount_rate, reverse=True)
    for s in ranked:
        bar = "#" * int(s.discount_rate * 20)
        A(f"  {s.store_name[:16]:<18} {s.discounted_count:>3}/{s.total_count:<4} "
          f"{s.discount_rate*100:>5.1f}%  {bar}")
    if ranked:
        best = ranked[0]
        worst = ranked[-1]
        A(f"  → 最高 {best.discount_rate*100:.0f}%（{best.store_name}）"
          f" vs 最低 {worst.discount_rate*100:.0f}%（{worst.store_name}）")
    A("")

    # ---- 目标产品比价
    A("【三】目标产品跨门店比价")
    A("  " + "-" * 56)
    verdicts = result["verdicts"]
    if not verdicts:
        A("  （未找到可比对的商品）")
    for v in verdicts[:6]:
        A(f"  ▸ {v.product_name}  极差 ¥{v.spread}（{v.spread_pct}%）")
        for code, name, price, dist in v.prices:
            d = f"{dist}m" if dist is not None else "-"
            A(f"      {name[:20]:<22} ¥{price:<6} ({d})")
        if v.cheapest and v.priciest and v.spread > 0:
            A(f"      最低 {v.cheapest[1]} ¥{v.cheapest[2]}　"
              f"最高 {v.priciest[1]} ¥{v.priciest[2]}")
    A("")

    # ---- 历史画像
    A("【四】你的历史消费画像")
    A("  " + "-" * 56)
    hist = result["history"]
    if hist.total_orders == 0:
        A("  （无历史订单记录）")
    else:
        A(f"  累计 {hist.total_orders} 单，合计 ¥{hist.total_spend:.2f}，"
          f"客单价 ¥{hist.total_spend/max(hist.total_orders,1):.2f}")
        for name, cnt in hist.top_stores(3):
            A(f"    常去：{name}（{cnt} 次）")
        ranked_items = sorted(hist.items.items(), key=lambda kv: kv[1]["qty"], reverse=True)
        for name, rec in ranked_items[:5]:
            tag = "（一次多份）" if rec["times"] == 1 and rec["qty"] > 1 else ""
            A(f"    买过：{name} ×{rec['qty']}（{rec['times']} 次）{tag}")
    A("")

    # ---- 跨片区定价体系对比
    cross = result.get("cross_area")
    if cross and cross.get("areas"):
        A("")
        A("【五】跨片区定价体系对比")
        A("  " + "-" * 56)
        for name, stat in sorted(cross["areas"].items(),
                                 key=lambda kv: kv[1]["rate"], reverse=True):
            A(f"  {name[:24]:<26} 折扣覆盖 {stat['discounted']}/{stat['total']} "
              f"= {stat['rate']*100:>5.1f}%")
        cv = cross.get("verdicts", [])
        if cv:
            A("  价差最大的商品：")
            for v in cv[:4]:
                A(f"    ▸ {v.product_name}  极差 ¥{v.spread}（{v.spread_pct}%）")
                for code, sname, price, dist in v.prices:
                    d = f"{dist}m" if dist is not None else "-"
                    A(f"        {sname[:26]:<28} ¥{price:<6} ({d})")

    # ---- 行动建议
    A("")
    A("【六】行动建议")
    A("  " + "-" * 56)
    for i, tip in enumerate(_build_tips(result), 1):
        A(f"  {i}. {tip}")
    A("")
    A("=" * 62)
    A(" 本工具数据全部来自麦当劳 MCP 实时接口；金额为接口返回值换算。")
    A("=" * 62)
    return "\n".join(lines)


def _build_tips(result: dict[str, Any]) -> list[str]:
    """根据四维数据生成可执行建议。

    排序原则：零距离成本的建议优先（券的使用），有距离成本的次之（换店）。
    """
    tips: list[str] = []
    coupon_tips: list[str] = []
    store_tips: list[str] = []
    price_tips: list[str] = []
    stores = result["stores"]
    verdicts = result["verdicts"]
    scopes = result["coupon_scopes"]
    hist = result["history"]

    # ---- 第一优先：券（零距离成本，立刻能用）
    for sc in scopes:
        if sc.unusable_stores:
            unusable_names = [s.store_name for s in stores if s.store_code in sc.unusable_stores]
            coupon_tips.append(
                f"你的「{sc.title}」在 {len(sc.unusable_stores)}/{len(stores)} 家门店不可用"
                f"（含 {'、'.join(unusable_names[:2])}），"
                f"可用门店：{'、'.join([s.store_name for s in stores if s.store_code in sc.usable_stores][:2])}。")
        if sc.trap_set_names:
            coupon_tips.append(
                f"注意：{sc.title} 只认「单独点」，套餐 "
                f"{'、'.join(sc.trap_set_names[:2])} 虽然含该商品，但用券不生效（discount=0）。")
        if sc.days_left >= 0 and sc.days_left <= 7 and sc.real_discount > 0:
            coupon_tips.append(
                f"「{sc.title}」还剩 {sc.days_left} 天失效，"
                f"用掉可省 ¥{sc.real_discount}，过期就白扔了。")

    # ---- 第二优先：门店定价异常（有认知价值，无距离成本）
    if len(stores) >= 2:
        ranked = sorted(stores, key=lambda s: s.discount_rate, reverse=True)
        best, worst = ranked[0], ranked[-1]
        if best.discount_rate - worst.discount_rate > 0.25:
            store_tips.append(
                f"{worst.store_name} 仅 {worst.discount_rate*100:.0f}% 商品有折扣，"
                f"而 {best.store_name} 有 {best.discount_rate*100:.0f}%——"
                f"同款在这里买明显更贵。")

    # ---- 第三优先：目标产品价差（需用户权衡距离）
    for v in verdicts[:2]:
        if v.spread >= 2 and v.cheapest and v.priciest:
            price_tips.append(
                f"{v.product_name}：{v.cheapest[1]} ¥{v.cheapest[2]} vs "
                f"{v.priciest[1]} ¥{v.priciest[2]}，差 ¥{v.spread}"
                f"（{v.spread_pct}%）。若在 {v.priciest[1]} 消费，"
                f"换到 {v.cheapest[1]} 可省 ¥{v.spread}。")

    # 历史对比
    if hist.stores and verdicts:
        top_store = hist.top_stores(1)[0][0]
        for v in verdicts[:1]:
            for code, name, price, dist in v.prices:
                if name == top_store:
                    others = [p for p in v.prices if p[1] != top_store]
                    if others:
                        cheapest = min(others, key=lambda x: x[2])
                        if cheapest[2] < price:
                            price_tips.append(
                                f"你常去 {top_store}（{v.product_name} ¥{price}），"
                                f"但 {cheapest[1]} 只要 ¥{cheapest[2]}，"
                                f"每次多付 ¥{round(price - cheapest[2], 2)}。")
                    break
    # 跨片区定价体系
    cross = result.get("cross_area")
    if cross and cross.get("areas") and cross.get("verdicts"):
        areas = cross["areas"]
        ranked_areas = sorted(areas.items(), key=lambda kv: kv[1]["rate"], reverse=True)
        if len(ranked_areas) >= 2:
            hi_name, hi = ranked_areas[0]
            lo_name, lo = ranked_areas[-1]
            if hi["rate"] - lo["rate"] > 0.15:
                store_tips.append(
                    f"定价体系差异：{hi_name} 折扣覆盖 {hi['rate']*100:.0f}%，"
                    f"{lo_name} 仅 {lo['rate']*100:.0f}%——"
                    f"后者是交通枢纽/景区型定价，同款普遍更贵。")
        for v in cross["verdicts"][:1]:
            if v.spread >= 5 and v.cheapest and v.priciest:
                price_tips.append(
                    f"跨片区最大价差：{v.product_name} 在 {v.priciest[1]} 要 ¥{v.priciest[2]}，"
                    f"在 {v.cheapest[1]} 只要 ¥{v.cheapest[2]}，差 ¥{v.spread}"
                    f"（{v.spread_pct}%）。")

    # 合并输出：券 → 门店认知 → 价差（零距离成本优先）
    tips = coupon_tips + store_tips + price_tips
    if not tips:
        tips.append("当前各店价格与折扣基本一致，直接按你的习惯点即可。")
    return tips


# ---------------------------------------------------------------- CLI


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser(
        description="麦门门店价差官 · 基于麦当劳 MCP 的购买决策工具")
    ap.add_argument("--city", required=True, help="城市，如 佛山")
    ap.add_argument("--keyword", required=True, help="位置关键词，如 九江 / 天河区")
    ap.add_argument("--product", help="目标产品名关键词，留空则展示价差最大的商品")
    ap.add_argument("--stores", type=int, default=12, help="参与比价的门店数")
    ap.add_argument("--no-price-check", action="store_true",
                    help="跳过算价验算（更快，但无法给出真实优惠金额）")
    ap.add_argument("--json", help="把结果另存为 JSON")
    ap.add_argument("--sample", action="store_true",
                    help="使用离线样例数据，不访问网络（用于演示与自测）")
    ap.add_argument("--verbose-stats", action="store_true",
                    help="运行结束后打印各 MCP 工具的调用次数")
    ap.add_argument("--compare", action="append", default=[],
                    help="跨片区对照，格式 城市/关键词，可重复。如 --compare 广州/广州塔 --compare 佛山/禅城区")
    args = ap.parse_args()

    if args.sample:
        from sample_runner import run_sample
        print(run_sample(args.product))
        return 0

    try:
        client = McdClient(verbose=True)
        scout = PriceScout(client)
        compare_areas = []
        for item in args.compare:
            if "/" in item:
                c, k = item.split("/", 1)
                compare_areas.append((c.strip(), k.strip()))
        print("\n正在拉取门店与菜单 ...")
        result = scout.advise(
            city=args.city, keyword=args.keyword,
            product_keyword=args.product,
            store_limit=args.stores,
            check_price=not args.no_price_check,
            compare_cities=compare_areas,
        )
    except McdError as exc:
        print(f"\n[错误] {exc}", file=sys.stderr)
        return 1

    # 接口用量统计（便于评估运行成本与限流余量）
    if args.verbose_stats:
        print(f"\n接口调用统计：共 {client.call_count} 次")
        for tool, n in sorted(client.tool_counts.items(), key=lambda kv: -kv[1]):
            print(f"   {tool:<26} {n} 次")

    card = render_card(result)
    print("\n" + card)

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(_jsonable(result), fh, ensure_ascii=False, indent=2)
        print(f"\n已保存 JSON：{args.json}")
    return 0


def _jsonable(obj: Any) -> Any:
    """把 dataclass 树转成可 JSON 序列化的结构。"""
    from dataclasses import asdict, is_dataclass
    if is_dataclass(obj):
        return {k: _jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, set):
        return sorted(obj)
    return obj


if __name__ == "__main__":
    sys.exit(main())
