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
from fastapi import WebSocket, WebSocketDisconnect, status

from core.simple_redis_saver import SimpleRedisSaver
import config.config as config
import core.token as token_module
from core.redemption_agent import RedemptionAgent
import wechat.products_db_builder as pdb

# make sure icbc vector db is safe in multiple processes
from core.icbc_db import ICBCVectorDB

# 禁用 LangChain 匿名遥测
os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

state = {}

async def start_single_services():
  """
  启动一些只需要一个进程中运行的的服务，如sync 带货助手的货物等
  """
  state["wechat_talent_sync_task_handle"] = asyncio.create_task(pdb.sync_task())

async def token_management_server():
  """
  基于异步 I/O 的 mTLS Token 管理服务
  """
  
  # 由于 Token Server 在windows上可能会和主服务竞争底层网络资源导致错误，增加启动延迟让主服务先抢占端口，Token Server 再来尝试绑定
  # （虽然这两个server是不同的端口，但是一些网络资源可能导致冲突）
  await asyncio.sleep(1.5)

  try:
    host = config.get_tokenserver_host()
    port = config.get_token_server_port()
   
    # mTLS SSL 上下文配置 (保持不变)
    ssl_context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    #ssl_context.load_cert_chain("data/token_server.crt", "data/token_server.key")
    ssl_context.load_cert_chain(config.get_token_certificate_chain_file(), config.get_token_private_key_file())
    ca_path = config.get_token_ca_cert_file()
    if not (ca_path and os.path.exists(ca_path)):
      _log.error("未找到 mTLS CA 证书，客户端认证将被跳过，存在安全风险！请确保 {} 文件存在", ca_path)
      return
    
    ssl_context.load_verify_locations(cafile=ca_path)
    ssl_context.verify_mode = ssl.CERT_REQUIRED
  except Exception as e:
    _log.error(f"❌ Token Server 启动阶段崩溃: {e}", exc_info=True)
    return

  tm: token_module.TokenManager = state.get("token_manager")

  async def handle_client(reader, writer):
    try:
      quit = False
      while True:
        line = await reader.readline()
        if not line: break
        
        request = json.loads(line.decode('utf-8'))
        cmd = request.get("cmd")
        
        if cmd == "getNewToken":
          response = await tm.get_new_token() # 调用异步方法
        elif cmd == "cancelToken":
          response = await tm.cancel_token(request.get("token"))
        elif cmd == "wechatDBSync":
          pdb.trigger_immediate_sync()
          response = {"status": "success"}
        else:
          response = {"status": "fail", "errorMsg": "Unknown Cmd"}
          quit = True
          
        writer.write((json.dumps(response) + "\n").encode('utf-8'))
        await writer.drain()
        if quit: break
        
    except Exception as e:
      _log.debug("Token Server 连接关闭: {}", e)
    finally:
      writer.close()
      try:
        await writer.wait_closed()
      except: pass

  try:
    # 尝试启动监听
    server = await asyncio.start_server(handle_client, host, port, ssl=ssl_context)
    state["is_master_node"] = True
  except OSError as e:
    # 10048 是 Windows 端口占用，98 是 Linux 端口占用
    if e.errno in (10048, 98):
      state["is_master_node"] = False
      _log.info("PID: {} | Token Server 端口 {} 已被其他 Worker 占用，本进程仅处理业务逻辑", os.getpid(), port)
      return # 抢不到端口直接退出函数，该 Task 结束
    raise e # 其他类型的网络错误仍然抛出

  _log.info("PID: {} | 🚀 成功抢占端口 {}，Token Server 启动完成", os.getpid(), port)
  
  async with server:
    await server.serve_forever()
    

async def init_singleton_classes():
  """
  this function to init singleton class in multiple processes. 
  to avoid some multiple processes issues.
  """
  ICBCVectorDB()


async def lifespan_init():
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
  state["shared_redis"] = shared_redis
  state["redis_pool"] = redis_pool

  loop = asyncio.get_running_loop()
  executor = ThreadPoolExecutor(
    max_workers=max_workers,
    thread_name_prefix=f"Worker_{os.getpid()}_Pool"
  )
  loop.set_default_executor(executor)
  state["loop"] = loop
  state["executor"] = executor
  
  # 2. 日志初始化
  import log.logger as logger_module
  logger_module.setup_logger()
  _log.info("Worker 子进程启动，PID: {} | 线程池大小: {}", os.getpid(), max_workers)
  
  # 2. 初始化 Token 管理器
  tm = token_module.TokenManager(ttl=config.get_token_ttl_in_seconds())
  tm.set_client(shared_redis)
  state["token_manager"] = tm
  state["token_task"] = asyncio.create_task(token_management_server())
  
  # 3. 实例化 Agent
  saver = SimpleRedisSaver(redis_client=shared_redis,ttl=config.get_redis_msg_ttl_in_seconds())
  state["agent"] = RedemptionAgent(saver=saver)
  
  # init singleton classes
  await init_singleton_classes()
  
  #启动单例服务
  while True:
    if "is_master_node" in state:
      if state["is_master_node"]:
        _log.info(f"当前进程为 Master Node，单例服务在本进程pid {os.getpid()} 中启动。")
        await start_single_services() # 启动单实例服务
      else:
        _log.info("当前节点为 Worker Node，不启动任何单例服务。")
        
      break
    else:
      await asyncio.sleep(1) 
  
@asynccontextmanager
async def lifespan(app: FastAPI):
  await lifespan_init()
  
  yield # --- 运行中 ---
  
  await lifespan_done()

async def lifespan_done():
  # 4. 资源回收
  _log.info("进程 PID:{} 正在清理资源...", os.getpid())
  if "wechat_talent_sync_task_handle" in state:
    task = state["wechat_talent_sync_task_handle"]
    _log.info("正在停止同步定时任务...")
    task.cancel()
    try:
      await asyncio.wait_for(task, timeout=5.0)
    except (asyncio.CancelledError, asyncio.TimeoutError):
      pass
    
  # A. 先取消 Token Server 任务并等待它结束
  token_task = state["token_task"]
  if not token_task.done():
    token_task.cancel()
    try: await asyncio.wait_for(token_task, timeout=2.0)
    except: pass

  # B. 清理 Agent 资源
  if "agent" in state:
    try:
      await asyncio.wait_for(state["agent"].close_resource(), timeout=3.0)
    except Exception as e:
      _log.warning("Agent 清理异常: {}", e)

  # C. 显式关闭 Redis (顺序：先 Client 后 Pool)
  try:
    shared_redis = state["shared_redis"]
    redis_pool = state["redis_pool"]
    await shared_redis.aclose() # 注意异步库建议用 aclose()
    await redis_pool.disconnect()
    _log.info("Redis 共享连接池已断开")
  except Exception: pass

  # D. 核心补丁：显式关闭自定义线程池
  # 如果不关闭这个，进程往往会挂在“Waiting for child process”
  try:
    executor = state["executor"]
    loop = state["loop"]
    _log.info("正在关闭线程池...")
    executor.shutdown(wait=False,cancel_futures=True) # 不再等待未完成的线程，强制收工
    
    current_loop = asyncio.get_running_loop()
    tasks = [t for t in asyncio.all_tasks(current_loop) if t is not asyncio.current_task()]
    for t in tasks:
      t.cancel()
    
    # 给一点点时间让 cancel 逻辑跑完
    if tasks:
      await asyncio.gather(*tasks, return_exceptions=True)
        
    loop.set_default_executor(None)
  except Exception: pass

  state.clear()

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
          await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
          _log.warning("由于 Token 无效，已强制断开 WebSocket 连接")
          break
      
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
  
  # 这确保了 ICBCVectorDB._shared_version 在父进程分配内存
  ICBCVectorDB()

  _log.info("--- 工银 i 豆精算管家启动 ---")
  _log.info("运行模式: {} | 监听: {}:{}", mode, host, port)

  uvicorn.run(**uvicorn_kwargs)
  
  
  