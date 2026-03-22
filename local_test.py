# 2 个空格对齐
import aiohttp
import asyncio
import uuid
import json
import ssl
import argparse
from typing import Dict, List, Optional

# 全局状态
pending_responses: Dict[str, List[str]] = {}
input_lock = asyncio.Event()
current_token: Optional[str] = None


host = "www.node09.cn"
#host = "node09.cn"
token_server_port = 8444
#msg_server_port = 8443

msg_server_port = 9443

# --- 1. mTLS Token 管理逻辑 ---
async def manage_token(cmd: str, token_to_cancel: str = None) -> Optional[str]:
  """
  通过 mTLS 连接到 Token Server
  """
  
  # 配置客户端 mTLS 上下文
  ssl_ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)
  # 加载你生成的客户端证书和私钥
  try:
    ssl_ctx.load_verify_locations(cafile="data/token_ca_cert.crt")
    ssl_ctx.load_cert_chain(certfile="data/client.crt", keyfile="data/client.key")
    #pass
  except Exception as e:
    print(f"\033[31m[❌ 证书错误] 无法加载 client.crt/key: {e}\033[0m")
    return None

  # 不验证服务端证书
  ssl_ctx.check_hostname = True
  ssl_ctx.verify_mode = ssl.CERT_REQUIRED
  #ssl_ctx.verify_mode = ssl.CERT_NONE

  try:
    try:
      # 给连接加个 5 秒超时
      reader, writer = await asyncio.wait_for(
          asyncio.open_connection(host, token_server_port, ssl=ssl_ctx), 
          timeout=5.0
      )
    except asyncio.TimeoutError:
      print("❌ 连接 Token Server 超时：TCP 通了但 TLS 握手没反应")
    
    payload = {"cmd": cmd}
    if token_to_cancel:
      payload["token"] = token_to_cancel
    
    writer.write((json.dumps(payload) + "\n").encode())
    await writer.drain()
    
    line = await reader.readline()
    writer.close()
    await writer.wait_closed()
    
    res = json.loads(line.decode())
    print(f"\033[32m[✅ Token Server 响应]: {res}\033[0m")
    if res.get("status") == "success":
      if cmd == "getNewToken":
        return res.get("token")
      elif cmd == "wechat_db_sync":
        return "db is syncing"
      else:
        return "cancelled"
    else:
      print(f"\033[31m[❌ Token 操作失败]: {res.get('errorMsg')}\033[0m")
  except Exception as e:
    print(f"\033[31m[❌ Token Server 连接异常]: {e}\033[0m")
  return None

# --- 2. WebSocket 响应处理 ---
async def handle_response(data: dict):
  seq = data.get("seq")
  msg_type = data.get("type")
  status = data.get("status")
  is_trace = data.get("isTrace", False)
  
  if status == "fail":
    error_msg = data.get("errorMsg", "未知错误")
    print(f"\r\033[31m[❌ 失败] Seq: {seq} | 错误: {error_msg}\033[0m\n")
    input_lock.set()
    return

  if msg_type == "chat":
    print(data)
    answer = data.get("answer", "")
    if is_trace:
      print(f"\n\033[34m[🔍 Trace]: {answer}\033[0m")
      return

    if seq not in pending_responses:
      pending_responses[seq] = []
      print(f"[🤖 Agent]: ", end="", flush=True)
    
    print(answer, end="", flush=True)
    pending_responses[seq].append(answer)
    
    if status == "end":
      print("\n")
      del pending_responses[seq]
      input_lock.set()

  elif msg_type == "loadUserHistory":
    history = data.get("history", [])
    print(f"\n[📜 历史记录] 共 {len(history)} 条:")
    for item in history:
      role = "用户" if item["role"] == "user" else "AI"
      print(f"  {role}: {item['content']}")
      if "products" in item:
        print(f"{item['products']}")
    print("-" * 30 + "\n")
    input_lock.set()

async def listen_loop(ws: aiohttp.ClientWebSocketResponse):
  async for msg in ws:
    if msg.type == aiohttp.WSMsgType.TEXT:
      try:
        await handle_response(json.loads(msg.data))
      except: pass
    elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
      input_lock.set()
      break

# --- 3. 主程序 ---
async def start_client(user_code: str):
  global current_token
  ws_url = f"wss://{host}:{msg_server_port}/v1/chat"
  
  ssl_ctx = ssl.create_default_context()
  ssl_ctx.check_hostname = False
  ssl_ctx.verify_mode = ssl.CERT_NONE
  
  print("=" * 60)
  print(f"🚀 工银 i 豆精算管家 - 集成测试客户端")
  print(f"🔧 指令: [get_token] [cancel_token] [wechat_db_sync] [history] [聊天内容] [exit]")
  print("=" * 60)

  async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
    try:
      async with session.ws_connect(ws_url, ssl=False) as ws:
        input_lock.set()
        asyncio.create_task(listen_loop(ws))
        
        while True:
          await input_lock.wait()
          prompt = await asyncio.get_event_loop().run_in_executor(
            None, lambda: input(f"\n(Token: {'已就绪' if current_token else '未获取'}) > ").strip()
          )

          if prompt.lower() in ['exit', 'quit']: break
          if not prompt: continue

          # 特殊指令处理
          if prompt.lower() == "get_token":
            print("[🔑] 正在通过 mTLS 请求新 Token...")
            current_token = await manage_token("getNewToken")
            if current_token: print(f"[✅] 获取成功: {current_token}")
            continue

          if prompt.lower() == "cancel_token":
            if current_token:
              await manage_token("cancelToken", current_token)
              print(f"[🗑️] Token {current_token} 已注销")
              #current_token = None
            else:
              print("[⚠️] 当前没有活跃的 Token")
            continue
          
          if prompt.lower() == "wechat_db_sync":
            await manage_token("wechatDBSync", current_token)
            print(f"[🗑️] start sync wechat db")
            continue

          # 正常请求
          input_lock.clear()
          request_type = "loadUserHistory" if prompt.lower() == "history" else "chat"
          payload = {
            "seq": str(uuid.uuid4())[:8],
            "type": request_type,
            "userCode": user_code,
            "token": current_token # 自动携带 Token
          }
          
          if request_type == "chat":
            payload["prompt"] = prompt
            payload["enableTrace"] = True

          await ws.send_json(payload)

    except Exception as e:
      print(f"❌ 连接异常: {e}")

if __name__ == "__main__":
  parser = argparse.ArgumentParser()
  parser.add_argument("-u", "--user", type=str, default="tester_001")
  args = parser.parse_args()
  try:
    asyncio.run(start_client(args.user))
  except KeyboardInterrupt:
    pass