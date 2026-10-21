# 麦当劳 MCP 工具清单与字段映射

> 基于 `https://mcp.mcd.cn` 实际调用验证（2026-10-21，服务端 v1.0.9，35 个工具）。
> 完整官方文档见 <https://github.com/M-China/mcd-mcp-server>。

## 接入信息

| 项 | 值 |
|---|---|
| 接入地址 | `https://mcp.mcd.cn` |
| 传输协议 | Streamable HTTP |
| 鉴权头 | `Authorization: Bearer YOUR_MCP_TOKEN` |
| 限流 | 每 Token 每分钟 600 次，超限返回 429 |
| 错误码 | 401 = Token 无效/过期；429 = 触发限流 |
| 开放平台 | <https://open.mcd.cn/mcp> |
| 服务规则 | <https://cdn.mcd.cn/cms/pages/MCPServerRules.html> |

## 本项目使用的 9 个工具

| 工具 | 必填参数 | 用途 |
|---|---|---|
| `query-nearby-stores` | beType, searchType(+city, keyword) | 找门店 |
| `query-meals` | storeCode, orderType, beType | 拉菜单（比价 + 折扣） |
| `query-store-coupons` | storeCode, orderType, beType | 门店可用券 + 券的 products |
| `query-meal-detail` | storeCode, orderType, beType, code | 展开虚拟编码 + 套餐构成 |
| `calculate-price` | storeCode, orderType, beType | 算价 + 验算券是否生效 |
| `query-my-coupons` | — | 用户卡包券（60+ 字段） |
| `order-list` | — | 历史消费画像 |
| `query-my-account` | — | 积分账户（扩展面板） |
| `now-time-info` | — | 服务端时间（算券剩余天数用） |

## 关键字段映射

### 菜单商品对象（字段全集只有 5 个）

```json
{ "name": "巨无霸", "image": "...", "currentPrice": "27.5",
  "originalPrice": "27.5", "canWithOrder": false }
```

| 字段 | 用途 |
|---|---|
| `currentPrice` | 比价、算价基数 |
| `originalPrice` | 与 currentPrice 比较判定是否有折扣；**跨店可能本身就不同** |
| `name` | 展示、与营养表/券名关联 |

### 券对象（60+ 字段中与本项目相关的）

| 字段 | 用途 | 实测值 |
|---|---|---|
| `id` | 关联门店券的 `couponId` | `32EC0AE5...` |
| `code` | 券码 | `MCD697Y560A410M010Q06` |
| `title` | 展示 | `13.9浓浓黑巧` |
| `discountInfo.discountValue` | 面值 | `13.9` |
| `tradeStartDate` / `tradeEndDate` | 有效期 | `2026-10-17` / `2026-10-23` |
| `receivedDays` / `receivedDaysMsg` | 领券后有效期 | `4` / `4日前收到` |
| `weekdays` | 星期限制（空=不限） | `[]` |
| `isAllTradeTime` / `tradeTimePeriods` | 时段限制 | `1` / `[]` |
| `orderType` / `orderTypes` | 到店/外送 | `1` / `[1,2]` |
| `beType` / `beTypes` | 场景 | `1` / `[1,2,3,5]` |
| `channels` | 适用渠道 | `["03","14","45","54"]` |
| `isPrepaid` / `ifFreeCoupon` / `paymentType` | 支付约束 | `0` / `false` / `0` |
| `isCanGive` / `supportReservation` / `supportSoldOut` | 赠送/预约/售罄 | `1` / `0` / `0` |
| `enable` | 是否启用 | `1` |
| `totalAvailableCount` / `currentDayAvailableCount` | 库存 | `1` / `1` |

> ⚠️ **卡包券对象不含 `products`**。券的适用商品必须从 `query-store-coupons` 获取。

### 订单对象

| 字段 | 用途 |
|---|---|
| `orderId` | 唯一标识 |
| `storeCode` / `storeName` | 门店统计 |
| `createTime` | 时间维度 |
| `realTotalAmount` | 金额（单位元） |
| `orderProductList[].productCode` | **可反查菜单拿单价** |
| `orderProductList[].productName` | 复购统计键 |
| `orderProductList[].quantity` | 与出现次数分开看，区分复购/一次多份 |

> 订单**不记录**用券与抵扣金额，MCP 未提供该信息。

## 已知坑（实测）

### 坑 1 · 券的 productCode 是虚拟项

券声明 `9900016270`，菜单里不存在，10 家受测店全都没有。
用 `query-meal-detail(code=9900016270)` 可展开：

```
rounds[1]「选择麦咖啡饮品」
  · 515946 | 冰浓浓黑巧中杯 | isDefault=1
  · 515945 | 热浓浓黑巧中杯 | isDefault=0
```

菜单只暴露合并后的 `515946`——**菜单层合并、详情层拆分，映射非一一对应**。

### 坑 2 · 券在套餐内不生效

```
calculate-price(items=[{productCode:"9900005466"(巨无霸三件套)}], couponId=...)
→ discount=0
```

尽管套餐 `rounds[2]`「选择套餐内饮料」的 choices 里有「热浓浓黑巧中杯」。
`appliedCategory=0`、`multipleProducts=false` 这两个字段**无法反映这个差异**，只有实测算价能。

### 坑 3 · calculate-price 字段名错了会静默返回 0

| 写法 | 结果 |
|---|---|
| `items:[{productCode:"1100", quantity:1}]` | ✅ `price=1390` |
| `items:[{code:"1100", quantity:1}]` | ❌ `price=0`，**不报错** |

券必须传**顶层** `couponId`，传 `items[].couponId` 会报错。

### 坑 4 · 金额单位是分

`price=1390` = ¥13.90；`discount=710` = ¥7.10。

### 坑 5 · 外送场景必传 beCode

`query-store-coupons` 在 `orderType=2` 时报 `600048`。
`beCode` 来自 `query-nearby-stores(beType=5)` 或 `delivery-query-stores`。

### 坑 6 · 关键词会解析到地标

传「广州」→ 命中广州南站；传「广州塔」→ 命中海珠一带。
且 `city` 必填，只传 keyword 报「城市名或者关键词不能为空」。

### 坑 7 · 营养表是文本不是 JSON

`list-nutrition-foods` 返回首行是表头 `[160]{productName,...}:`，每行 9 字段、行首两个空格。
与菜单按菜名 join 命中率仅 40%（"麦乐鸡" ≠ "麦乐鸡5块"）。

## 错误码

| 码 | 含义 | 处理 |
|---|---|---|
| 401 | Token 无效/已失效 | 提示重新申请 |
| 429 | 触发限流 | 客户端滑动窗口等待 |
| 600048 | 外送必传 beCode | 降级到店场景 |
| 610501 | 仅团餐场景可用 | 跳过该工具 |
| 499 | 订单无匹配答卷 | 跳过该订单 |
| 600050 | 收藏餐厅为空 | 回退 searchType=2 |
