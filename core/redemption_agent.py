# 2 个空格对齐
import operator
import redis
from typing import Annotated, List, TypedDict, Dict, Optional, Literal, Union
from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

import config.config as config
import config.resource as resource
from core import model_factory
from core.simple_redis_saver import SimpleRedisSaver
from core.llm_tools import calculate_exchange_value, vector_search_icbc_mall, search_jd_promotion, get_points_activities, query_icbc_voucher_rules

# --- 2. 状态定义与 Agent 逻辑 ---
class AgentState(TypedDict):
  # 这里的 operator.add 确保了历史消息的持久记忆
  messages: Annotated[List[BaseMessage], operator.add]
  # 用于结构化存储提取出的积分（可选，当前主要依赖 message 历史）
  user_points: Optional[int]

class RedemptionAgent:
  def __init__(self):
    self.llm = model_factory.get_model()

    # 初始化持久化层
    self.redis_client = redis.Redis(
      host=config.get_redis_host(),
      port=config.get_redis_port(),
      db=0,
      decode_responses=False
    )

    self.checkpointer = SimpleRedisSaver(
      redis_client=self.redis_client,
      ttl=config.get_redis_msg_ttl_in_seconds()
    )

    self.tools = [
      calculate_exchange_value,
      vector_search_icbc_mall,
      search_jd_promotion,
      get_points_activities,
      query_icbc_voucher_rules
    ]

    # 绑定工具
    self.model_with_tools = self.llm.bind_tools(self.tools)
    self.tool_node = ToolNode(self.tools)

    # 编译工作流
    self.app = self._build_workflow().compile(
      checkpointer=self.checkpointer
    )

  def _call_model(self, state: AgentState):
    """大脑节点：执行带有审计和精算指令的决策"""
    # 从资源文件获取你刚才写的那个精妙的 System Prompt
    system_prompt = resource.get_resource()["default_values"]["analyze_intent_system_prompt"]

    # 这里的 state["messages"] 已经包含了通过 thread_id 捞回的历史记录
    messages = [HumanMessage(content=system_prompt)] + state["messages"]

    response = self.model_with_tools.invoke(messages)
    return {"messages": [response]}

  def _router(self, state: AgentState):
    """路由逻辑：判断是需要调用工具还是直接结束"""
    last_msg = state["messages"][-1]
    if last_msg.tool_calls:
      return "tools"
    return END

  def _build_workflow(self):
    """构建 LangGraph 状态机"""
    workflow = StateGraph(AgentState)

    workflow.add_node("agent", self._call_model)
    workflow.add_node("tools", self.tool_node)

    workflow.set_entry_point("agent")
    
    # 动态决策路径
    workflow.add_conditional_edges("agent", self._router)
    workflow.add_edge("tools", "agent")

    return workflow

  def chat(self, user_input: str, thread_id: str):
    """
    对外统一对话接口
    thread_id 用于 Redis 隔离不同用户的对话上下文
    """
    config_dict = {"configurable": {"thread_id": thread_id}}
    
    # 构造输入，LangGraph 会自动合并到 state["messages"] 中
    inputs = {
      "messages": [HumanMessage(content=user_input)]
    }

    # 执行工作流（自动处理重试和工具调用循环）
    final_state = self.app.invoke(inputs, config=config_dict)
    
    # 返回模型最后的回复内容
    return final_state["messages"][-1].content
  
  def chat_with_trace(self, user_input: str, thread_id: str):
    """
    对外统一对话接口，支持返回工具调用轨迹 (Trace)
    返回：(最终回复内容, 工具调用轨迹列表)
    """
    config_dict = {"configurable": {"thread_id": thread_id}}
    inputs = {
      "messages": [HumanMessage(content=user_input)]
    }

    # 1. 执行工作流，获取完整的状态
    final_state = self.app.invoke(inputs, config=config_dict)
    
    # 2. 提取最终回复
    final_content = final_state["messages"][-1].content
    
    # 3. 提取本次对话触发的工具轨迹
    # 我们只关注最后一次用户输入之后产生的 Tool 调用
    trace = []
    
    # 倒序查找，直到遇到本次输入的 HumanMessage 停止
    for msg in reversed(final_state["messages"][:-1]):
      if isinstance(msg, HumanMessage):
        break
        
      # 如果是包含工具调用的 AIMessage
      if isinstance(msg, AIMessage) and msg.tool_calls:
        for tc in msg.tool_calls:
          # 寻找对应的 ToolMessage (结果)
          # 在 LangGraph 中，ToolMessage 的 tool_call_id 会匹配 AIMessage 的 id
          tool_result = next(
            (m.content for m in final_state["messages"] 
             if isinstance(m, ToolMessage) and m.tool_call_id == tc["id"]), 
            "No result found"
          )
          
          trace.append({
            "tool": tc["name"],
            "input": tc["args"],
            "output": tool_result
          })
          
    # 因为是倒序提取的，最后翻转一下顺序使其符合时间线
    return final_content, list(reversed(trace))