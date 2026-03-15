# 2 个空格对齐
import operator
import json
from typing import Annotated, List, TypedDict, Optional, Dict, Any
from loguru import logger as _log

# 异步组件导入
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage, SystemMessage
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

import config.config as config
import config.resource as resource
from core import model_factory
from core.simple_redis_saver import SimpleRedisSaver
from core.llm_tools import (
  #get_ecard_voucher_rules, 
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
  def __init__(self,saver: SimpleRedisSaver):
    # 预定义的友好描述映射
    # 2. 定义工具名称到友好描述的映射
    self.tool_descriptions = {
      "vector_search_icbc_mall": "正在工行商城为您搜寻最优惠的商品和E卡...",
      "search_jd_promotion": "正在对比京东同款商品的价格与优惠政策...",
      "get_points_activities": "正在为您查询最新的攒豆活动...",
      "query_icbc_voucher_rules": "正在确认立减金的兑换限制与风控要求..."
    }
    
    #预编译 System Prompt
    raw_prompt = resource.get_resource()["default_values"]["analyze_intent_system_prompt"]
    voucher_rate = config.get_icbc_voucher_rate()
    # 在初始化时就存入内存，后续直接引用
    self.base_system_message = SystemMessage(
      content=raw_prompt.replace("{{voucher_rate}}", str(voucher_rate))
    )
    
    #获取支持异步的 LLM 实例
    self.llm = model_factory.get_model()

    #初始化异步持久化层
    self.checkpointer = saver
    
    #注册工具
    self.tools = [
      #get_ecard_voucher_rules,
      vector_search_icbc_mall,
      search_jd_promotion,
      get_points_activities,
      query_icbc_voucher_rules
    ]

    #绑定工具并构建异步工作流
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
    messages = [self.base_system_message] + state["messages"]
    
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
    异步获取历史记录：严格过滤掉所有工具交互及中间 JSON 报文
    """
    config_dict = {"configurable": {"thread_id": user_id}}
    state = await self.app.aget_state(config_dict)
    
    history = []
    if state and "messages" in state.values:
      msgs = state.values["messages"]
      
      for msg in msgs:
        # 兼容处理字典或对象
        content = getattr(msg, 'content', '') or (msg.get('content', '') if isinstance(msg, dict) else '')
        msg_type = getattr(msg, 'type', '') or (msg.get('type', '') if isinstance(msg, dict) else '')
        tool_calls = getattr(msg, 'tool_calls', None) or (msg.get('tool_calls') if isinstance(msg, dict) else None)

        # --- 核心过滤逻辑 ---
        
        # 1. 必须有内容
        if not content or not str(content).strip():
          continue
          
        # 2. 排除工具执行结果 (ToolMessage)
        if msg_type == "tool":
          continue
          
        # 3. 排除 AI 的工具调用指令 (带 tool_calls 的 AIMessage)
        if tool_calls:
          continue

        # 4. 排除 AI 直接复读的原始 JSON 数据 (防止那种 AI: [{"name":...}] 的情况)
        # 判定标准：内容是以 [ 开头并以 ] 结尾，且尝试解析 JSON 成功
        stripped_content = str(content).strip()
        if stripped_content.startswith("[") and stripped_content.endswith("]"):
          try:
            json.loads(stripped_content)
            continue # 如果是纯 JSON 数组，跳过，不给用户看
          except:
            pass # 解析失败说明是普通文本，保留

        # 5. 确定角色
        if msg_type == "human":
          history.append({"role": "user", "content": content})
        elif msg_type == "ai":
          history.append({"role": "assistant", "content": content})
          
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
                # 获取友好描述，如果没定义则回退到函数名
                friendly_desc = self.tool_descriptions.get(
                  tool_msg.name, 
                  f"正在执行 {tool_msg.name}..."
                )
                
                trace_msg = {
                  "seq": seq,
                  "type": "chat",
                  "userCode": user_id,
                  "status": "success",
                  "isTrace": True,
                  "answer": friendly_desc,
                  # don't send the full tool output to frontend, just indicate which tool is being executed
                  #"data": {"tool": tool_msg.name, "output": str(tool_msg.content)[:100]}
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
    pass