import asyncio,traceback
import ssl
import pathlib
import websockets
from aiohttp import web

from config import config
import log.logger
from core.redemption_agent import RedemptionAgent

_log = log.logger.get_logger()

async def main():
  # --- 3. 测试运行 ---

  agent = RedemptionAgent()
  
  while True:
    user_input = input("请输入您的问题（输入 'exit' 退出）: ")
    if user_input.lower() == 'exit':
      break
    
    try:
      response = agent.chat(user_input, thread_id="test_user")
      print("智能体回复:", response)
    except Exception as e:
      _log.error(traceback.format_exc())
      print("发生错误:", str(e))
      break
  
  #print("--- 第一轮 ---")
  #print(agent.chat("我想买个耳机给爸爸", thread_id="user_888"))
  
  #print("\n--- 第二轮 ---")
  #print(agent.chat("我大概有 10000 积分", thread_id="user_888"))


# Start servers
if __name__ == "__main__":
  # Get the default event loop and run the main routine
  loop = asyncio.get_event_loop()
  loop.run_until_complete(main())

 
  