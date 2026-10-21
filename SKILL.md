---
name: mcd-store-price-scout
description: 麦当劳门店定价洞察与购买决策助手。当用户想购买某个麦当劳产品、询问附近哪家麦当劳更划算、手里的麦当劳优惠券在哪能用、麦当劳积分能换什么、或者想了解不同麦当劳门店的价格差异时使用。基于麦当劳中国 MCP 实时接口，提供可复核的确定性结论。
license: MIT
---

# 麦门门店价差官

## 何时使用

触发场景（命中任一即应使用本技能）：

- 「我想买巨无霸 / 巨无霸套餐，去哪家店划算」
- 「附近哪家麦当劳便宜」「XX 店的麦乐鸡多少钱」
- 「我这张麦当劳券能用吗」「哪几家店能用我这张券」
- 「我的券快过期了还能用吗」
- 「XX 店和 XX 店的麦门价格有差别吗」
- 「我在麦当劳花了多少钱」「我常买什么」
- 「麦当劳积分怎么用最划算」

不适用：帮用户实际下单、领券、抽奖（那些是写操作，本技能只做只读分析）。

## 前置条件

1. 已申请麦当劳 MCP Token，并设置环境变量 `MCD_MCP_TOKEN`
2. MCP Server 已在客户端配置（`https://mcp.mcd.cn`，`streamablehttp`，Bearer 鉴权）
3. Python 3.9+，无第三方依赖

若 Token 缺失，脚本会明确提示申请步骤，不要猜测或伪造数据。

## 工作流程

### 第一步 · 定位门店

```
query-nearby-stores(searchType=2, beType=1, city=..., keyword=...)
```

- `city` 必填，`keyword` 必填（searchType=2 时）
- 关键词会解析到具体地标：传「广州」命中广州南站，传「广州塔」才命中海珠
- `beType=1` 返回的门店无 `beCode`，`beType=5`（得来速）有

### 第二步 · 逐店拉菜单（比价 + 折扣统计）

```
query-meals(storeCode=..., orderType=1, beType=1)   # 逐店调用
```

从返回的 `meals{}` 中提取：
- `currentPrice` / `originalPrice` → 跨店比价、各店折扣覆盖率
- 两者不等即该商品在活动折扣中

### 第三步 · 判定券的适用范围

```
query-store-coupons(storeCode=..., orderType=1, beType=1)   # 逐店调用
```

这是唯一能拿到券 `products` 的接口。然后：

1. 券的 `productCode` 常是**虚拟组合项**，菜单里查不到
   → 用 `query-meal-detail(code=该虚拟码)` 展开 `rounds[].choices[].code` 得到真实编码
2. 用真实编码调 `calculate-price` 验算优惠金额：
   - 券必须传**顶层** `couponId`，不能在 `items` 里
   - 商品字段名必须是 `productCode`（传 `code` 静默返回全 0）
   - 返回金额单位是**分**
3. 找出「套餐陷阱」：对含该商品的套餐实测算价，`discount=0` 即券不适用

### 第四步 · 提取历史画像

```
order-list
```

统计复购次数、份数、常去门店、客单价。
注意区分「复购」（多次买）和「一次多份」（1 次买多件）——靠 `times` 与 `qty` 分开判断。

### 第五步 · 跨片区对比（价值最高的一步）

同片区门店价差很小（约 ¥1），**跨片区才可能有 ¥27 级别差异**。
对比社区店与交通枢纽/景区店的折扣覆盖率，能看出定价体系差异。

```
query-nearby-stores(city=另一城市, keyword=另一地标)
```

## 硬性约束

1. **不要让模型口算价格。** 金额一律来自接口返回，单位是分，需 /100。
2. **不要凭字段推断券是否适用。** `appliedCategory`、`multipleProducts` 不可靠，
   只有实测算价比对 `discount` 才准确。
3. **不要编造门店价格或券状态。** 所有数字必须来自本次 MCP 调用。
4. **只做只读操作。** 不调用 `create-order`、`auto-bind-coupons`、`draw-lottery`、
   `mall-create-order`、`party-order-create`、`delivery-create-address`。
5. **外送场景需要 `beCode`。** 拿不到时降级到到店场景，并说明。
6. **Token 只从环境变量读取**，不要写进任何文件或输出。

## 输出格式

按以下五段结构组织结论：

```
【一】门店活动折扣覆盖率      各店有折扣商品占比 + 最高最低对比
【二】目标产品跨门店比价      各店价格 + 距离 + 极差与极差率
【三】你的券 · 适用范围与门店   虚拟编码展开说明 + 可兑/不可兑门店 + 套餐陷阱
【四】你的历史消费画像        复购统计 + 常去门店 + 客单价
【五】行动建议                可直接执行的几条结论
```

**结论必须可执行**。不要只给「更便宜」，要给出「去哪家店、省多少、券能不能用」。

## 常见误判提醒

- 用户说「套餐里有这个商品，券应该能用」→ **错**。实测券在套餐内 `discount=0`。
- 用户以为全国统一定价 → **错**。实测同款套餐跨店极差 ¥27.1。
- 用户以为有券就一定用得上 → **错**。券绑定门店，20 家里可能只有 9 家能用。
- 看到 `currentPrice` 与 `originalPrice` 不同就说是活动 → 需确认原价本身是否也因店而异。

## 辅助脚本

仓库 `scripts/` 下提供了零依赖的完整实现，可直接调用：

```bash
python scripts/price_scout.py --city 佛山 --keyword 禅城区 \
    --compare 广州/广州塔 --compare 广州/南站
```

离线演示（不联网、不需 Token）：`python scripts/sample_runner.py`
