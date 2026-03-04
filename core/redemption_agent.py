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

  async def stream_chat(self, user_input: str, user_id: str, seq: str, websocket: Any,with_trace: bool = False):
    """
    异步流式对话接口
    通过 websocket 实时推送 token 和工具执行轨迹
    """
    config_dict = {"configurable": {"thread_id": user_id}}
    inputs = {"messages": [HumanMessage(content=user_input)]}

    try:
      # 使用 astream 异步流式迭代
      # stream_mode="values" 会在每次节点更新时返回完整状态
      # stream_mode="updates" 会返回当前节点的增量更新
      async for event in self.app.astream(
        inputs, 
        config=config_dict, 
        stream_mode="updates"
      ):
        for node_name, output in event.items():
          # 1. 处理工具执行轨迹推送
          if node_name == "tools" and with_trace:
            for msg in output.get("messages", []):
              if isinstance(msg, ToolMessage):
                await websocket.send_json({
                  "userCode": user_id,
                  "seq": seq,
                  "type": "trace",
                  "data": {
                    "tool": msg.name,
                    "output": str(msg.content)[:200] + "..." # 截断过长输出
                  }
                })

          # 2. 处理 Agent 的最终文本输出
          elif node_name == "agent":
            last_msg = output["messages"][-1]
            if not last_msg.tool_calls and last_msg.content:
              # 推送最终文本
              await websocket.send_json({
                "userCode": user_id,
                "seq": seq,
                "type": "answer",
                "content": last_msg.content,
                "status": "success"
              })

    except Exception as e:
      _log.exception("stream_chat 运行异常")
      await websocket.send_json({
        "userCode": user_id,
        "seq": seq,
        "type": "error",
        "content": f"服务异常: {str(e)}"
      })

  async def close_resource(self):
    """清理资源，在 lifespan 的 yield 之后调用"""
    await self.redis_client.close()
    _log.info("Agent Redis 连接已关闭")