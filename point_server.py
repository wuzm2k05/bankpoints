# 2 个空格对齐
import os
import asyncio
from concurrent.futures import ThreadPoolExecutor
import multiprocessing
import uvicorn
from contextlib import asynccontextmanager
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from loguru import logger as _log

# 导入你的配置模块 (假设文件名为 config_loader.py)
import config.config as config
from core.redemption_agent import RedemptionAgent

# 禁用 LangChain 匿名遥测
os.environ["ANONYMIZED_TELEMETRY"] = "False"
os.environ["LANGCHAIN_TRACING_V2"] = "false"

# 定义全局变量容器
state = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
  """
  代替旧的 startup/shutdown 事件。
  在每个子进程启动时执行 yield 之前的逻辑。
  """
  
  # 1. 获取配置的线程数
  max_workers = config.get_max_thread_workers()
  
  if int(max_workers) <= 0:
    # 如果没配，则按核心数 5 倍计算，最小不低于 32，最大不超过 200
    cpu_count = multiprocessing.cpu_count()
    max_workers = max(32, min(cpu_count * 5, 200))

  # 2. 设置当前子进程的自定义线程池
  loop = asyncio.get_running_loop()
  executor = ThreadPoolExecutor(
    max_workers=max_workers,
    thread_name_prefix=f"Worker_{os.getpid()}_Pool" # 加上前缀方便 Loguru 打印线程名调试
  )
  loop.set_default_executor(executor)
  
  # 每个子进程启动时初始化一次
  import log.logger as logger_module
  logger_module.setup_logger()
  
  # 获取 loguru 实例并记录启动信息
  _log.info(f"Worker 子进程启动，PID: {os.getpid()}")
  
  # 实例化 Agent 并存入 state 中，确保每个进程独立
  state["agent"] = RedemptionAgent()
  
  yield # --- [程序运行中] ---

  # --- [Shutdown 逻辑] ---
  _log.info(f"进程 PID:{os.getpid()} 正在清理资源...")
  
  # 调用 Agent 内部的资源回收函数
  if "agent" in state:
    await state["agent"].close_resource()
    
  state.clear()

# 这里的 lifespan 参数即为上面定义的上下文管理器
app = FastAPI(lifespan=lifespan)

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
  """
  WebSocket 入口，支持多用户并发任务 (Pipeline)
  """
  await websocket.accept()
  active_tasks = set()
  # 从 state 中获取当前进程的 agent 实例
  agent: RedemptionAgent = state.get("agent")

  try:
    while True:
      # 1. 接收前端指令 (包含 cmd, prompt, userCode, seq)
      data = await websocket.receive_json()
      
      type = data.get("type", "chat")
      user_id = data.get("userCode")
      seq = data.get("seq")
      user_input = data.get("prompt")

      # 2. 根据命令类型分发任务协程
      if type == "loadhistory":
        task = asyncio.create_task(
          handle_load_history(user_id, seq, websocket)
        )
      else:
        # 默认执行聊天/精算逻辑
        task = asyncio.create_task(
          agent.stream_chat(user_input, user_id, seq, websocket)
        )

      # 3. 任务生命周期管理：完成后自动从集合中移除，防止内存泄漏
      active_tasks.add(task)
      task.add_done_callback(active_tasks.discard)

  except WebSocketDisconnect:
    _log.info(f"用户 {user_id} 连接已断开，正在清理任务...")
  except Exception as e:
    _log.error(f"WebSocket 异常: {str(e)}")
  finally:
    # 4. 连接关闭时，强制取消该连接产生的所有后台异步任务
    for task in active_tasks:
      if not task.done():
        task.cancel()

async def handle_load_history(user_id: str, seq: str, websocket: WebSocket):
  """
  专门处理历史记录查询的协程
  """
  agent: RedemptionAgent = state.get("agent")
  
  try:
    history = await agent.get_history(user_id)
    await websocket.send_json({
      "userCode": user_id,
      "seq": seq,
      "cmd": "loadhistory",
      "data": history,
      "status": "success"
    })
  except Exception as e:
    await websocket.send_json({
      "userCode": user_id,
      "seq": seq,
      "cmd": "loadhistory",
      "status": "fail",
      "errorMsg": str(e)
    })

if __name__ == "__main__":
  # 1. 获取服务器基础配置
  host = config.get_server_host()
  port = config.get_server_port()
  
  # 2. 处理进程数量：如果配置为 0，则获取物理 CPU 核心数
  conf_workers = config.get_server_process_num()
  final_workers = conf_workers if conf_workers > 0 else multiprocessing.cpu_count()

  # 3. 构造 Uvicorn 启动参数
  uvicorn_kwargs = {
    "app": "server:app",  # 必须使用字符串形式以启用多进程
    "host": host,
    "port": port,
    "workers": final_workers,
    "loop": "asyncio",
  }

  # 4. 加载 WSS (TLS) 证书配置
  # 根据常规实践，如果证书文件存在且端口通常为 443，则启用 SSL
  cert_path = config.get_certificate_chain_file()
  key_path = config.get_private_key_file()
  
  if os.path.exists(cert_path) and os.path.exists(key_path):
    uvicorn_kwargs.update({
      "ssl_keyfile": key_path,
      "ssl_certfile": cert_path,
    })
    protocol = "WSS"
  else:
    protocol = "WS"

  _log.info(f"--- 工银 i 豆精算管家启动 ---")
  _log.info(f"运行模式: {protocol}")
  _log.info(f"监听地址: {host}:{port}")
  _log.info(f"工作进程: {final_workers}")

  # 5. 启动
  uvicorn.run(**uvicorn_kwargs)