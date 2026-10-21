# MCP 调用详解

> 基于对 `https://mcp.mcd.cn` 的真实抓包（2026-10-21 13:04-13:06）
> 对应代码：`scripts/mcd_client.py`

---

## 一、协议层

### 1.1 传输

| 项 | 值 |
|---|---|
| 协议 | **MCP（Model Context Protocol）over JSON-RPC 2.0** |
| 传输 | Streamable HTTP |
| 端点 | `POST https://mcp.mcd.cn` |
| 方法 | 全部是 POST，请求体里的 `method` 字段区分 |
| 鉴权 | 请求头 `Authorization: Bearer <MCP Token>` |
| 请求头 | `Content-Type: application/json`<br>`Accept: application/json, text/event-stream` |

**所有 MCP 方法共用一个 URL**，靠 body 里的 `method` 区分。这与 REST 的「一个 URL 对一个动作」不同。

### 1.2 三种方法

| method | 用途 | 本项目是否使用 |
|---|---|---|
| `initialize` | 能力协商，确认协议版本 | **仅自检时用** |
| `tools/list` | 列出服务端全部工具 | 自检时用 |
| `tools/call` | 调用具体工具 | **业务主路径，34 次全部走它** |

---

## 二、真实报文（逐字节）

### 2.1 `initialize` —— 握手

```http
POST https://mcp.mcd.cn HTTP/1.1
Authorization: Bearer <你的MCP_TOKEN>
Content-Type: application/json
Accept: application/json, text/event-stream
Content-Length: 183
```
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {
    "protocolVersion": "2024-11-05",
    "capabilities": {},
    "clientInfo": { "name": "mcd-store-price-scout", "version": "1.0" }
  }
}
```

响应：

```json
{"jsonrpc":"2.0","id":1,"result":{
  "capabilities":{"tools":{}},
  "protocolVersion":"2024-11-05",
  "serverInfo":{"name":"mcd-mcp","version":"1.0.0"}
}}
```

### 2.2 `tools/list` —— 取工具清单

请求体只有 65 字节，**响应体 76,979 字符**（含 35 个工具的完整 inputSchema 与 outputSchema）。

```json
{ "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {} }
```

响应的第一个工具长这样：

```json
{
  "name": "list-nutrition-foods",
  "description": "获取麦当劳常见餐品的营养成分数据……",
  "inputSchema": { "properties": {}, "type": "object" },
  "outputSchema": {
    "description": "OK",
    "properties": {
      "code": { "description": "错误码", "type": "number" },
      "data":  { "description": "餐品热量列表", "type": "string" },
      "message": { "description": "错误信息", "type": "string" }
    }
  }
}
```

> **`outputSchema` 里就写明了 `code` / `message` / `data` 三段式**——
> 这是后面错误处理机制的依据。

### 2.3 `tools/call` —— 业务主路径

```http
POST https://mcp.mcd.cn HTTP/1.1
Authorization: Bearer <你的MCP_TOKEN>
Content-Type: application/json
Accept: application/json, text/event-stream
Content-Length: 181
```
```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "query-nearby-stores",
    "arguments": {
      "searchType": 2,
      "beType": 1,
      "city": "佛山",
      "keyword": "禅城区"
    }
  }
}
```

**参数是 `params.arguments` 里的扁平字典**，不是嵌套对象。

响应结构（这是本项目解析的核心）：

```json
{
  "jsonrpc": "2.0", "id": 1,
  "result": {
    "content": [ { "type": "text", "text": "每个门店的基本信息按单行展示：…" } ],
    "isError": false,
    "structuredContent": {
      "success": true,
      "code": 200,
      "message": "请求成功",
      "datetime": "2026-10-21 13:05:04",
      "traceId": "…",
      "data": [ { "storeCode": "1400956", "storeName": "…", … } ]
    }
  }
}
```

---

## 三、★ 最关键的机制：业务失败时 HTTP 仍是 200

实测外送场景缺 `beCode`：

```http
POST https://mcp.mcd.cn HTTP/1.1
...
{ "jsonrpc":"2.0","id":1,"method":"tools/call","params":{
    "name":"query-store-coupons",
    "arguments":{"storeCode":"1400147","orderType":2,"beType":2}}}
```

响应：

```http
HTTP/1.1 200 OK          ← 注意：这里是 200，不是 4xx
```

```json
{
  "result": {
    "content": [ { "type": "text", "text": "## Response Structure …" } ],
    "isError": false,      ← 连 isError 都是 false
    "structuredContent": {
      "success": false,    ← 真正的失败标志在这里
      "code": 600048,
      "message": "外送场景下，beType=2/5/6时必传beCode",
      "datetime": "2026-10-21 13:05:05",
      "traceId": "…"
    }
  }
}
```

### 三重判定，必须逐层查

| 检查顺序 | 字段 | 含义 |
|---|---|---|
| 1 | HTTP 状态码 | **不可靠**——业务失败也是 200 |
| 2 | `result.isError` | 传输级错误 |
| 3 | `structuredContent.success` | **业务级成功与否（真正的判据）** |
| 4 | `structuredContent.code` | 官方错误码（600048 / 610501 / 499 / 600050 …） |
| 5 | `structuredContent.message` | 中文原因描述 |

### 代码里的对应实现

`mcd_client.py:116` `call()`：

```python
result = self._rpc("tools/call", {"name": tool, "arguments": arguments})

if result.get("isError"):          # 第 2 层
    raise McdError(...)

sc = result.get("structuredContent")
if sc is None:                     # 兼容无 structuredContent 的情况
    ...  # 退回解析 content[].text

if sc.get("success") is False:     # 第 3 层 ← 真正的判据
    raise McdError(f"工具 {tool} 失败[{sc.get('code')}]：{sc.get('message')}")

return sc.get("data")              # 只取 data
```

**如果只看 HTTP 状态码，600048 这类错误会被当成成功**，
然后 `data` 是 `None`，后续计算会静默出错。这是最容易踩的坑。

---

## 四、三层调用封装

```
业务层    price_scout.py          ← 只关心「门店快照」「券范围」这些业务概念
          ↓
语义层    mcd_client.py           ← get_menu / get_store_coupons / calculate_price …
          ↓                        每个函数封装一个工具 + 它的前置约束
协议层    McdClient._rpc()        ← JSON-RPC 封装、HTTP、限流、错误解析
```

### 4.1 协议层 `_rpc()`（`mcd_client.py:68`）

职责：
1. 组装 JSON-RPC 2.0 请求体，`id` 自增
2. `urllib.request.Request` 发 POST（**只用标准库，零第三方依赖**）
3. **限流检查**（`_throttle()`）
4. 捕获 `HTTPError` / `URLError`，转成 `McdError`
5. **剥 SSE 前缀**——`Accept` 里带了 `text/event-stream`，服务端可能返回 `data: {...}` 格式
6. `json.loads` 解析，检查 `error` 字段

**SSE 剥离逻辑**（实测必要）：

```python
text = raw
if "data:" in raw:
    chunks = [ln[len("data:"):].strip() for ln in raw.splitlines() if ln.startswith("data:")]
    text = chunks[-1] if chunks else raw
```

### 4.2 语义层封装

每个函数把「工具的调用约束」封在里面，调用方不需要知道：

| 函数 | 封装了什么 |
|---|---|
| `get_menu()` | `orderType`/`beType` 默认值；外送需 `beCode`；返回 `meals{}` 子字典 |
| `get_store_coupons()` | 同上；**空数组是有效数据**，返回 `[]` 而非 None |
| `get_nearby_stores()` | `searchType=2` + `city`/`keyword` 组合；返回门店列表 |
| `get_meal_detail()` | **带缓存**——按 `(storeCode, code, orderType, beType, beCode)` 复用 |
| `calculate_price()` | **`productCode` 字段名**；**券传顶层**；返回原始「分」值不换算 |
| `get_order_history()` | 从 `data.list` 取出订单数组 |
| `get_my_coupons()` | 从 `data.coupons` 取出券数组 |
| `get_my_account()` | 积分账户 |

**示例**——`calculate_price` 封装了两个致命细节：

```python
def calculate_price(client, store_code, items, coupon_id=None, ...):
    args = {"storeCode": store_code, "orderType": order_type, "beType": be_type, "items": items}
    if coupon_id:
        args["couponId"] = coupon_id      # ← 券必须传顶层，不能塞进 items
    data = client.call_safe("calculate-price", **args)
    return data if isinstance(data, dict) else {}
```

而 `items` 里的字段名必须是 `productCode`——传 `code` 会**静默返回 price=0**，不报错。

### 4.3 业务层

`price_scout.py` 完全不知道 HTTP 的存在，只调用语义层函数。
这就是「取数交给 MCP，算术交给代码」在代码结构上的体现。

---

## 五、★ 该服务端是无状态的：不需要 initialize 握手

**实测对比**：

| 方式 | 结果 |
|---|---|
| 直接发 `tools/call`（不发 initialize） | ✅ 正常返回 |
| 先 `initialize` 再 `tools/call` | ✅ 同样正常返回 |

两者响应完全一致。

**原因**：该服务端**不返回 `Mcp-Session-Id`**，不做会话绑定，每个请求独立处理。
所以本项目的 `McdClient` **不做 initialize 握手**——每次 `tools/call` 直接发出去。

**`initialize` 只在两个场景用**：
- `mcd_client.py` 的 `__main__` 自检（打印工具数）
- `list_tools()` 方法（供开发期查看）

**这带来一个实际好处**：省掉一次往返，34 次调用的总耗时 13.4 秒。

如果换成一个有状态的 MCP 服务端（需要 `Mcp-Session-Id` 后续传递），
只需在 `_rpc()` 里加两行——这也是为什么把协议层封在一个类里的价值。

---

## 六、限流保护

**官方限制**：每 Token 每分钟 600 次请求，超限返回 429。

**实现**（`mcd_client.py:55`）：

```python
def _throttle(self):
    now = time.monotonic()
    self._call_times = [t for t in self._call_times if now - t < 60.0]  # 清掉 60 秒外的
    if len(self._call_times) >= RATE_LIMIT_PER_MIN:
        sleep_for = 60.0 - (now - self._call_times[0]) + 0.5
        time.sleep(max(sleep_for, 0.1))          # 主动等待，而不是重试
        now = time.monotonic()
        self._call_times = [t for t in self._call_times if now - t < 60.0]
    self._call_times.append(now)
```

**设计要点**：
- **滑动窗口**而非固定窗口——更保守，不会卡在分钟边界
- **主动等待而非重试**——避免重试风暴
- 调用前检查，不等到 429 再补救

**实测余量**：一次完整运行 34 次调用，13.4 秒。距 600/分 的上限极远。

---

## 七、失败降级：两种策略

| 策略 | 方法 | 用在哪 | 理由 |
|---|---|---|---|
| **抛异常** | `call()` | 核心数据源（菜单、算价） | 失败必须让用户知道，不能假装成功 |
| **返回 None** | `call_safe()` | 可选数据源（受限工具） | 单个可选源失败不应让整体结论丢失 |

```python
def call_safe(self, tool: str, **arguments: Any) -> Any:
    try:
        return self.call(tool, **arguments)
    except McdError as exc:
        if self.verbose:
            print(f"[mcp] {tool} 调用失败，忽略：{exc}")
        return None
```

**用 `call_safe()` 的工具**：`query-promotions`（外送必传 beCode）、
`query-meal-assistance`（仅团餐场景）、`query-survey-coupon`（订单无答卷）等受限接口。

**关键原则**：`call_safe()` 返回 `None` 时，调用方必须**显式处理**，
不能用 0 或猜测值填充——这是「不编造数据」在代码层的落实。

---

## 八、Token 处理

```python
self.token = token or os.environ.get(TOKEN_ENV, "")   # MCD_MCP_TOKEN

if not self.token:
    raise McdError(
        f"未找到 MCP Token。请先申请 Token（https://open.mcd.cn/mcp），"
        f"然后设置环境变量 {TOKEN_ENV}，例如：\n"
        f"  PowerShell: $env:{TOKEN_ENV}='你的Token'\n"
        f"  Bash:       export {TOKEN_ENV}='你的Token'"
    )
```

- 只从**环境变量**读，不接受命令行参数（避免进入 shell 历史）
- 缺失时**明确报错并给出获取步骤**，不静默降级
- 全仓库扫描确认无明文（CI 每次 push 检查）

---

## 九、一次完整运行的调用分布

**实测（2026-10-21 11:53，34 次调用 / 13.4 秒）**：

| 工具 | 次数 | 调用时机 |
|---|---:|---|
| `query-meals` | 10 | 每家门店 1 次（跨片区复用后） |
| `query-meal-detail` | 7 | 展开券的虚拟编码 + 套餐构成 |
| `calculate-price` | 7 | 验算券是否生效（1 单品 + 6 套餐） |
| `query-nearby-stores` | 4 | 主查询 1 + 跨片区 3 |
| `query-store-coupons` | 4 | 每家门店 1 次 |
| `query-my-coupons` | 1 | 卡包券 |
| `order-list` | 1 | 历史订单 |

**依赖关系**：

```
query-nearby-stores  →  storeCode
        ↓
query-meals / query-store-coupons        （可并行）
        ↓
query-meal-detail（用 store-coupons 给的 productCode 展开）
        ↓
calculate-price（用展开后的真实编码验算）
```

`query-meal-detail` 与 `calculate-price` 是**严格串行**的——
后者依赖前者产出的真实编码。

---

## 十、错误码速查

| 码 | 含义 | 本项目处理 |
|---|---|---|
| 401 | Token 无效/过期 | 抛 `McdError`，提示重新申请 |
| 429 | 触发限流 | 客户端主动等待，不重试 |
| 400003 | 鉴权头缺失 | 由 `_rpc` 的请求头保证 |
| 600048 | 外送场景必传 `beCode` | 降级到到店场景 |
| 610501 | 仅企业团餐场景可用 | `call_safe` 跳过 |
| 499 | 订单无匹配答卷 | `call_safe` 跳过 |
| 600050 | 收藏餐厅列表为空 | 回退 `searchType=2` |
| 0001 | 系统错误（无鉴权时） | 抛 `McdError` |
| —— | `calculate-price` 传错字段名 | **静默返回 price=0，不报错** ← 最危险 |

最后一条不是服务端错误码，而是**接口设计陷阱**，只能靠自测与交叉校验防住。
