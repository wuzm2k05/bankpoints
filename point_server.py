# 2 个空格对齐
import os
import asyncio
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from loguru import logger as _log

import config.config as config
from core.redemption_agent import RedemptionAgent

# 禁用 LangChain 匿名遥测
os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

state = {}

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
  
  # 3. 实例化 Agent
  state["agent"] = RedemptionAgent()
  
  yield # --- 运行中 ---

  # 4. 资源回收
  _log.info("进程 PID:{} 正在清理资源...", os.getpid())
  if "agent" in state:
    try:
      # 给资源清理设置一个硬性超时，防止无限等待
      await asyncio.wait_for(state["agent"].close_resource(), timeout=5.0)
      #await state["agent"].close_resource()
    except asyncio.TimeoutError:
      _log.warning("清理资源超时，强制退出")
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

  try:
    while True:
      data = await websocket.receive_json()
      _log.debug("收到消息: {}", data)
      
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