# 2 个空格对齐
import os
os.environ["ANONYMIZED_TELEMETRY"] = "False"
# 如果使用了 langsmith，也可以一并禁用
os.environ["LANGCHAIN_TRACING_V2"] = "false"

from fastapi import FastAPI
from pydantic import BaseModel
from core.redemption_agent import RedemptionAgent
import uvicorn

app = FastAPI()
agent = RedemptionAgent()

class ChatRequest(BaseModel):
  user_input: str
  user_id: str

@app.post("/chat")
async def chat(req: ChatRequest):
  # 假设你的 agent.chat 是异步的 (async def chat)
  # 如果目前是同步的，也可以直接调用，FastAPI 会在线程池中处理
  result = agent.chat(req.user_input, req.user_id)
  return {"answer": result}

if __name__ == "__main__":
  uvicorn.run(app, host="127.0.0.1", port=8000)