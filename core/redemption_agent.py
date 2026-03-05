# 2 个空格对齐
import operator
import json
from typing import Annotated, List, TypedDict, Optional, Dict, Any
from loguru import logger as _log

# 异步组件导入
from redis.asyncio import Redis as AsyncRedis
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

import config.config as config
import config.resource as resource
from core import model_factory
from core.simple_redis_saver import SimpleRedisSaver
from core.llm_tools import (
  get_ecard_voucher_rules, 
  vector_search_icbc_mall, 
  search_jd_promotion, 
  get_points_activities, 
  query_icbc_voucher_rules
)

# --- 1. 状态定义 ---
class AgentState(TypedDict):
  # 这里的 operator.add 用于合并消息历史
  messages: Annotated[List[BaseMessage], operator.add]
  user_points: Optional[int]

class RedemptionAgent:
  def __init__(self):
    # 1. 获取支持异步的 LLM 实例
    self.llm = model_factory.get_model()

    # 2. 初始化异步 Redis 客户端
    self.redis_client = AsyncRedis(
      host=config.get_redis_host(),
      port=config.get_redis_port(),
      db=0,
      decode_responses=False # 注意：Saver 内部可能需要原始 bytes 进行 Pickle
    )

    # 3. 初始化异步持久化层
    self.checkpointer = SimpleRedisSaver(
      redis_client=self.redis_client,
      ttl=config.get_redis_msg_ttl_in_seconds()
    )

    # 4. 注册工具
    self.tools = [
      get_ecard_voucher_rules,
      vector_search_icbc_mall,
      search_jd_promotion,
      get_points_activities,
      query_icbc_voucher_rules
    ]

    # 5. 绑定工具并构建异步工作流
    self.model_with_tools = self.llm.bind_tools(self.tools)
    self.tool_node = ToolNode(self.tools)
    self.app = self._build_workflow().compile(
      checkpointer=self.checkpointer
    )
    _log.info("RedemptionAgent 异步工作流编译完成")

  def _build_workflow(self):
    """构建 LangGraph 异步状态机"""
    workflow = StateGraph(AgentState)

    workflow.add_node("agent", self._call_model)
    workflow.add_node("tools", self.tool_node)

    workflow.set_entry_point("agent")
    workflow.add_conditional_edges("agent", self._router)
    workflow.add_edge("tools", "agent")

    return workflow

  async def _call_model(self, state: AgentState):
    """异步大脑节点"""
    _log.debug("--- 正在调用 LLM ---") # 添加这一行
    system_prompt = resource.get_resource()["default_values"]["analyze_intent_system_prompt"]
    messages = [SystemMessage(content=system_prompt)] + state["messages"]
    
    # 异步调用模型
    response = await self.model_with_tools.ainvoke(messages)
    return {"messages": [response]}

  def _router(self, state: AgentState):
    """路由逻辑"""
    last_msg = state["messages"][-1]
    if last_msg.tool_calls:
      return "tools"
    return END

  async def get_history(self, user_id: str) -> List[Dict]:
    """
    异步获取历史记录接口
    """
    config_dict = {"configurable": {"thread_id": user_id}}
    state = await self.app.aget_state(config_dict)
    
    history = []
    if state and "messages" in state.values:
      for msg in state.values["messages"]:
        role = "user" if isinstance(msg, HumanMessage) else "assistant"
        # 过滤掉工具调用的中间报文，只给前端看对话
        if isinstance(msg, (HumanMessage, AIMessage)) and msg.content:
          history.append({"role": role, "content": msg.content})
    return history

  async def stream_chat(self, user_input: str, user_id: str, seq: str, websocket: Any, with_trace: bool = False):
    """
    异步流式对话接口：保持 type 为 chat，通过 isTrace 区分内容
    """
    _log.debug("stream_chat 开始执行, seq: {}", seq)
    
    config_dict = {"configurable": {"thread_id": user_id}}
    inputs = {"messages": [HumanMessage(content=user_input)]}
    has_sent_answer = False

    try:
      async for event in self.app.astream(
        inputs, 
        config=config_dict, 
        stream_mode="updates"
      ):
        for node_name, output in event.items():
          # 1. 处理 Trace (中间过程)
          if with_trace:
            trace_msg = None
            if node_name == "tools":
              msgs = output.get("messages", [])
              if msgs and isinstance(msgs[-1], ToolMessage):
                tool_msg = msgs[-1]
                trace_msg = {
                  "seq": seq,
                  "type": "chat",
                  "userCode": user_id,
                  "status": "success",
                  "isTrace": True,
                  "answer": f"正在通过 {tool_msg.name} 查询相关信息...",
                  "data": {"tool": tool_msg.name, "output": str(tool_msg.content)[:100]}
                }
            elif node_name == "agent":
              # 只有当接下来还要调工具时，才发送“思考中”的 trace
              last_msg = output.get("messages", [])[-1]
              if getattr(last_msg, 'tool_calls', None):
                trace_msg = {
                  "seq": seq,
                  "type": "chat",
                  "userCode": user_id,
                  "status": "success",
                  "isTrace": True,
                  "answer": "AI 正在分析需求并准备调用工具..."
                }

            if trace_msg:
              await websocket.send_json(trace_msg)

          # 2. 处理正式 Answer (最终回答)
          if node_name == "agent":
            messages = output.get("messages", [])
            if messages:
              last_msg = messages[-1]
              # 仅当没有 tool_calls 时，才认为这是发给用户的最终文本
              if last_msg.content and not getattr(last_msg, 'tool_calls', None):
                has_sent_answer = True
                await websocket.send_json({
                  "seq": seq,
                  "type": "chat",
                  "userCode": user_id,
                  "status": "success",
                  "isTrace": False,
                  "answer": last_msg.content
                })

      # 3. 发送结束标志
      await websocket.send_json({
        "seq": seq,
        "type": "chat",
        "userCode": user_id,
        "status": "end",
        "isTrace": False,
        "answer": "" if has_sent_answer else "未搜索到相关结果。"
      })

    except Exception as e:
      _log.error("流式对话异常: {}", e)
      await websocket.send_json({
        "seq": seq,
        "type": "chat",
        "userCode": user_id,
        "status": "fail",
        "isTrace": False,
        "errorCode": "500",
        "errorMsg": str(e)
      })

  async def close_resource(self):
    """清理资源，在 lifespan 的 yield 之后调用"""
    await self.redis_client.close()
    _log.info("Agent Redis 连接已关闭")