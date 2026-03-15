# 2 个空格对齐
import os
import asyncio,json,ssl
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from loguru import logger as _log
from redis.asyncio import Redis, ConnectionPool

from core.simple_redis_saver import SimpleRedisSaver
import config.config as config
import core.token as token_module
from core.redemption_agent import RedemptionAgent

# 禁用 LangChain 匿名遥测
os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

state = {}

async def token_management_server():
  """
  基于异步 I/O 的 mTLS Token 管理服务
  """
  host = config.get_tokenserver_host()
  port = config.get_token_server_port()
  
  # mTLS SSL 上下文配置 (保持不变)
  ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
  ssl_context.load_cert_chain(config.get_certificate_chain_file(), config.get_private_key_file())
  ca_path = config.get_token_ca_cert_file()
  if ca_path and os.path.exists(ca_path):
    ssl_context.load_verify_locations(ca_file=ca_path)
    ssl_context.verify_mode = ssl.CERT_REQUIRED

  tm: token_module.TokenManager = state.get("token_manager")

  async def handle_client(reader, writer):
    try:
      while True:
        line = await reader.readline()
        if not line: break
        
        request = json.loads(line.decode('utf-8'))
        cmd = request.get("cmd")
        
        if cmd == "getNewToken":
          response = await tm.get_new_token() # 调用异步方法
        elif cmd == "cancelToken":
          response = await tm.cancel_token(request.get("token"))
        else:
          response = {"status": "fail", "errorMsg": "Unknown Cmd"}
          
        writer.write((json.dumps(response) + "\n").encode('utf-8'))
        await writer.drain()
    except Exception as e:
      _log.debug("Token Server 连接关闭: {}", e)
    finally:
      writer.close()
      await writer.wait_closed()

  server = await asyncio.start_server(handle_client, host, port, ssl=ssl_context)
  _log.info("异步 mTLS Token Server 启动于端口 {}", port)
  async with server:
    await server.serve_forever()
    
@asynccontextmanager
async def lifespan(app: FastAPI):
  """
  初始化子进程资源
  """
  # 1. 线程池配置
  max_workers = config.get_max_thread_workers()
  if int(max_workers) <= 0:
    cpu_count = multiprocessing.cpu_count()
    max_workers = max(32, min(cpu_count * 5, 200))
  
  # 将连接数设为线程池 worker 数的两倍左右比较稳妥
  redis_pool = ConnectionPool(
    host=config.get_token_redis_host(),
    port=config.get_token_redis_port(),
    db=0,
    max_connections=config.get_max_thread_workers() * 2 or 100,
    decode_responses=False # 注意：Saver 可能需要 bytes，TokenManager 自行处理字符串
  )
  shared_redis = Redis(connection_pool=redis_pool)

  loop = asyncio.get_running_loop()
  executor = ThreadPoolExecutor(
    max_workers=max_workers,
    thread_name_prefix=f"Worker_{os.getpid()}_Pool"
  )
  loop.set_default_executor(executor)
  
  # 2. 日志初始化
  import log.logger as logger_module
  logger_module.setup_logger()
  _log.info("Worker 子进程启动，PID: {} | 线程池大小: {}", os.getpid(), max_workers)
  
  # 2. 初始化 Token 管理器
  tm = token_module.TokenManager(ttl=config.get_token_ttl_in_seconds())
  tm.set_client(shared_redis)
  state["token_manager"] = tm
  token_task = asyncio.create_task(token_management_server())
  
  # 3. 实例化 Agent
  saver = SimpleRedisSaver(redis_client=shared_redis,ttl=config.get_redis_msg_ttl_in_seconds())
  state["agent"] = RedemptionAgent(saver=saver)
  
  yield # --- 运行中 ---

  # 4. 资源回收
  _log.info("进程 PID:{} 正在清理资源...", os.getpid())
  token_task.cancel()
  
  if "agent" in state:
    try:
      # 给资源清理设置一个硬性超时，防止无限等待
      await asyncio.wait_for(state["agent"].close_resource(), timeout=5.0)
      #await state["agent"].close_resource()
    except asyncio.TimeoutError:
      _log.warning("清理资源超时，强制退出")
  state.clear()
  
  try:
    await shared_redis.close() # 关闭客户端实例
    await redis_pool.disconnect() # 彻底释放连接池中的所有 TCP 连接
    _log.info("Redis 共享连接池已断开")
  except Exception as e:
    _log.error("断开 Redis 连接池失败: {}", e)

app = FastAPI(lifespan=lifespan)

@app.websocket("/v1/chat")
async def websocket_endpoint(websocket: WebSocket):
  """
  WebSocket 入口，支持 Pipeline 并发任务
  """
  await websocket.accept()
  active_tasks = set()
  agent: RedemptionAgent = state.get("agent")
  user_id = "unknown"

  try:
    while True:
      data = await websocket.receive_json()
      _log.debug("收到消息: {}", data)
      
      if config.get_token_enabled():
        token_str = data.get("token")
        if not token_str or not await state["token_manager"].verify_token(token_str):
          await websocket.send_json({
            "status": "fail",
            "errorCode": "INVALID_TOKEN",
            "errorMsg": "Token 无效或已过期"
          })
          continue
      
      msg_type = data.get("type", "chat")
      user_id = data.get("userCode")
      seq = data.get("seq")
      user_input = data.get("prompt")

      # 根据协议分发
      if msg_type == "loadUserHistory":
        task = asyncio.create_task(
          handle_load_history(user_id, seq, websocket)
        )
      else:
        enableTrace = data.get("enableTrace", False)
        # chat 逻辑由 agent.stream_chat 处理，内部需遵循 status: success/end 逻辑
        task = asyncio.create_task(
          agent.stream_chat(user_input, user_id, seq, websocket,enableTrace)
        )

      active_tasks.add(task)
      task.add_done_callback(active_tasks.discard)

  except WebSocketDisconnect:
    _log.info("用户 {} 连接已断开", user_id if 'user_id' in locals() else "未知")
  except Exception as e:
    _log.error("WebSocket 异常: {}", e)
  finally:
    for task in active_tasks:
      if not task.done(): task.cancel()

async def handle_load_history(user_id: str, seq: str, websocket: WebSocket):
  """
  严格按照设计文档返回历史记录
  """
  agent: RedemptionAgent = state.get("agent")
  try:
    history = await agent.get_history(user_id)
    # 按照文档：字段名为 history，且单次返回 status 为 end
    await websocket.send_json({
      "seq": seq,
      "type": "loadUserHistory",
      "userCode": user_id,
      "history": history,
      "status": "end" 
    })
  except Exception as e:
    _log.error("获取历史记录失败: {}", e)
    await websocket.send_json({
      "seq": seq,
      "type": "loadUserHistory",
      "status": "fail",
      "errorCode": "500",
      "errorMsg": str(e)
    })

if __name__ == "__main__":
  host = config.get_server_host()
  port = config.get_server_port()
  conf_workers = config.get_server_process_num()
  final_workers = conf_workers if conf_workers > 0 else multiprocessing.cpu_count()

  # 基础启动参数
  uvicorn_kwargs = {
    "app": "point_server:app",
    "host": host,
    "port": port,
    "workers": final_workers,
    "loop": "asyncio",
    "log_level": "info",
  }

  # --- SSL/WSS 核心配置 ---
  cert_path = config.get_certificate_chain_file()
  key_path = config.get_private_key_file()
  
  # 只有当两个文件都存在时，才启用 SSL 
  if cert_path and key_path and os.path.exists(cert_path) and os.path.exists(key_path):
    uvicorn_kwargs.update({
      "ssl_keyfile": key_path,
      "ssl_certfile": cert_path,
    })
    mode = "WSS (Secure)"
  else:
    mode = "WS (Plain)"

  _log.info("--- 工银 i 豆精算管家启动 ---")
  _log.info("运行模式: {} | 监听: {}:{}", mode, host, port)

  uvicorn.run(**uvicorn_kwargs)
  
  
  