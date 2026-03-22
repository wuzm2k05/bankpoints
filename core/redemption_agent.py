# 2 个空格对齐
import operator
import json,re
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
  #search_jd_promotion, 
  get_points_activities, 
  query_icbc_voucher_rules,
  vector_search_wechat_products
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
      "vector_search_icbc_mall": "正在工行商城为您搜寻最优惠的商品...",
      #"search_jd_promotion": "正在对比京东同款商品的价格与优惠政策...",
      "vector_search_wechat_products": "正在对比微信小店商品的价格...",
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
      #search_jd_promotion,
      vector_search_wechat_products,
      get_points_activities,
      query_icbc_voucher_rules
    ]

    #绑定工具并构建异步工作流
    self.model_with_tools = self.llm.bind_tools(self.tools)
    self.tool_node = ToolNode(self.tools)
    self.app = self._build_workflow().compile(
      checkpointer=self.checkpointer
    )
    
    self.json_pattern = re.compile(r"\[PRODUCTS_JSON\](.*?)\[/PRODUCTS_JSON\]", re.DOTALL)
    
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
    异步获取历史记录：
    1. 严格过滤掉所有工具交互。
    2. 解析 AI 消息中的 [PRODUCTS_JSON] 块并结构化返回。
    """
    config_dict = {"configurable": {"thread_id": user_id}}
    state = await self.app.aget_state(config_dict)
    
    history = []
    if state and "messages" in state.values:
      msgs = state.values["messages"]
      
      for msg in msgs:
        # 获取基础属性
        content = getattr(msg, 'content', '') or (msg.get('content', '') if isinstance(msg, dict) else '')
        msg_type = getattr(msg, 'type', '') or (msg.get('type', '') if isinstance(msg, dict) else '')
        tool_calls = getattr(msg, 'tool_calls', None) or (msg.get('tool_calls') if isinstance(msg, dict) else None)

        # --- 1. 基础过滤 ---
        if not content or not str(content).strip():
          continue
        if msg_type == "tool" or tool_calls:
          continue

        # --- 2. 角色判定与内容清洗 ---
        if msg_type == "human":
          history.append({"role": "user", "content": content})
          
        elif msg_type == "ai":
          raw_content = str(content)
          # 尝试从内容中提取 JSON 协议块
          match = self.json_pattern.search(raw_content)
          
          # 默认值
          clean_content = raw_content
          products = []

          if match:
            try:
              # 提取并解析 JSON
              json_str = match.group(1).strip()
              product_data = json.loads(json_str)
              raw_products = product_data.get("products", [])
              
              # 映射为前端标准格式
              try:  
                for item in raw_products:
                  products.append({
                    "source": "wechat",
                    "appid": item["outAppId"],
                    "productId": item["outId"],
                    "productPromotionLink": item["link"]
                  })
              except Exception as e:
                _log.error(f"compose products in ai message error : {str(e)}")
                # error then we don't use products
                products = []
                
              # 清洗正文，剔除协议块
              clean_content = self.json_pattern.sub("", raw_content).strip()
            except Exception as e:
              _log.error(f"历史记录解析 JSON 失败: {str(e)}")

          # 特殊情况：如果内容被清洗后为空（例如 AI 只发了 JSON），则跳过或保留原样
          if not clean_content and not products:
            continue
          
          msg = {"role": "assistant","content": clean_content}
          if products:
            msg["products"] = products
            
          history.append(msg)
          
    return history
  
  async def stream_chat(self, user_input: str, user_id: str, seq: str, websocket: Any, with_trace: bool = False):
    """
    异步流式对话接口：
    1. 在 updates 模式下，节点执行完才会推送结果，因此 content 是完整的。
    2. 使用正则从 content 中分离出隐藏的 [PRODUCTS_JSON] 块。
    3. answer 字段仅保留纯文字，结构化数据放入 products 字段。
    """
    import re
    _log.debug("stream_chat 开始执行, seq: {}, user: {}", seq, user_id)
    
    config_dict = {"configurable": {"thread_id": user_id}}
    inputs = {"messages": [HumanMessage(content=user_input)]}
    
    # 局部变量，用于追踪状态和缓存最终数据
    has_sent_final_answer = False
    final_products = []
    last_full_text = ""

    # 正则表达式：匹配 [PRODUCTS_JSON]...[/PRODUCTS_JSON]
    #json_pattern = r"\[PRODUCTS_JSON\](.*?)\[/PRODUCTS_JSON\]"

    try:
      async for event in self.app.astream(
        inputs, 
        config=config_dict, 
        stream_mode="updates"
      ):
        for node_name, output in event.items():
          # --- 1. 处理 Trace (过程状态提示) ---
          if with_trace:
            trace_msg = None
            if node_name == "tools":
              msgs = output.get("messages", [])
              if msgs and isinstance(msgs[-1], ToolMessage):
                tool_msg = msgs[-1]
                friendly_desc = self.tool_descriptions.get(
                  tool_msg.name, 
                  f"正在处理 {tool_msg.name}..."
                )
                trace_msg = {
                  "seq": seq, "type": "chat", "userCode": user_id,
                  "status": "success", "isTrace": True, "answer": friendly_desc
                }
            elif node_name == "agent":
              # 如果接下来还要调工具，发一个“思考中”的 Trace
              last_msg = output.get("messages", [])[-1]
              if getattr(last_msg, 'tool_calls', None):
                trace_msg = {
                  "seq": seq, "type": "chat", "userCode": user_id,
                  "status": "success", "isTrace": True, "answer": "正在精算最优方案..."
                }
            
            if trace_msg:
              await websocket.send_json(trace_msg)

          # --- 2. 处理 Agent 的最终文本输出 ---
          if node_name == "agent":
            messages = output.get("messages", [])
            if not messages:
              continue
              
            last_msg = messages[-1]
            # 只有当 AI 不再打算调用工具时，才认为是最终回答内容
            if last_msg.content and not getattr(last_msg, 'tool_calls', None):
              raw_content = str(last_msg.content)
              match = self.json_pattern.search(raw_content)
              display_answer = raw_content
              
              if match:
                try:
                  json_str = match.group(1).strip()
                  product_data = json.loads(json_str)
                  final_products = product_data.get("products", [])
                  
                  # 从发给用户的 answer 中剔除 JSON 标记
                  display_answer = self.json_pattern.sub("", raw_content).strip()
                except Exception as je:
                  _log.error("解析隐藏商品 JSON 失败: {}", je)

              has_sent_final_answer = True
              
              await websocket.send_json({
                "seq": seq,
                "type": "chat",
                "userCode": user_id,
                "status": "success",
                "isTrace": False,
                "answer": display_answer
              })

      # --- 3. 发送结束信号 (Status: end) ---
      # 如果整个流程跑完都没触发出回答（例如 tool 执行失败），给个保底提示
      return_products = []
      try:  
        for item in final_products:
          return_products.append({
            "source": "wechat",
            "appid": item["outAppId"],
            "productId": item["outId"],
            "productPromotionLink": item["link"]
          })
      except Exception as e:
        _log.error(f"compose products after llm error : {str(e)}")
        # error then we don't use products
        return_products = []
      
      msg = {
        "seq": seq,
        "type": "chat",
        "userCode": user_id,
        "status": "end",
        "isTrace": False,
        "answer": "" if has_sent_final_answer else "抱歉，暂时没有为您找到合适的方案。"
      }
      if return_products:
        msg["products"] = return_products  
      await websocket.send_json(msg)

    except Exception as e:
      _log.error("流式对话链路异常: {}", e)
      await websocket.send_json({
        "seq": seq,
        "type": "chat",
        "userCode": user_id,
        "status": "fail",
        "isTrace": False,
        "errorCode": "500",
        "errorMsg": f"系统繁忙，请稍后再试: {str(e)}"
      })

  async def close_resource(self):
    """清理资源，在 lifespan 的 yield 之后调用"""
    pass