# 设计文档 — 工银 i豆精算管家

本文档描述「工银 i豆精算管家」（拼乐智聊）的系统设计。这是一个面向微信小程序的 AI 智能客服系统，引导用户将工行「工银i豆」积分兑换为微信立减金或商品。

> 本文档描述**系统设计与实现架构**。前后端通信报文的字段级约定见 [websocket-protocol.md](./websocket-protocol.md)；对外接口见 [api.md](./api.md)。

---

## 1. 系统定位与总体架构

### 1.1 业务目标

用户在微信小程序中与 AI 助手对话，系统需要：

1. **导购比价** — 根据用户需求检索工行 i豆商城与微信小店商品，辅助决策「用工行商城直兑」还是「用立减金在微信小店购买」。
2. **立减金兑换** — 引导用户确定兑换方案并创建兑换订单，返回支付链接。
3. **客服问答** — 解答 i豆攒取攻略、立减金规则、订单状态、人工客服等问题。
4. **攒豆建议** — 当用户 i豆不足时给出攒豆方案。

### 1.2 分层架构

```
┌──────────────────────────────────────────────────────────────┐
│                     微信小程序前端                            │
└────────────┬─────────────────────────────────┬───────────────┘
             │ WSS  /v1/chat (业务)             │ mTLS TCP
             │ ← 聊天 / 拉历史 / 商品卡片        │ ← token 管理
┌────────────▼─────────────────────────────────▼───────────────┐
│                     point_server.py (FastAPI)                 │
│  ┌──────────────────┐      ┌────────────────────────────┐    │
│  │ WebSocket 端点    │      │ Token Management Server    │    │
│  │ /v1/chat          │      │ (mTLS, line-delimited JSON)│    │
│  └────────┬─────────┘      └────────────────────────────┘    │
│           │                                                   │
│  ┌────────▼─────────────────────────────────────────────┐    │
│  │        RedemptionAgent  (LangGraph 状态机)            │    │
│  │  ┌────────┐ ┌───────────────┐ ┌──────────┐ ┌───────┐ │    │
│  │  │ router │ │customer_service│ │ points_  │ │ goods_│ │    │
│  │  │  网关  │ │    客服房      │ │exchange  │ │exchange│ │    │
│  │  └────────┘ └───────────────┘ └──────────┘ └───────┘ │    │
│  └────────┬─────────────────────────────────────────────┘    │
│           │ ToolNode (LangChain @tool)                        │
└───────────┼───────────────────────────────────────────────────┘
            │
   ┌────────┼─────────┬──────────────┬───────────────┐
   ▼        ▼         ▼              ▼               ▼
┌──────┐┌───────┐┌─────────┐┌──────────────┐┌─────────────┐
│Redis ││Chroma ││ SQLite  ││ 拼乐后端 API  ││ DeepSeek/   │
│(会话)││(向量) ││(商品)   ││(下单/发券)   ││ DashScope   │
└──────┘└───────┘└─────────┘└──────────────┘└─────────────┘
```

### 1.3 技术选型

| 组件 | 选型 | 作用 |
|------|------|------|
| Web 框架 | FastAPI + uvicorn | WebSocket 服务、多进程 |
| Agent 编排 | LangGraph `StateGraph` | 多智能体状态机 |
| LLM 接入 | LangChain `ChatOpenAI` | 统一 OpenAI 兼容协议 |
| 会话持久化 | Redis + 自研 `SimpleRedisSaver` | LangGraph Checkpointer |
| 向量检索 | ChromaDB + DashScope Embedding | RAG 知识库 |
| 关系数据 | SQLite (WAL) | 推荐商品表 |
| 可观测性 | OpenTelemetry Metrics | 计数器/直方图 |
| 日志 | loguru | 按 PID 分文件 |

---

## 2. 多进程运行模型

### 2.1 进程拓扑

`python point_server.py` 通过 uvicorn 启动 `workers = process_num`（默认 `cpu_count()`）个**独立进程**，每个进程拥有：

- 独立的 event loop 与 `ThreadPoolExecutor`
- 独立的 Redis 连接池与 `RedemptionAgent` 实例
- 独立的 LLM 连接池（`model_factory` 内部单例）

### 2.2 Master / Worker 选举

多进程下必须保证**单例服务**（微信小店商品同步、mTLS token 服务）只在一个进程内运行。本项目**不使用**主进程分发，而是用**端口抢占**实现无锁选举：

```python
# point_server.py:160-168
server = await asyncio.start_server(handle_client, host, port, ssl=ssl_context)
state["is_master_node"] = True
except OSError as e:
  # 10048 是 Windows 端口占用，98 是 Linux 端口占用
  if e.errno in (10048, 98):
    state["is_master_node"] = False
    return  # 抢不到端口直接退出，该 Task 结束
  raise e
```

**设计意图**：谁成功 `bind` 了 token server 端口，谁就是 Master。抢占失败的进程 `errno` 命中端口占用错误，静默降级为 Worker。

为规避 Windows 上启动期的底层网络资源竞争，token server 在绑定前**刻意延迟 1.5s**（`point_server.py:95`），让业务端口先完成抢占。

### 2.3 启动时序（`lifespan_init`）

```
setup_opentelemetry()                      # 指标上报
  ↓
构造 ThreadPoolExecutor → loop.set_default_executor()
  ↓
setup_logger()                             # 日志（含 PID）
  ↓
TokenManager(ttl) ← 注入 shared_redis
  ↓
token_task = create_task(token_management_server())   # 竞争 Master
  ↓
SimpleRedisSaver(redis_client, ttl) → RedemptionAgent(saver=...)  # 编译图
  ↓
await init_singleton_classes()             # 预初始化 ICBCVectorDB
  ↓
轮询等待 state["is_master_node"] 就绪
  ↓
if Master: await start_single_services()   # 启动微信商品同步任务
```

注意 `init_singleton_classes()` 与 `main` 中显式调用 `ICBCVectorDB()` 的原因：**`_shared_version` 是 `multiprocessing.Value`，必须在父进程 fork/spawn 前分配共享内存**，否则子进程各自持有独立副本，蓝绿切换的跨进程失效（详见 §6.3）。

### 2.4 关闭时序（`lifespan_done`）

严格按依赖倒序释放：同步任务 → token server → agent 资源 → Redis（**先 client 后 pool**）。

---

## 3. 传输层设计

### 3.1 WebSocket 端点 `/v1/chat`

单端点承载两类请求，由 `type` 字段分流：

| type | 处理器 | 说明 |
|------|--------|------|
| `loadUserHistory` | `handle_load_history` | 拉取用户历史对话 |
| `chat`（默认） | `agent.stream_chat` | 对话主流程 |

**并发模型**：收到消息后 `asyncio.create_task` 派发，任务加入 `active_tasks` 集合，完成时通过 `add_done_callback(discard)` 自动移除。因此同一连接内**请求是 pipeline 的**——不必等上一个响应即可发送下一个，响应顺序不保证与请求一致，前端须用 `seq` 匹配。

连接断开（`WebSocketDisconnect`）或异常时，`finally` 中取消所有未完成任务。

### 3.2 Token 校验

每次收到请求（若 `token.enabled=true`）先校验 token：

```python
if not token_str or not await state["token_manager"].verify_token(token_str):
  发送 {status: "fail", errorCode: "INVALID_TOKEN", ...}
  await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
  break
```

校验失败即**强制断开连接**，而非仅拒绝该次请求。

### 3.3 mTLS Token 服务

独立 TCP 服务，基于 `asyncio.start_server`，**行分隔 JSON**（line-delimited JSON）协议：

- 服务端证书 `token_server.crt/key`，客户端校验 CA `token_ca_cert.crt`
- 三者齐备时 `verify_mode = CERT_REQUIRED`（双向认证）；缺 CA 则降级为仅服务端认证并告警；缺证书则退化为明文（依赖前端 Nginx 卸载 SSL）
- 支持命令：`getNewToken` / `cancelToken` / `wechatDBSync`

Token 本身是 UUID4 hex，存于 Redis，key 为 `{prefix}{token}`，通过 `setex` 设置 TTL。这种「Redis 键存在即有效」的设计使得 token 的有效性在多进程间天然一致。

---

## 4. Agent 核心设计

### 4.1 状态定义

```python
class AgentState(TypedDict):
  messages: Annotated[List[BaseMessage], operator.add]  # 归约合并
  current_agent: str                                    # 当前房间
```

`messages` 使用 `operator.add` 归约器，节点只需返回增量消息。

### 4.2 四个房间

| 房间 | 模型配置 | 工具集 | 职责 |
|------|---------|--------|------|
| `router` | `deepseek-flash` | 仅路由工具 | 意图识别与分流 |
| `customer_service` | `deepseek-flash` | 规则检索、订单查询、攒豆、返回 | 客服问答 |
| `points_exchange` | `deepseek-pro` | 下单、发券、返回 | 立减金兑换 |
| `goods_exchange` | `deepseek-pro` | 商城检索、微信小店检索、攒豆、返回 | 商品导购 |

模型按房间差异化配置，源自 `resource.yaml` 的 `agent_models`，经 `model_factory.get_model()` 获取（进程内单例，避免重复建连）。

**网关特殊绑定**：只有 router 使用 `tool_choice="required"`，强制大模型必须调用某个路由工具，杜绝「只回话不分流」：

```python
if agent_name == "router":
    self.runnable_agents[agent_name] = agent_llm.bind_tools(
        tools_list, strict=True, tool_choice="required")
```

### 4.3 路由机制（本设计的核心）

路由**不依赖 LLM 解析返回值**，而是通过**路由工具 + 条件边**实现确定性跳转。

**路由工具**（`core/llm_tools.py`）无参数、无副作用，仅返回约定的路由令牌字符串：

| 工具 | 返回令牌 | 触发语义 |
|------|---------|---------|
| `route_to_customer_service` | `ROUTE_CUSTOMER_SERVICE` | 咨询、查豆、订单状态、找人工 |
| `route_to_points_exchange` | `ROUTE_POINTS` | 纯 i豆兑换立减金 |
| `route_to_goods_exchange` | `ROUTE_GOODS` | 商品购买/比价 |
| `route_back_to_router` | `ROUTE_BACK` | 诉求超出本房间能力 |

**令牌消费**（`_tool_return_router`）：工具节点执行完后，读取最后一条 `ToolMessage.content`，比对令牌决定下一个节点：

```python
if last_msg.content == "ROUTE_CUSTOMER_SERVICE": return "customer_service_node"
elif last_msg.content == "ROUTE_POINTS":         return "points_exchange_node"
elif last_msg.content == "ROUTE_GOODS":          return "goods_exchange_node"
elif last_msg.content == "ROUTE_BACK":           return "router_node"
# 普通业务工具执行完毕，回归原母体房间继续研判
return f"{state.get('current_agent', 'router')}_node"
```

**这套设计的意义**：房间切换是**状态机级别的确定性行为**，而非「大模型说了算」。大模型只负责「选哪个工具」，工具名到节点的映射由代码固化，杜绝了自由文本解析带来的不确定性。

### 4.4 入口快车道

`_global_entry_router` 作为条件入口，根据上一轮的 `current_agent` 决定入口：

```python
last_node = state.get("current_agent", "router")
return f"{last_node}_node"
```

**设计意图**：多轮对话中，用户常常在同一房间内连续发言（如已在 goods 房继续追问商品）。若每轮都从 router 重判，会浪费一次 LLM 调用且可能误分流。因此**默认留在原房间**，只有当房间主动调用 `route_back_to_router` 时才回到网关重判。

### 4.5 消息卫生（关键实现细节）

LangGraph 会把所有历史消息持久化到 Redis，包括路由过程中的临时消息。若不清洗，下一轮 LLM 会看到「上一轮的路由意图」，导致行为污染。`_process_agent_node` 实施三层清洗：

**第一层 · 逆向清理尾部路由意图**

若最后一条消息是携带路由工具调用的 `AIMessage`（说明上一轮触发了换房，条件边直接拦截跳转，这条 AIMessage 遗留在新房历史末尾），则从尾部**向前弹出**消息，直到撞到 `HumanMessage` 为止：

```python
if any(call.get("name") in route_tool_names for call in tool_calls):
  while clean_messages:
    top_msg = clean_messages[-1]
    if top_type == "human" or isinstance(top_msg, HumanMessage):
      break            # 撞到用户发言刹车
    clean_messages.pop()
```

**第二层 · 纵深防御过滤**

遍历全文，剥离所有 `type == "remove"` 的消息、任何含路由工具调用的 AIMessage、以及路由工具的 ToolMessage。这层是兜底，防止 Redis 中的历史残留。

**第三层 · 滑动窗口截断**

```python
truncated_messages = trim_messages(
  safe_messages,
  max_tokens=self.slide_window,   # agent_settings.slide_window，默认 10
  strategy="last",                # 保留最新
  token_counter=len,              # 传 len 时 max_tokens 语义为「消息条数」
  allow_partial=False,            # 不允许拆散 AIMessage/ToolMessage 配对
  end_on=("human", "tool"),
  start_on="human",               # 截断后首条必须是用户发言
)
```

`start_on="human"` 是精髓：自动丢弃截断后残留的孤立 ToolMessage/AIMessage，避免向 LLM 发送**没有对应 tool_call 的 ToolMessage**（这会被 OpenAI 兼容接口直接拒绝）。

### 4.6 回复流与产物提取

`stream_chat` 以 `stream_mode="updates"` 消费图的事件流，逐节点处理：

**Trace 提示**（`enableTrace=true` 时）：工具节点推送该工具的中文友好描述（来自 `self.tool_descriptions` 映射，如 `vector_search_icbc_mall` → 「正在工行商城为您搜寻最优惠的商品...」）；业务房间若产生了 tool_calls，推送「正在为您核实中...」。

**最终回复**：仅当业务房间的最后一条消息**有内容且无 tool_calls**（即真正面向用户的收尾发言）时才推送，这保证了一个请求只产生一条最终答案。

**`[PRODUCTS_JSON]` 协议**：商品房间可在回复文本中内嵌隐藏 JSON 块：

```
[PRODUCTS_JSON]{"products": [...]}[/PRODUCTS_JSON]
```

`stream_chat` 用正则提取该块 → 解析出 `products` 数组 → 从展示文本中**剥离**该块 → 在终态 `end` 消息中作为 `products` 卡片下发。前端据此渲染微信小店商品组件。

**终态**：无论是否有最终回复，都以 `status: "end"` 收尾；若本次没有任何业务回复，则兜底「好的，请问还有什么我可以帮您的？」。

### 4.7 开关与灰度

房间行为受三组配置开关控制，均定义在 `resource.yaml` 的 `default_values`：

| 开关 | 位置 | 关闭效果 |
|------|------|---------|
| `egg_voucher_config.enabled` | `RedemptionAgent.__init__` | 切换 points 房 prompt key（含/不含发券话术），且**不把 `issue_custom_voucher` 注册进工具池**，杜绝发券调用 |
| `coupon_redeem_config.enabled` | `points_exchange_node_fn` | 立减金兑换整体下架：**不调用 LLM**，硬编码返回「暂不支持」，并把 `current_agent` 置回 `router` |
| `ad_push_config.enabled` | `RedemptionAgent` | 广告文案推送 |

`coupon_redeem` 关闭时的处理值得注意——它刻意把 `current_agent` 重置为 `router`，否则下一轮会被入口快车道（§4.4）再次拽回 points 房，形成死循环。

---

## 5. 工具层设计

### 5.1 工具清单

所有工具均为 LangChain `@tool`，使用严格 Pydantic `args_schema`，且 `extra = "forbid"`——**禁止大模型传入 schema 之外的任何参数**。

| 工具 | 所属房间 | 依赖 | 说明 |
|------|---------|------|------|
| `vector_search_icbc_mall` | goods | ChromaDB | 工行 i豆商城商品向量检索 |
| `vector_search_wechat_products` | goods | ChromaDB | 微信小店商品向量检索 |
| `get_points_activities` | customer_service / goods | —（硬编码） | 攒豆攻略 |
| `query_icbc_voucher_rules` | customer_service | ChromaDB | 立减金规则 FAQ 检索 |
| `query_voucher_order_status` | customer_service | 拼乐 API | 订单状态查询 |
| `create_voucher_order` | points | 拼乐 API | 创建兑换订单 |
| `issue_custom_voucher` | points | 拼乐 API | 发放鸡蛋代金券 |
| `route_*` ×4 | 全部 | — | 路由令牌（见 §4.3） |

> **注意**：`query_egg_info`（鸡蛋知识库检索）已在 `llm_tools.py` 中实现，但**未注册进任何房间的工具池**，`redemption_agent.py` 既未导入也未绑定，因此当前是死代码。配套的 `CUSOTMER_SERVICE_AGENT_EGG_PROMPT` 同样无引用（见 §12）。

**工具返回契约**：业务工具统一返回 JSON 字符串，采用 `{code, message[, data]}` 信封——`code == 0` 表示成功，`1` 表示业务失败，`2` 表示下单通道系统异常；路由工具返回裸的大写令牌字符串；向量检索工具返回 Python list/dict，由 LangChain 序列化进 `ToolMessage`。

### 5.2 用户身份注入

工具需要知道「当前是谁」，但**不希望让大模型生成 openid**（幻觉风险）。方案是使用 LangChain 的 `InjectedToolArg` + `ToolRuntime`：

```python
async def create_voucher_order(
    total_points: int, vouchers: List[VoucherItem],
    runtime: Annotated[ToolRuntime, InjectedToolArg]) -> str:
  ...
  user_id = runtime.config.get("configurable").get("thread_id")
```

`runtime` 参数由框架从 `config.configurable` 注入，**不出现在给大模型的 schema 中**（`IssueCustomVoucherSchema` 因此只有一个不可见字段）。这是把「会话身份」安全传递给工具的标准做法。

> **注意**：项目**当前使用的是 `ToolRuntime` + `InjectedToolArg`** 而非 `Annotated[dict, InjectedState]`。CLAUDE.md 中描述的 `InjectedState` 模式对应的是早期的 `core/abc_llm_tools.py`（已废弃，未纳入版本控制），现存代码中已无任何 `InjectedState` 引用。

### 5.3 立减金兑换风控红线

`create_voucher_order` 在**调用下游接口之前**实施完整的本地审计，任何违规都返回 `code: 1` 并携带引导文案，**不发起网络请求**。这是硬性业务约束：

| 红线 | 规则 | 违规文案要点 |
|------|------|-------------|
| 面额白名单 | 仅允许 `1 / 10 / 100` 元 | 提示检测到不支持的面额 |
| 1 元合并 | 1 元张数 **≥ 10** 违规 | 「每 10 张 1 元必须合并为 1 张 10 元」 |
| 10 元合并 | 10 元张数 **≥ 10** 违规 | 「每 10 张 10 元必须合并为 1 张 100 元」 |
| 总额上限 | 总金额 **> 5000 元** 违规 | 「超过最大风控限制 5000 元」 |
| 批次上限 | 同 `(面额, 卡类型)` **> 60 张** 违规 | 「单个批次不能超过 60 张」 |

注意「批次」的定义是 `(amount, card_type)` 二元组——**信用卡与借记卡分别计数**。

**红线判定用 `>=` 而文案说「不超过 9 张」**：代码对 1 元/10 元采用 `>= 10` 拒绝，等价于「最多 9 张」，与注释/文案一致，非 off-by-one。

卡类型在本地完成中英映射后传下游：`{"信用卡": "credit", "借记卡": "debit"}`。

**信任边界**：红线**强制**在工具内由代码执行，但**方案的生成**（面额拆分、豆/元换算）交给大模型。若模型给出违规方案，工具拒绝并返回 `code: 1`，由提示词引导其重新计算。注意 `total_points` 与券面额之间**没有代码级一致性校验**，该一致性完全依赖提示词约束。

### 5.4 下游接口

业务调用统一走 `VoucherOrder`（`SingletonMeta` 单例），使用 `httpx.AsyncClient` 异步请求：

- **创建订单** `POST /api/coupon/order/create`，payload 为 `{openid, total_points, coupons}`（内部字段名与工具参数名不同，在此完成映射），超时 30s，直接返回 `response.text`。**此路径不签名**。
- **查询状态** `GET /api/coupon/order/status`，需签名：`sign = md5(salt + order_code)`，salt 来自 `config.get_voucher_order_salt()`。**签名逻辑对 LLM 完全透明**。
- **发放鸡蛋券** `GET /jifen/distri/13/send?openid=...&code=P901`，超时 10s。

**签名是不对称的**：仅「查询状态」需要 `sign`，「创建订单」不需要——这是下游接口的既有约定，非本项目设计选择。

---

## 6. 数据层设计

### 6.1 Redis — 会话持久化

自研 `SimpleRedisSaver`（继承 LangGraph `BaseCheckpointSaver`）实现异步 Checkpointer：

- **存储结构**：每个 `thread_id` 一个 Hash，`field = checkpoint_id`，另有 `__latest__` 指向最新快照
- **序列化**：`CrossCompatibleSerializer` 先用 `ormsgpack`，失败回退 JSON；递归把 Pydantic 模型/dict 转为可序列化结构
- **TTL**：`redis.msg_ttl_in_seconds`（默认 7200s），每次写入 `expire` 续期
- **同步接口**：`get_tuple` / `put` / `list` 等同步方法直接 `raise NotImplementedError("Use aput instead")`，**这是刻意的 API 契约声明**，引导调用方使用异步版本——图以 `checkpointer=` 编译后，LangGraph 会走 `aput`/`aget_tuple`/`alist`/`aput_writes` 异步路径

`thread_id` 即用户的 `userCode`，因此**会话上下文天然按用户隔离**。

### 6.2 ChromaDB — 向量库

`ICBCVectorDB`（`SingletonMeta` 单例）封装 ChromaDB `PersistentClient`，路径 `./icbc_vector_db`。

**Collections**：

| Collection | 内容 | 消费方 |
|-----------|------|--------|
| `icbc_products` | i豆商城商品 | `vector_search_icbc_mall` |
| `icbc_standing_vouchers` | 立减金规则 FAQ | `query_icbc_voucher_rules` |
| `yifengyuan_egg_knowledge` | 鸡蛋知识库 | （暂无——`query_egg_info` 未接线，见 §12） |
| `wechat_talent_products` | 微信小店商品 | `vector_search_wechat_products` |

**Embedding**：DashScope `text-embedding-v3`，key 来自 `config.get_qwen_api_key()`。

**异步包装**：`asearch_*` 系列用 `asyncio.to_thread` 包装同步方法，避免阻塞 event loop。

**相似度标注**：`query_icbc_voucher_rules` 等工具会把检索距离翻译为中文可信度标签后再交给大模型——距离 `< 0.4` 标记「高度相关」，`< 0.6` 标记「相关」，否则标记「参考信息」，并在提示中要求「所有内容距离均大于 0.6 时」不得强行作答（`core/llm_tools.py:405,421`）。

### 6.3 蓝绿切换与跨进程版本号

微信小店商品库需要**定期全量重建**，但不能让线上查询读到半成品。方案是蓝绿切换：

```
start_rebuild_session()   → 建临时集合 wechat_tmp_{ts}
append_to_rebuild_session() → 分批写入（每批算完向量即释放）
finalize_rebuild_session()  → 原子改名：
    wechat_talent_products      → wechat_talent_products_old
    wechat_tmp_{ts}             → wechat_talent_products
```

**跨进程一致性**是难点：Worker 进程持有的是各自 Python 对象里的旧 collection 引用。解决方案是 `multiprocessing.Value` 共享版本号：

```python
class ICBCVectorDB(metaclass=SingletonMeta):
  _shared_version = Value('i', 0)     # 类属性，父进程分配

  # finalize 时递增
  with self.shared_version.get_lock():
    self.shared_version.value += 1
```

各进程在访问前比对 `shared_version` 与本地 `local_version`，不一致则重新 `get_collection` 刷新引用。

**这也解释了为什么必须在父进程调用一次 `ICBCVectorDB()`**（`point_server.py:425`）：只有在 fork/spawn 之前创建该 `Value`，子进程才会共享同一块共享内存；否则各进程 `_shared_version` 相互独立，版本号机制失效。

**自愈逻辑**：初始化时若发现正式表丢失但 `_old` 存在（说明切换中途崩溃），自动把 `_old` 改回正式名；同时清理残留的 `wechat_tmp_*` 集合。

### 6.4 SQLite — 推荐商品

`SQLiteGoodsRepository`（`BaseGoodsRepository` 的 SQLite 实现）存储推荐商品表 `goods(product_id, description, link, category, extra)`。

关键是多进程安全：开启 **WAL 模式**（`PRAGMA journal_mode=WAL`）允许多进程读写并发、读写互不阻塞，配合 `PRAGMA synchronous=NORMAL` 降低同步开销。路径统一转为项目根目录下的绝对路径，避免工作目录漂移。

---

## 7. 单例后台服务 — 微信小店商品同步

**仅 Master 进程运行**（`start_single_services`）。

### 7.1 调度策略

`sync_task` 是一个 `while True` 调度循环，支持**定时**与**手动**双触发：

```python
is_manual = sync_event.is_set()
in_window  = (start_hour <= current_hour < end_hour)
days_passed = (time.time() - last_sync_timestamp) / (24*3600)
already_done_today = (last_sync_date == today)

should_run = is_manual or (in_window and days_passed >= period_days and not already_done_today)
```

- **定时**：需同时满足「在时间窗口内」「距上次 ≥ N 天」「今日未执行」
- **手动**：`sync_event.set()` 唤醒，**忽略窗口与周期限制**

等待采用 `asyncio.wait_for(sync_event.wait(), timeout=check_interval_sec)`——既能被手动事件立即唤醒，又能按 `check_interval` 轮询检查时间条件。

外部触发经由 token server 的 `wechatDBSync` 命令调用 `trigger_immediate_sync()`。

### 7.2 增量复用与流式构建

`sync_once` 的设计重点是在**长耗时任务中控制内存**：

1. 分页拉取商品列表（`last_buffer` 游标）
2. **增量复用**：按 `product_id` 查旧库，若标题未变则直接复用已有描述，**跳过 LLM**；标题变了才重新生成
3. **批量 LLM 增强**：待处理队列攒够 `vector_db_batch_llm_size`（默认 5）就调一次 LLM
4. **分批落盘**：写队列攒够 50 条就 `to_thread(append_to_rebuild_session)` 写入临时集合

这种「队列 + 阈值」的流式处理避免了把所有商品和向量同时驻留内存。

**失败清理**：`sync_once` 异常时删除临时集合，且先检查存在性再删（避免二次崩溃）。由于正式表在 `finalize` 前从未被触碰，**同步失败不影响线上查询**——这是蓝绿切换的核心收益。

---

## 8. 配置体系

### 8.1 两类配置文件

| 文件 | 读取方式 | 内容 |
|------|---------|------|
| `data/config.ini` | `configparser`，**导入时读一次** | 服务端口、Redis、TLS、日志、兑换率、salt |
| `data/resource.yaml` | `yaml.safe_load`，**导入时读一次** | 提示词、模型定义、开关、同步策略 |

`data/config.ini` 的每个 getter 都遵循同一模式：

```python
def get_server_port():
  return config.getint('server','port', fallback=int(os.environ.get('SERVER_PORT', 443)))
```

即 **INI 优先 → 环境变量兜底 → 硬编码默认值**。

`config/resource.py` 在导入时即加载 YAML，失败则 `os._exit(1)` —— **快速失败**，避免带着空配置启动。

### 8.2 提示词模板

提示词存于 `resource.yaml` 的 `default_values`，经 `_replace_prompt_variables` 渲染：

```python
rendered_prompt = prompt_template\
  .replace("{CUSTOMER_SERVICE_AGENT_CAPABILITY}", capability_cs)\
  .replace("{POINTS_EXCHAGNGE_AGENT_CAPABILITY}", capability_points)\
  .replace("{GOODS_EXCHAGE_AGENT_CAPABILITY}", capability_goods)\
  .replace("{{voucher_rate}}", voucher_rate_str)
```

**刻意使用 `str.replace` 而非 `str.format`**：提示词中含 LaTeX 数学公式（如 `$P_{icbc}$`），`format` 会因大括号报错。

其中已注册的原子能力占位符有三个层级：原子能力串 → 拦截规则 → 整体拼装。

### 8.3 模型配置

```yaml
active_model: deepseek
models:
  deepseek-flash: {type: deepseek, model_name: deepseek-v4-flash, base_url, api_key: DEEPSEEK_API_KEY, temperature: 0, extra_body: {thinking: {type: disabled}}}
  deepseek-pro:   {...}
default_values:
  agent_models: {router: deepseek-flash, customer_service: deepseek-flash, points_exchange: deepseek-pro, goods_exchange: deepseek-pro}
```

`api_key` 字段存的是**环境变量名**，`model_factory` 通过 `os.getenv(raw_key, raw_key)` 解析真实密钥——**真实密钥永不落盘**。

`extra_body` 等非一级参数以 `**standard_kwargs` 展开传入 `ChatOpenAI`。`thinking: disabled` 用于关闭 DeepSeek 的思维链输出。

---

## 9. 可观测性

### 9.1 Metrics（OpenTelemetry）

`redemption_agent.py` 定义四个进程级指标：

| 指标 | 类型 | 标签 |
|------|------|------|
| `agent_chat_requests_total` | Counter | `status` |
| `agent_chat_request_duration_seconds` | Histogram | `status` |
| `agent_node_execution_total` | Counter | `agent_name` |
| `tool_node_execution_total` | Counter | `tool_name` |

`stream_chat` 在成功与异常分支分别记录耗时与计数，因此**可以按 status 维度计算失败率**。

### 9.2 日志

- loguru 统一封装（`from loguru import logger as _log`）
- 首次 `logger.remove()` 清除默认 stderr handler
- 输出目标由 `logging.destination`（`console` / `file` / 逗号分隔多选）决定
- 文件日志 `rotation` 按配置大小轮转
- **格式含 PID** —— 多进程下这是区分进程的唯一手段
- **特殊过滤器**：`missing_product` 记录额外字段被单独路由到 `data/wechat_missing_products.jsonl`（纯 JSON 行），与业务日志分流

---

## 10. 部署形态

### 10.1 网络拓扑

```
nginx                             internal
api.ninenode.com:9443              8446 (token port)
api.ninenode.com:443               8445 (msg port)
www.node09.cn:9443                 8445 (msg port)
```

外部 HTTPS/WSS 由 Nginx 终止并卸载 SSL，内部为明文；token 服务走独立 mTLS 通道。

### 10.2 运行依赖

- **Redis 必需** —— 会话持久化与 token 存储
- **ChromaDB 数据** `icbc_vector_db/` 必须存在
- **`data/resource.yaml`** 决定全部提示词与模型配置

### 10.3 离线构建工具（`tools_cmd.py`）

| 命令 | 作用 |
|------|------|
| `bvd <file>` | 构建立减金规则向量库 |
| `bmd <file>` | 构建工行商城商品向量库 |
| `egg_build` | 构建鸡蛋知识库 |
| `add_sqlite` / `query_sqlite` | 填充/查询推荐商品表 |

微信小店商品库无需离线构建，由 Master 进程的同步任务自动维护。

---

## 11. 关键设计取舍

| 决策 | 收益 | 代价 |
|------|------|------|
| **端口抢占选主** | 无中心协调，无额外组件 | 依赖错误的 `errno` 判定；需启动延迟规避竞态 |
| **路由工具 + 条件边** | 房间切换完全确定，不解析自由文本 | 每个房间需注册路由工具，占用 schema 空间 |
| **入口快车道（留在原房间）** | 省一次 LLM 调用，多轮体验连续 | 房间需主动 `route_back` 才能重判，依赖提示词质量 |
| **三层消息清洗** | 彻底隔离路由临时消息 | 每轮遍历全量历史，O(n) 开销 |
| **蓝绿切换** | 重建期间线上零影响，失败可自愈 | 需 `multiprocessing.Value` 跨进程同步版本 |
| **自研 RedisSaver** | 可定制 TTL、序列化、跨版本兼容 | 需自行维护 LangGraph Checkpointer 契约 |
| **两个配置文件分流** | 提示词（频繁改）与运行时参数（很少改）解耦 | 配置项分散，需明确「该放哪个文件」 |

---

## 12. 已知技术债

> 以下为通读代码时发现的、**与设计意图不符或已失效**之处，供后续排期参考。

1. **`tester.py` 已失效** —— 调用 `agent.chat_with_trace()`，但该方法在异步化改造（commit `d449067`）中已删除，现由 `stream_chat` 取代。**当前无可运行的自动化回归测试**。
2. **`query_egg_info` 工具与 `CUSOTMER_SERVICE_AGENT_EGG_PROMPT` 均为死代码** —— 前者已在 `llm_tools.py` 实现、后者已在 `resource.yaml` 定义，但**都未被任何代码引用**：`redemption_agent.py` 未导入 `query_egg_info`，也未将其加入任何房间的工具池；`CUSOTMER_SERVICE_AGENT_EGG_PROMPT` 在全部 `.py` 中零引用。鸡蛋知识库 collection（`yifengyuan_egg_knowledge`）因此当前无消费方。
3. **`ishopping.py` 与主流程脱节** —— 为早期同步 REPL，现已可构造 `SimpleRedisSaver` 正常运行，但不反映新的 WebSocket 架构。
4. **`attach_extra_msg`（下单后自动发鸡蛋券）已被注释禁用** —— `stream_chat` 中相关调用被注释，实际生效的是「下单成功直接追加发券文案」的路径。当前自动发券逻辑存在两套实现，仅一套生效。
5. **CLAUDE.md 的身份注入说明已过时** —— 文档描述 `Annotated[dict, InjectedState]`，但现存代码已全部改用 `ToolRuntime` + `InjectedToolArg`，`InjectedState` 零引用。
6. **`simple_redis_saver.adelete` 未被调用** —— 会话 Redis 数据目前仅靠 TTL 过期回收，无主动清除入口。
7. **`jd_*` 配置项无消费者** —— `config.get_jd_*` 与 `resource.yaml` 的京东相关配置当前无调用方（京东比价分支尚未接入主流程）。
8. **环境变量缺失时静默降级** —— `model_factory` 用 `os.getenv(raw_key, raw_key)`，若环境变量未设置，会把变量名本身当作 API Key 使用，直到首次调用才报错，缺少启动期校验。
9. **`enqueue=True` 的延迟写入** —— loguru 多进程文件写入依赖队列，进程异常退出可能丢尾部日志。
