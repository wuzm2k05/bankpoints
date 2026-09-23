# 工银 i豆精算管家

面向微信小程序的 AI 智能客服系统，引导用户将工行「工银i豆」积分兑换为微信立减金或商品。

系统基于 **LangGraph 多智能体状态机 + FastAPI WebSocket** 构建：用户与 AI 对话，AI 检索工行商城与微信小店商品辅助比价、创建立减金兑换订单、解答 i豆攒取与立减金规则，并在 i豆不足时给出攒豆方案。

---

## 快速开始

### 环境要求

- **Python 3.11+**
- **Redis**（必需，用于会话持久化与 token 存储）
- ChromaDB 向量库数据（`icbc_vector_db/`）
- `data/resource.yaml`（提示词与模型配置，**未纳入版本控制，需自行准备**，可参考 `data/resource_example.yaml`）

### 安装

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows
# source .venv/bin/activate   # Linux / macOS

pip install -r requirements.txt
```

### 配置

两步即可跑起来：

**1. API 密钥 —— 写入 `.env`**

`.env` 已在 `.gitignore` 中，不会被提交。需要的最小集合：

```ini
DEEPSEEK_API_KEY=sk-xxxxxxxx        # 主对话模型
QWEN_API_KEY=sk-xxxxxxxx            # DashScope Embedding（向量检索）
```

**2. 运行时参数 —— `data/config.ini`**

关键项（每项都可用同名环境变量覆盖）：

```ini
[server]
host = 127.0.0.1
port = 8443
process_num = 1          # 0 表示使用 CPU 核心数

[redis]
host = 127.0.0.1
msg_ttl_in_seconds = 36000

[token]
enabled = false          # 是否强制校验 WebSocket token
```

> **密钥永不落盘**：`data/resource.yaml` 的 `api_key` 字段存的是**环境变量名**（如 `DEEPSEEK_API_KEY`），由 `model_factory` 在运行时通过 `os.getenv()` 解析真实密钥。

### 启动

```bash
python point_server.py
```

服务以多进程方式启动。启动日志中会出现：

- **Master Node** —— 抢到 mTLS token 端口的进程，额外运行单例服务（微信小店商品同步）
- **Worker Node** —— 其余进程，仅处理业务请求

访问入口：`ws://<host>:<port>/v1/chat`

---

## 项目结构

```
point_server.py              FastAPI + WebSocket 服务入口、多进程选主
tester.py                    脚本化回归测试（⚠️ 当前失效，见「已知问题」）
local_test.py                交互式 WebSocket 命令行客户端
tools_cmd.py                 离线向量库构建命令入口

core/
  redemption_agent.py        LangGraph 状态机核心（四个房间 + 路由）
  llm_tools.py               LangChain @tool 工具定义（含立减金风控红线）
  voucher_order.py           立减金下单/查询/发券，对接拼乐后端 API
  icbc_db.py                 ChromaDB 向量库封装（蓝绿切换 + 跨进程版本号）
  simple_redis_saver.py      LangGraph Checkpointer（Redis, 异步）
  model_factory.py           LLM 实例工厂（进程内单例）
  token.py                   mTLS token 管理器

config/
  config.py                  config.ini 读取器（INI → 环境变量 → 默认值）
  resource.py                resource.yaml 加载器（导入时加载，失败即退出）

wechat/
  products_db_builder.py     微信小店商品同步服务（仅 Master 进程运行）
  talent_assitant.py         微信带货助手 API 客户端

sqldb/                       SQLite 推荐商品表（WAL 模式）
log/logger.py                loguru 日志配置（按 PID 区分进程）
document/                    设计文档、协议文档、接口文档
tools/                       离线构建脚本
```

---

## 核心概念

### 四个房间（多智能体）

| 房间 | 职责 |
|------|------|
| `router` | 意图识别与分流（强制调用路由工具） |
| `customer_service` | 客服问答：攒豆攻略、立减金规则、订单状态 |
| `points_exchange` | 立减金兑换：创建订单、发放鸡蛋券 |
| `goods_exchange` | 商品导购：商城检索、微信小店检索、比价 |

**路由是确定性的**：房间切换不依赖解析大模型的自由文本，而是由路由工具返回固定令牌（`ROUTE_POINTS` 等），条件边比对令牌跳转。大模型只负责「选哪个工具」，工具到节点的映射由代码固化。

### 多轮对话快车道

默认停留在上一轮的房间，而非每轮重判意图——省一次 LLM 调用，且多轮体验连续。只有当房间主动调用 `route_back_to_router` 时才回到网关重新研判。

### 立减金兑换红线

`create_voucher_order` 在调用下游接口**之前**完成本地审计，任何违规直接返回 `code: 1`，不发起网络请求：

| 红线 | 规则 |
|------|------|
| 面额白名单 | 仅允许 1 / 10 / 100 元 |
| 1 元合并 | 1 元张数 ≥ 10 违规（须合并为 10 元） |
| 10 元合并 | 10 元张数 ≥ 10 违规（须合并为 100 元） |
| 总额上限 | 总金额 > 5000 元违规 |
| 批次上限 | 同「面额 + 卡类型」> 60 张违规 |

### 商品卡片协议

商品房间可在回复文本中内嵌隐藏 JSON 块：

```
[PRODUCTS_JSON]{"products": [...]}[/PRODUCTS_JSON]
```

后端提取该块、从展示文本中剥离，并在终态 `end` 消息中作为 `products` 卡片下发。**该标签格式属于协议约定，修改前请同步前端**。

---

## 开发与调试

### 交互式客户端

```bash
python local_test.py -u <userCode>
```

连接本地服务（默认 `127.0.0.1:8443`，可在文件顶部修改），支持多轮对话与历史拉取。

### 离线构建向量库

```bash
python tools_cmd.py bvd <立减金知识文件>   # 构建立减金规则库
python tools_cmd.py bmd <商城商品文件>     # 构建工行商城商品库
python tools_cmd.py egg_build              # 构建鸡蛋知识库
python tools_cmd.py add_sqlite             # 填充推荐商品表
python tools_cmd.py query_sqlite           # 查询推荐商品表
```

微信小店商品库**无需离线构建**，由 Master 进程的同步任务自动维护（默认每 7 天，在 2:00–4:00 窗口内执行）。

### 手动触发商品同步

通过 mTLS token 通道发送：

```json
{"cmd": "wechatDBSync"}
```

同步采用**蓝绿切换**：先写入临时集合，完成后原子改名上线。因此同步失败**不影响线上查询**。

### 单元测试

```bash
python -m unittest discover -s tests
```

> `tests/` 目前是空壳目录。

---

## 监控与日志

日志由 loguru 统一管理，输出目标通过 `config.ini` 的 `logging.destination` 配置（`console` / `file` / 逗号分隔多选）。

**日志格式包含 PID** —— 多进程部署下这是区分进程的唯一手段。

鸡蛋相关缺失商品记录会被单独路由到 `data/wechat_missing_products.jsonl`（纯 JSON 行），与业务日志分流。

OpenTelemetry 指标：需设置 `OTEL_EXPORTER_OTLP_ENDPOINT` 环境变量启用，未设置则自动跳过。上报四类指标：请求计数、请求耗时、节点执行数、工具执行数。

---

## 配置体系

| 文件 | 内容 | 是否入库 |
|------|------|---------|
| `data/config.ini` | 服务端口、Redis、TLS、日志、兑换率 | ✅ |
| `data/resource.yaml` | 提示词、模型定义、功能开关 | ❌ 需自行准备 |
| `.env` | API 密钥等敏感信息 | ❌ |

**取值优先级**：`config.ini` → 同名环境变量 → 硬编码默认值。

**功能开关**（`resource.yaml` 的 `default_values`）：

| 开关 | 作用 |
|------|------|
| `egg_voucher_config.enabled` | 鸡蛋券发放（关闭时不注册发券工具） |
| `coupon_redeem_config.enabled` | 立减金兑换总开关（关闭后硬编码提示暂不支持） |
| `ad_push_config.enabled` | 广告推送 |

> 提示词模板使用 `str.replace` 而非 `str.format` 渲染，因为提示词中含 LaTeX 公式（如 `$P_{icbc}$`），`format` 会因大括号报错。

---

## 文档

| 文档 | 内容 |
|------|------|
| [document/design.md](document/design.md) | **系统设计文档**（架构、路由机制、数据层、设计取舍） |
| [document/websocket-protocol.md](document/websocket-protocol.md) | 前后端 WebSocket 报文协议 |
| [document/api.md](document/api.md) | 对外接口说明 |
| [document/online.md](document/online.md) | 线上部署与网络拓扑 |

---

## 已知问题

> 上线前请留意以下几项，详见 [design.md §12](document/design.md)。

- **`tester.py` 当前不可用** —— 它调用的 `agent.chat_with_trace()` 在异步化改造中已被删除（现由 `stream_chat` 取代），运行会直接抛 `AttributeError`。**因此目前没有可运行的自动化回归测试**。
- **`query_egg_info` 工具是死代码** —— 已实现但未注册进任何房间，鸡蛋知识库当前无消费方。
- **`ishopping.py` 与主流程脱节** —— 早期同步 REPL，不反映当前 WebSocket 架构。
- **环境变量缺失时静默降级** —— 若 `.env` 中未设置 `DEEPSEEK_API_KEY`，程序会把变量名本身当作密钥使用，直到首次调用才报错，启动期无校验。

---

## 运维提示

**Redis 是硬依赖**：会话上下文与 token 均存于 Redis，服务无法降级运行。

**`resource.yaml` 在导入时加载一次**：修改提示词或模型配置后**必须重启服务**，热更新不生效。

**向量库重建期间服务可用**：蓝绿切换保证切换前线上始终读旧库；但如果重建进程在切换中途崩溃，下次启动时会自动尝试从 `_old` 恢复。
