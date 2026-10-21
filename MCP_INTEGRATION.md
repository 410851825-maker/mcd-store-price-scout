# MCP 集成说明

本文档说明「麦门门店价差官」实际使用了麦当劳 MCP Server 的哪些能力、
以什么顺序调用、以及每个环节踩过的坑。

所有内容基于对 `https://mcp.mcd.cn` 的实际调用验证（2026-10-21，服务端版本 1.0.9，
共 35 个工具）。

---

## 目录

| 官方要求的四要素 | 对应章节 |
|---|---|
| **MCP Server** | [一、MCP Server 与接入信息](#一mcp-server-与接入信息) |
| **Tool** | [二、Tool 清单 · 实际使用的工具（9 个）](#二tool-清单--实际使用的工具9-个) ／ [三、Tool 清单 · 已验证未使用的工具](#三tool-清单--已验证未使用的工具) |
| **调用流程** | [四、调用流程](#四调用流程) ／ [五、真实调用追踪](#五真实调用追踪) |
| **业务价值** | [六、业务价值](#六业务价值) |

---

## 一、MCP Server 与接入信息

| 项 | 值 |
|---|---|
| Server 地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权方式 | 请求头 `Authorization: Bearer <MCP Token>` |
| Token 获取 | <https://open.mcd.cn/mcp> → 登录 → 控制台 → 激活 |
| 服务端版本 | 1.0.9（2026-09-10），共 35 个工具 |
| 限流 | 每 Token 每分钟 600 次请求，超限返回 429 |
| 开放范围 | 中国大陆地区（不含港澳台） |
| 本项目使用 | **9 个只读工具**，不含任何写操作 |
| 配置示例 | [`mcp-config.example.json`](mcp-config.example.json)（仅含 `${MCD_MCP_TOKEN}` 占位符） |

---
```
                       ┌─────────────────────────┐
   用户提问 ──────────▶│  price_scout.advise()   │
                       └────────────┬────────────┘
                                    │
    ┌───────────────┬───────────────┼───────────────┬──────────────┐
    ▼               ▼               ▼               ▼              ▼
① 找门店        ② 拉菜单        ③ 判券          ④ 判历史       ⑤ 跨片区
nearby-stores   query-meals     store-coupons    order-list     nearby-stores
                × N 店          × N 店            my-coupons     × M 片区
    │               │               │               │              │
    │               │               ├─ meal-detail │              │
    │               │               │  (展开虚拟码) │              │
    │               │               └─ calculate-  │              │
    │               │                  price        │              │
    └───────────────┴───────────────┴───────────────┴──────────────┘
                                    │
                                    ▼
                          购买决策卡（确定性结论）
```

**调用是并行的可能性很低**：②③ 都依赖 ① 的 `storeCode` 输出，③ 内部还有二次依赖链。

---

## 二、Tool 清单 · 实际使用的工具（9 个）

### 2.1 `query-nearby-stores` — 找门店

| 项 | 值 |
|---|---|
| 作用 | 按城市+位置关键词搜索可点餐门店 |
| 必填参数 | `beType`（1=到店自取 / 5=得来速）、`searchType`（2=按位置搜） |
| 条件参数 | `city`、`keyword`（searchType=2 时必填） |
| 返回 | `storeCode`、`storeName`、`address`、`distance`、`businessStatus`、`businessStartTime/EndTime`、`reservation` |

**实测要点**：

- `beType=1`（到店自取）返回的门店**没有** `beCode`；`beType=5`（得来速）**有** `beCode`，
  后续调用外送相关工具时必须传。
- **关键词会被解析到具体地标**：传「广州」会命中广州南站，传「广州塔」才命中海珠一带。
- `city` 是必填项，只传 `keyword` 会报「城市名或者关键词不能为空」。
- `searchType=1`（收藏餐厅）在无收藏记录时返回错误码 `600050`。

**本工具用法**：分别用 `beType=1` 取到店门店列表。

---

### 2.2 `query-meals` — 拉菜单（比价与折扣统计的核心）

| 项 | 值 |
|---|---|
| 作用 | 查询指定门店当前可售卖的餐品列表 |
| 必填参数 | `storeCode`、`orderType`（1=到店 / 2=外送）、`beType`（1/2/5/6） |
| 返回 | `categories[]`（分类）、`meals{}`（商品字典，key 为 productCode） |

**返回结构**：

```json
{
  "categories": [ { "name": "人气热卖", "meals": [ {"code": "9900016076", "tags": [...]} ] } ],
  "meals": {
    "1100": {
      "name": "巨无霸",
      "image": "https://menu-img.mcd.cn/...",
      "currentPrice": "27.5",
      "originalPrice": "27.5",
      "canWithOrder": false
    }
  }
}
```

**商品对象的字段全集只有 5 个**：`name`、`image`、`currentPrice`、`originalPrice`、`canWithOrder`。

**实测要点**：

- **菜单不带营养成分**，营养数据要另调 `list-nutrition-foods`。
- **菜单不带套餐构成**，套餐要另调 `query-meal-detail` 拿 `rounds`。
- `originalPrice != currentPrice` 即代表该商品在活动折扣中。
  这是判断「这家店有没有在做活动」的唯一可靠依据。
- **不同门店的菜单价格不同**，且差异可能很大（实测同款套餐极差 ¥27.1）。
  部分差异来自活动折扣（`originalPrice` 相同、`currentPrice` 不同），
  部分来自门店独立定价（`originalPrice` 本身就不同，实测 83 项共同商品中 60 项如此）。
- 实测单店菜单 81–134 项不等。

**本工具用法**：逐店调用，建立 `productCode → price` 索引，用于：
1. 同商品跨店比价（取 min/max/极差）
2. 各店折扣覆盖率（有折扣项数 / 总项数）
3. 各店折扣总额（`originalPrice - currentPrice` 求和）

---

### 2.3 `query-store-coupons` — 门店可用券

| 项 | 值 |
|---|---|
| 作用 | 查询指定门店+订单类型下可用的优惠券 |
| 必填参数 | `storeCode`、`orderType`、`beType` |
| 条件参数 | `beCode`（外送/得来速/团餐必传）、`reservationDate` |
| 返回 | `title`、`couponId`、`couponCode`、`tradeDateTime`、`products[]`、`promotionId` |

**返回结构**：

```json
[
  {
    "title": "13.9浓浓黑巧",
    "couponId": "32EC0AE5FAE33F46B20027B94A7D7143",
    "couponCode": "MCD697Y560A410M010Q06",
    "tradeDateTime": "2026-10-17 00:00:00-2026-10-23 23:59:59",
    "products": [ { "productCode": "9900016270", "productName": "浓浓黑巧中杯（冰/热）" } ],
    "promotionId": "112464002"
  }
]
```

**实测要点**：

- **这是唯一能拿到券的 `products` 字段的接口**。
  卡包接口 `query-my-coupons` 的券对象有 60+ 字段，但**不含 `products`**——
  想列出「这张券能买什么」，必须至少调一次本接口，而本接口又需要先知道门店。
- **`orderType=2`（外送）必须传 `beCode`**，否则报错误码 `600048`
  「外送场景下，beType=2/5/6时必传beCode」。
- 某店没有可用券时返回**空数组**，不是错误。
- `products[].productCode` 常常是**虚拟组合项**，见 2.4。

**本工具用法**：逐店调用，得到「哪些店能用哪些券」+「券指向哪些商品」。

---

### 2.4 `query-meal-detail` — 展开券的虚拟编码

| 项 | 值 |
|---|---|
| 作用 | 查询餐品详情，含套餐构成与特制选项 |
| 必填参数 | `storeCode`、`orderType`、`beType`、`code` |
| 条件参数 | `beCode`、`reservationDate` |
| 返回 | `code`、`name`、`rounds[]`、`modification`、`supportModify` |

**这是本项目最关键的一次「数据考古」。**

券声明的 `productCode` 常常是一个**虚拟组合项**，在菜单里查不到。
用 `query-meal-detail` 查它，会返回可展开的 `rounds`：

```
query-meal-detail(code="9900016270")
→ rounds[1] 名称「选择麦咖啡饮品」 qty=1
   · 515946 | 冰浓浓黑巧中杯 | isDefault=1 | diff=+ ¥0 | supportModify=true
   · 515945 | 热浓浓黑巧中杯 | isDefault=0 | diff=+ ¥0 | supportModify=true
```

而 `query-meals` 里实际暴露的是合并后的 `515946`（现价 ¥13.9 / 原价 ¥21）。
**菜单层做了温度合并，详情层拆开了**，所以映射不是一一对应。

**套餐的 rounds 结构**（用于识别「套餐陷阱」）：

```
query-meal-detail(code="9900005460")   # 麦香鱼套餐
→ rounds[0] 麦香鱼 ×1              choices: 麦香鱼
   rounds[1] 选择套餐内小食 ×1        choices: 中薯条 / 5块心形薯饼 / ...
   rounds[2] 选择套餐内饮料 ×1        choices: 可乐中杯 / ... / 热浓浓黑巧中杯
```

> ⚠️ `choices` 列出的是该 round 的**全部可选项**，不是已选中的那一个。
> 判断实际组成需结合 `isDefault`。

---

### 2.5 `calculate-price` — 算价与优惠验算

| 项 | 值 |
|---|---|
| 作用 | 计算商品价格（含优惠） |
| 必填参数 | `storeCode`、`orderType`、`beType` |
| 条件参数 | `items[]`、`couponId`、`couponCode`、`beCode`、`withOrder`、`needTableware` |
| 返回 | `price`、`originalPrice`、`discount`、`productList[]` |

**三个必须记住的约定**：

1. **商品字段名是 `productCode`，不是 `code`。**
   传 `code` 会**静默返回全 0**（`price=0, originalPrice=0, discount=0`），不报错。
   这是本项目在开发中遇到的第一个坑。

2. **券要传在顶层，不在 `items` 里。**
   传 `items[].couponId` 会报错；传顶层 `couponId` 才生效。

3. **金额单位是「分」。**
   巨无霸返回 `price: 2750` = ¥27.50。`discount: 710` = ¥7.10。
   算总价必须 /100。

**正确调用示例**：

```json
{
  "storeCode": "1400147", "orderType": 1, "beType": 1,
  "items": [ { "productCode": "515946", "quantity": 1 } ],
  "couponId": "32EC0AE5FAE33F46B20027B94A7D7143"
}
```

**返回**：`price=1390`（¥13.90），`originalPrice=2100`（¥21.00），`discount=710`（¥7.10）。

**本工具用法（关键）**：这是判定「券能不能用于某商品/套餐」的**唯一可靠方法**。

```python
# 券在单品上生效
calculate_price(store, items=[{productCode: "515946", quantity: 1}], coupon_id=cid)
# → discount=710  ✅ 券可用

# 券在套餐上不生效
calculate_price(store, items=[{productCode: "9900005466", quantity: 1}], coupon_id=cid)
# → discount=0    ❌ 券不适用
```

> **为什么必须实测算价？**
> 券对象里 `appliedCategory=0`、`multipleProducts=false` 这些字段**不足以判断适用性**。
> 实测证明：同一张券在单品上 discount=710，在套餐上 discount=0，
> 而这两个字段的取值完全一样。**只有实测算价能给出真相。**

---

### 2.6 `query-my-coupons` — 用户卡包券

| 项 | 值 |
|---|---|
| 作用 | 获取用户卡包中的优惠券资产 |
| 必填参数 | 无 |
| 条件参数 | `page`、`pageSize` |
| 返回 | `coupons[]`、`totalCount`、`currentPage`、`pageSize`、`totalPages` |

**券对象的 60+ 字段中，与本工具相关的**：

| 字段 | 含义 | 实测示例 |
|---|---|---|
| `id` / `code` | 券 ID / 券码 | `32EC0AE5...` / `MCD697Y560A410M010Q06` |
| `title` | 券名 | `13.9浓浓黑巧` |
| `discountInfo.discountValue` | 面值 | `13.9` |
| `tradeStartDate` / `tradeEndDate` | 有效期起止 | `2026-10-17` / `2026-10-23` |
| `receivedDays` / `receivedDaysMsg` | 领券后几天内有效 | `4` / `4日前收到` |
| `weekdays` / `isAllTradeTime` / `tradeTimePeriods` | 星期与时段限制 | `[]` / `1` / `[]` |
| `orderType` / `orderTypes` | 到店/外送 | `1` / `[1,2]` |
| `beType` / `beTypes` | 场景 | `1` / `[1,2,3,5]` |
| `channels` | 适用渠道 | `["03","14","45","54"]` |
| `isPrepaid` / `ifFreeCoupon` / `paymentType` | 支付相关约束 | `0` / `false` / `0` |
| `isCanGive` / `supportReservation` / `supportSoldOut` | 赠送/预约/售罄 | `1` / `0` / `0` |
| `enable` | 是否启用 | `1` |
| `totalAvailableCount` / `currentDayAvailableCount` | 库存 / 今日可用 | `1` / `1` |

**⚠️ 重要：这个接口不返回 `products`**，券的适用商品必须去 2.3 拿。

---

### 2.7 `order-list` — 历史消费画像

| 项 | 值 |
|---|---|
| 作用 | 查询历史订单（非商城） |
| 必填参数 | 无 |
| 返回 | `list[]`，每项含 `orderId`、`storeCode`、`storeName`、`createTime`、`orderStatus`、`realTotalAmount`、`orderProductList[]` |

**订单商品结构**：

```json
{
  "productCode": "1100", "productName": "巨无霸", "quantity": 1, "comboItemList": []
}
```

**实测要点**：

- 订单里有 `productCode`，**可以反查菜单拿单价**（订单本身不带单价）。
- 订单**不记录用了哪张券、抵扣了多少**——这部分信息 MCP 没有提供。
- **`times` 与 `qty` 必须分开看**：
  实测样本中「板烧鸡腿堡」只出现 1 次但买了 4 份（一次多份，非复购），
  「德式图林根香肠」出现 3 次共 6 份（真复购）。
  混为一谈会得出错误偏好判断。

**本工具用法**：统计复购次数、份数、常去门店、客单价，叠加到决策建议里。

---

### 2.8 `query-my-account` — 积分账户（用于扩展面板）

| 项 | 值 |
|---|---|
| 作用 | 查询麦享会积分账户详情 |
| 必填参数 | 无 |
| 返回 | `availablePoint`、`accumulativePoint`、`usedPoint`、`expiredPoint`、`frozenPoint`、`currentMouthExpirePoint`、`lastMouthExpirePoint`、`nextMouthExpirePoint` |

实测样本：累计 4893.2 / 已用 3033 / **已过期 1359.6** / 可用 500.6 → **沉没率 27.8%**。

这些字段是「积分资产体检」扩展面板的数据源（当前版本尚未接入主流程）。

---

### 2.9 `now-time-info` — 服务端时间

| 项 | 值 |
|---|---|
| 作用 | 获取服务端完整时间（时间戳/时区/年月日） |
| 必填参数 | 无 |

**用途**：券的剩余天数必须以**服务端时间**为准，不能用本地时间——
用户机器时区或时钟可能不准。本项目通过 `query-my-coupons` 返回的
`tradeEndDate` 结合 `now-time-info` 计算剩余天数。

---

## 三、Tool 清单 · 已验证未使用的工具

| 工具 | 验证结果 | 未使用原因 |
|---|---|---|
| `list-nutrition-foods` | 160 条营养数据，9 字段 CSV 文本 | 与菜单 join 率仅 40%（规格后缀不匹配），不作为决策依据 |
| `campaign-calendar` | 正常返回当月活动 | 用于「何时买」建议，当前版本未接入 |
| `mall-points-products` | 50 件商品 | 用于积分折价计算，属扩展面板 |
| `query-lottery-info` | 24 积分/次，10 个奖品 | 写操作相关，不纳入只读主流程 |
| `available-coupons` | 3 张可领券 | 只给券名和图片，不给门店范围 |
| `delivery-query-stores` | 需 addressId | 账号地址簿为空 |
| `query-promotions` | 报 `600048` | 外送场景必传 beCode，本版本只做到店场景 |
| `query-meal-assistance` | 报 `610501` | 仅企业团餐场景（beType=6）可用 |

**本项目全程只做只读查询**，不下单、不领券、不抽奖、不修改地址。

---

## 四、调用流程

### 4.1 完整链路

```
CLI 入口
  ↓
MCP 客户端（鉴权 / 限流 / 失败降级）
  ↓
取数层 ── 9 个只读工具的真实返回
  ↓
单位换算 ── 唯一的换算点（calculate-price 返回分，其余返回元）
  ↓
计算层 ── 四个维度
  ↓
输出层 ── 六段式决策卡
```

**核心原则：取数交给 MCP，算术交给代码，表达交给 Agent。**
程序不生成任何数字——每个金额都来自接口返回，算术只有加、减、除、比较。

### 4.2 调用顺序与依赖

| 步 | 工具 | 次数 | 依赖 |
|---|---|---|---|
| 1 | `query-nearby-stores` | 1 + 每对照片区 1 | — |
| 2 | `query-meals` | 每店 1 | 依赖步 1 的 `storeCode` |
| 3 | `query-store-coupons` | 每店 1 | 依赖步 1 的 `storeCode` |
| 4 | `query-my-coupons` | 1 | — |
| 5 | `query-meal-detail` | 每券 1 + 套餐候选 | 依赖步 3 的 `products[].productCode` |
| 6 | `calculate-price` | 每券 1 + 套餐验算 | 依赖步 5 展开后的真实编码 |
| 7 | `order-list` | 1 | — |
| 8 | `now-time-info` | 1 | — |

**实测一次完整运行**：34 次调用，13.4 秒，限流上限 600/分余量充足。
调用构成：`query-meals` 10 / `query-meal-detail` 7 / `calculate-price` 7 /
`query-nearby-stores` 4 / `query-store-coupons` 4 / `query-my-coupons` 1 / `order-list` 1。

### 4.3 关键依赖链：券的适用范围判定

这是整个项目技术含量最高的一段，**三步缺一不可**：

```
query-store-coupons  →  券声明的 productCode（可能是虚拟项，菜单里查不到）
        ↓
query-meal-detail    →  rounds[].choices[].code  展开为真实编码
        ↓
calculate-price      →  discount 判定券是否生效（整数比较，不看不可靠字段）
```

实测证据：

```
query-meal-detail  code=9900016270   → rounds=1 choices=2
   虚拟编码 9900016270 → 真实编码 515946（冰）/ 515945（热）
calculate-price    couponId=32EC...  → price=1390 discount=710
   单点黑巧：优惠 710 分 = ¥7.10，券生效
calculate-price    couponId=32EC...  → price=3350 discount=0
   麦辣鸡腿汉堡三件套：优惠 0 分，券不生效 → 记为陷阱
```

---

## 五、真实调用追踪

`scripts/trace_run.py` 包装 `McdClient.call`，把每次调用的工具、关键参数、
返回摘要全部打印出来，同时把业务逻辑拆成 7 步展示。

```bash
export MCD_MCP_TOKEN="你的Token"
python scripts/trace_run.py 佛山 禅城区
```

完整输出见 [`docs/trace-output.txt`](docs/trace-output.txt)（258 行真实记录，已脱敏）。

**七步结构**：

| 步 | 内容 | 输出 |
|---|---|---|
| 1 | 初始化客户端 | 接入地址、Token 来源、限流阈值 |
| 2 | 定位门店 + 拉菜单 | 4 家门店 / 394 个商品条目 |
| 3 | 拉各店可用券 | 1/0/0/0 项（空数组是有效数据） |
| 4 | 比价 + 折扣覆盖率 | 零接口调用，纯本地计算 |
| 5 | 判定券适用范围 | 虚拟编码展开 → 实测算价 |
| 6 | 历史消费画像 | 5 单 / ¥233.00 / 客单价 ¥46.60 |
| 7 | 跨片区对比 | 佛山 47.3% / 广州塔 35.0% / 南站 0.0% |

**这份追踪日志同时是回归工具**：改动后跑一遍就能看出接口调用数有没有异常增长、
每个金额的真实来源是否正确。

---

## 六、业务价值

### 6.1 定价体系差异可视化

社区店折扣覆盖率稳定在 46–51%，地标/枢纽店 0–1%。
这个断层让「同款在不同店差多少」从不可知变成一张表。

### 6.2 券的可用性从「试错」变成「查询」

一张券在多少家店能用、哪几家能用、券认不认套餐——
原本只能一家家点进去试，现在一次查询得到。

### 6.3 避免沉没式浪费

券到期前 3 天提醒 + 指定可用门店 + 明确「套餐里用不了」，
让券的优惠真正落到实付上。

---

## 七、错误处理

| 服务端返回 | 含义 | 本项目的处理 |
|---|---|---|
| `600048` | 外送场景必传 beCode | 捕获后降级到到店场景，不中断整体流程 |
| `610501` | 仅团餐场景可用 | 同上，跳过该工具 |
| `499` | 该订单无匹配答卷 | 跳过该订单 |
| `600050` | 收藏餐厅列表为空 | 回退到 `searchType=2` 按位置搜索 |
| `401` | Token 无效或已过期 | 明确报错，提示重新申请 Token |
| `429` | 触发限流（>600 次/分） | 客户端内置滑动窗口限流，主动等待而非重试 |

客户端实现见 [`scripts/mcd_client.py`](scripts/mcd_client.py)，
`call_safe()` 用于可选数据源，失败返回 `None` 而不中断主流程。

---

## 八、复现方式

```bash
export MCD_MCP_TOKEN="你的Token"

# 基础
python scripts/price_scout.py --city 佛山 --keyword 禅城区

# 跨片区对比（复现 README 的 ¥27.1 极差）
python scripts/price_scout.py --city 佛山 --keyword 禅城区 \
    --compare 广州/广州塔 --compare 广州/南站 --compare 佛山/禅城区
```

完整工具清单与官方文档：<https://github.com/M-China/mcd-mcp-server>
