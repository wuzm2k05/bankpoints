# 2 个空格对齐
import operator
import json,re
import time
from typing import Annotated, List, TypedDict, Optional, Dict, Any
from pydantic import BaseModel,Field
from loguru import logger as _log

# 异步组件导入
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage, SystemMessage,RemoveMessage, trim_messages
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, END
from langgraph.prebuilt import ToolNode

#from langgraph.checkpoint.memory import InMemorySaver

import config.config as config
import config.resource as resource
from core import model_factory
from core.simple_redis_saver import SimpleRedisSaver

#from sqldb.sqlite_respository import SQLiteGoodsRepository

# Meter
from opentelemetry import metrics

meter = metrics.get_meter(__name__)

# 1. 整体对话请求数和耗时
chat_request_counter = meter.create_counter(
  name="agent_chat_requests_total",
  description="Total number of chat requests processed by RedemptionAgent",
  unit="1"
)

chat_duration_histogram = meter.create_histogram(
  name="agent_chat_request_duration_seconds",
  description="Duration of chat requests processed by RedemptionAgent",
  unit="s"
)

agent_node_execution_counter = meter.create_counter(
  name="agent_node_execution_total",
  description="Total number of node executions in RedemptionAgent",
  unit="1"
)

tool_node_execution_counter = meter.create_counter(
  name="tool_node_execution_total",
  description="Total number of node executions in RedemptionAgent",
  unit="1"
)

from core.llm_tools import (
  #get_ecard_voucher_rules, 
  vector_search_icbc_mall, 
  #search_jd_promotion, 
  get_points_activities, 
  query_icbc_voucher_rules,
  vector_search_wechat_products,
  query_voucher_order_status,
  create_voucher_order,
  route_back_to_router,
  route_to_goods_exchange,
  route_to_points_exchange,
  route_to_customer_service
)

class RouterDecision(TypedDict):
  next_agent: str
  flow_status: str

# --- 1. 状态定义 ---
class AgentState(TypedDict):
  # 这里的 operator.add 用于合并消息历史
  messages: Annotated[List[BaseMessage], operator.add]
  current_agent: str

class RedemptionAgent:
  def __init__(self,saver: SimpleRedisSaver):
    #self.completion_keywords = ["成功", "已完成", "已为您", "兑换好", "查询到", "办理完毕", "还有其他", "还有什么"]
    # 预定义的友好描述映射
    # 2. 定义工具名称到友好描述的映射
    self.tool_descriptions = {
      "vector_search_icbc_mall": "正在工行商城为您搜寻最优惠的商品...",
      #"search_jd_promotion": "正在对比京东同款商品的价格与优惠政策...",
      "vector_search_wechat_products": "正在全网对比商品的价格...",
      "get_points_activities": "正在为您查询最新的攒豆活动...",
      "query_icbc_voucher_rules": "正在确认立减金的兑换限制与风控要求...",
      "query_voucher_order_status": "正在查询您的立减金订单状态...",
      "create_voucher_order": "正在为您创建立减金兑换订单..."
    }
    
    config_resource = resource.get_resource()["default_values"]
    
    # ========== 加载广告推送配置 ==========
    ad_config = config_resource.get("ad_push_config", {})
    self.ad_enabled = ad_config.get("enabled", True)
    self.ad_template = ad_config.get("ad_template", "")
    
    # ========== 加载用户后缀配置 ==========
    self.user_suffix_config = config_resource.get("user_suffix_config", {})
    self.suffix_mapping = self.user_suffix_config.get("suffix_mapping", {})
    self.default_suffix = self.user_suffix_config.get("default_suffix", "")
    self.suffix_enabled = self.user_suffix_config.get("enabled", True)
    _log.info(f"用户后缀配置已加载，映射条目数: {len(self.suffix_mapping)}, 启用状态: {self.suffix_enabled}")
    
    # 2. 动态拼接各个子 Agent 的专属提示词 (追加全局规范)
    router_agent_system_prompt = self._replace_prompt_variables(config_resource['ROUTER_AGENT_PROMPT'])
    customer_service_agent_system_prompt = self._replace_prompt_variables(config_resource['CUSTOMER_SERVICE_AGENT_PROMPT'])
    points_exchange_agent_system_prompt = self._replace_prompt_variables(config_resource['POINTS_EXCHANGE_AGENT_PROMPT'])
    goods_exchange_agent_system_prompt = self._replace_prompt_variables(config_resource['GOODS_EXCHANGE_AGENT_PROMPT'])
    
    self.slide_window = config_resource['agent_settings']['slide_window']
 
    #初始化异步持久化层
    self.checkpointer = saver
    #self.checkpointer = InMemorySaver()
    
    #注册工具
    self.tools = [
      #get_ecard_voucher_rules,
      vector_search_icbc_mall,
      #search_jd_promotion,
      vector_search_wechat_products,
      get_points_activities,
      query_icbc_voucher_rules,
      query_voucher_order_status,
      create_voucher_order,
      route_back_to_router,
      route_to_customer_service,
      route_to_goods_exchange,
      route_to_points_exchange
    ]
    
    self.route_tool_names = [
      "route_back_to_router",
      "route_to_customer_service",
      "route_to_goods_exchange",
      "route_to_points_exchange"
    ]
    
    self.agent_tools_config = {
      "router": [route_to_points_exchange,route_to_goods_exchange,route_to_customer_service], # 路由网关
      "customer_service": [query_icbc_voucher_rules, query_voucher_order_status, get_points_activities,route_back_to_router],
      "points_exchange": [create_voucher_order,route_back_to_router],
      "goods_exchange": [vector_search_icbc_mall, vector_search_wechat_products,get_points_activities,route_back_to_router] # 商品导购也可以查询攒豆活动，作为辅助信息
    }
    
    self.agent_system_prompts = {
      "router": SystemMessage(content=router_agent_system_prompt),
      "customer_service": SystemMessage(content=customer_service_agent_system_prompt),
      "points_exchange": SystemMessage(content=points_exchange_agent_system_prompt),
      "goods_exchange": SystemMessage(content=goods_exchange_agent_system_prompt)
    }
    
    #获取支持异步的 LLM 实例
    agent_model_mapping = config_resource.get("agent_models", {})
    self.runnable_agents = {}
    for agent_name, tools_list in self.agent_tools_config.items():
      # 获取该 Agent 专属的模型名称
      specific_model_name = agent_model_mapping.get(agent_name, None) 
      
      # 从工厂中获取对应的模型实例（单例模式，不会重复创建连接池）
      agent_llm = model_factory.get_model(specific_model_name)
      
      # 针对网关和其他子系统的不同绑定策略
      if agent_name == "router":
          self.runnable_agents[agent_name] = agent_llm.bind_tools(
              tools_list, strict=True, tool_choice="required"
          )
      else:
          self.runnable_agents[agent_name] = agent_llm.bind_tools(
              tools_list, strict=True
          )

    #绑定工具并构建异步工作流
    self.tools_node = ToolNode(self.tools)
    self.app = self._build_workflow().compile(checkpointer=self.checkpointer)
    _log.info("RedemptionAgent 异步工作流编译完成")
    
  def _replace_prompt_variables(self, prompt_template: str) -> str:
    """
    替换提示词模板中的所有嵌套占位符变量。
    采用拓扑顺序（由原子能力 -> 拦截规则 -> 整体拼装），并完美兼容全局规范占位符。
    """
    config_resource = resource.get_resource()["default_values"]

    # 1. 提取最底层的【原子能力描述】与【全局输出 JSON 规范】
    capability_cs = config_resource["CUSTOMER_SERVICE_AGENT_CAPABILITY"]
    capability_points = config_resource["POINTS_EXCHAGNGE_AGENT_CAPABILITY"]
    capability_goods = config_resource["GOODS_EXCHAGE_AGENT_CAPABILITY"]

    # 3. 提取全局兑换率（硬编码适配 yaml 中的 {{voucher_rate}}，依据 points 房间 1100豆/元 铁律）
    try:
      from config.config import get_icbc_voucher_rate
      voucher_rate_str = str(get_icbc_voucher_rate())
    except Exception:
      voucher_rate_str = "1100"

    # 4. 执行全量点对点安全替换（避免 format 导致的数学公式 $P_{icbc}$ 报错）
    rendered_prompt = prompt_template\
      .replace("{CUSTOMER_SERVICE_AGENT_CAPABILITY}", capability_cs)\
      .replace("{POINTS_EXCHAGNGE_AGENT_CAPABILITY}", capability_points)\
      .replace("{GOODS_EXCHAGE_AGENT_CAPABILITY}", capability_goods)\
      .replace("{{voucher_rate}}", voucher_rate_str)
        
    return rendered_prompt
  
  def _build_workflow(self):
    """构建 LangGraph 异步状态机"""
    workflow = StateGraph(AgentState)

    # 1. 用标准的 async def 定义局部的单参数包装器，完美支持 await
    async def router_node_fn(state):
      return await self._process_agent_node(state, "router")

    async def customer_service_node_fn(state):
      return await self._process_agent_node(state, "customer_service")

    async def points_exchange_node_fn(state):
      return await self._process_agent_node(state, "points_exchange")

    async def goods_exchange_node_fn(state):
      return await self._process_agent_node(state, "goods_exchange")
      
    # 显式分离节点入口，确保 current_agent 状态同步精准
    workflow.add_node("router_node", router_node_fn)
    workflow.add_node("customer_service_node", customer_service_node_fn)
    workflow.add_node("points_exchange_node", points_exchange_node_fn)
    workflow.add_node("goods_exchange_node", goods_exchange_node_fn)
    workflow.add_node("tools_node", self.tools_node)
    
    workflow.add_conditional_edges("router_node", self._central_conditional_router)
    workflow.add_conditional_edges("customer_service_node", self._central_conditional_router)
    workflow.add_conditional_edges("points_exchange_node", self._central_conditional_router)
    workflow.add_conditional_edges("goods_exchange_node", self._central_conditional_router)
    workflow.add_conditional_edges("tools_node", self._tool_return_router)

    workflow.set_conditional_entry_point(self._global_entry_router)
    return workflow

  def _global_entry_router(self, state: AgentState) -> str:
    """
    【全局条件大门】：根据历史状态快照（current_agent），
    决定是走“直达快车道”，还是走“前台网关研判”。
    """
    #看上一轮最后死在哪个房间
    last_node = state.get("current_agent", "router")
    return f"{last_node}_node"
  
  async def _process_agent_node(self, state: AgentState, current_agent_name: str):
    _log.debug(f"--- [节点响应中: {current_agent_name}] ---")
    
    raw_messages = state["messages"]
    _log.debug(f"raw_messages 原始快照: {raw_messages}")
    
    def get_msg_attr(msg, attr_name, default=None):
      if hasattr(msg, attr_name):
        return getattr(msg, attr_name)
      elif isinstance(msg, dict):
        return msg.get(attr_name, default)
      return default

    route_tool_names = self.route_tool_names

    # ==================== 🧼 零残留内存清洗（完全基于工具名过滤） ====================
    clean_messages = list(raw_messages)
    
    if clean_messages:
      last_msg = clean_messages[-1]
      tool_calls = get_msg_attr(last_msg, "tool_calls", None)
      
      # 🎯 触发清洗条件：如果最后一条消息是发起“路由工具调用”的 AIMessage
      # 这意味着前一个房间触发了换房，由于条件边直接拦截跳转，这条 AIMessage 正处于新房历史的末尾
      if tool_calls and isinstance(tool_calls, list):
        if any(call.get("name") in route_tool_names for call in tool_calls):
          _log.info("🧼 发现上一条是带有路由跳转意图的 AIMessage！启动逆向清理...")
          
          while clean_messages:
            top_msg = clean_messages[-1]
            top_type = get_msg_attr(top_msg, "type")
            
            # 撞到用户的 HumanMessage 刹车
            if top_type == "human" or isinstance(top_msg, HumanMessage):
              _log.debug("Found human base line. Stop scanning.")
              break
              
            # 弹出夹在用户发言和当前房间之间的路由意图（完全在内存中抹除它的干扰）
            clean_messages.pop()
            _log.debug(f"🧹 局部清洗临时消息类型: {top_type}")

    # 🎯 【纵深安全防御层】：全面剥离 Redis 中可能由于过去历史残留的所有路由信息
    safe_messages = []
    for msg in clean_messages:
      m_type = get_msg_attr(msg, "type", "")
      
      if m_type == "remove" or "remove" in str(m_type):
        continue
      
      # 过滤包含路由工具调用的 AIMessage
      tool_calls = get_msg_attr(msg, "tool_calls", None)
      if tool_calls and isinstance(tool_calls, list):
        if any(call.get("name") in route_tool_names for call in tool_calls):
          _log.debug(f"🛡️ 过滤包含路由函数名的 AIMessage")
          continue
          
      # 过滤任何残留的路由 ToolMessage
      m_name = get_msg_attr(msg, "name", "")
      if m_type == "tool" and m_name in route_tool_names:
        _log.debug(f"🛡️ 过滤路由函数名响应 ToolMessage: {m_name}")
        continue
        
      safe_messages.append(msg)
    # =========================================================================
    truncated_messages = trim_messages(
      safe_messages,
      max_tokens=self.slide_window, # 可以按 token 数量截断，也可以按消息条数
      strategy="last",               # 保留最新的消息
      token_counter=len,             # 如果传 len，则 max_tokens 代表“保留的最长消息条数”；也可以传入 llm.get_num_tokens
      allow_partial=False,           # 核心参数：不允许拆分成对的结构（如 AIMessage 与 ToolMessage）
      end_on=("human", "tool"),      # 允许结尾的消息类型
      start_on="human",              # 核心参数：截断后的第一条消息必须是用户发言，自动清理前面残留的孤立 ToolMessage/AIMessage
    )
    
    """
    if len(safe_messages) > self.slide_window:
      truncated_messages = safe_messages[-self.slide_window:]
    else:
      truncated_messages = safe_messages
    """
    
    base_messages = [self.agent_system_prompts[current_agent_name]] + truncated_messages
    _log.debug(f"送给llm的真正messages: {base_messages}")
    
    response = await self.runnable_agents[current_agent_name].ainvoke(base_messages)
    _log.debug(f"原始响应：{response}")
    
    update_payload = {
      "current_agent": current_agent_name,
      "messages": [response]
    }
    
    # -- 增加metrics计数器
    agent_node_execution_counter.add(1,{"agent_name": current_agent_name})
        
    return update_payload
    
  def _central_conditional_router(self, state: AgentState):
    """中央条件路由：智能拦截并分流 Tool Call 意图"""
    last_msg = state["messages"][-1] if state["messages"] else None
    
    if last_msg and hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
      return "tools_node"
      
    # 如果没有工具调用且产生了文本回复，代表需要直接输出交付给用户
    return END
  
  def _tool_return_router(self, state: AgentState):
    """工具流转核心总线：依靠路由/退场工具的返回值完成智能切换"""
    last_msg = state["messages"][-1] if state["messages"] else None
    
    if last_msg and isinstance(last_msg, ToolMessage):
      # 增加 metrics 计数器
      tool_name = getattr(last_msg, 'name', 'unknown_tool')
      tool_node_execution_counter.add(1, {"tool_name": tool_name})
      
      # 检测路由分流工具的返回值
      if last_msg.content == "ROUTE_CUSTOMER_SERVICE":
        return "customer_service_node"
      elif last_msg.content == "ROUTE_POINTS":
        return "points_exchange_node"
      elif last_msg.content == "ROUTE_GOODS":
        return "goods_exchange_node"
      elif last_msg.content == "ROUTE_BACK":
        return "router_node"
        
    # 普通业务工具执行完毕，回归原母体房间继续研判
    return f"{state.get('current_agent', 'router')}_node"
  
  async def get_history(self, user_id: str) -> List[Dict]:
    config_dict = {"configurable": {"thread_id": user_id}}
    state = await self.app.aget_state(config_dict)
    
    history = []
    if state and "messages" in state.values:
      for msg in state.values["messages"]:
        content = getattr(msg, 'content', '') or (msg.get('content', '') if isinstance(msg, dict) else '')
        msg_type = getattr(msg, 'type', '') or (msg.get('type', '') if isinstance(msg, dict) else '')
        tool_calls = getattr(msg, 'tool_calls', None) or (msg.get('tool_calls') if isinstance(msg, dict) else None)

        if not str(content).strip() or msg_type == "tool" or tool_calls:
          continue

        if msg_type == "human":
          history.append({"role": "user", "content": content})
        elif msg_type == "ai":
          # 移除可能存在的 PRODUCTS_JSON 后提取纯文本展示给前端历史
          clean_content = re.sub(r'\[PRODUCTS_JSON\].*?\[/PRODUCTS_JSON\]', '', str(content), flags=re.DOTALL).strip()
          if clean_content:
            history.append({"role": "assistant", "content": clean_content})
              
    return history
  
  """
  def add_recommendation_products(self,answer):
    if not answer or not answer.strip():
      return answer

    # 1. 研判是否命中办理完成的结束语
    is_completed = any(kw in answer for kw in self.completion_keywords)
    
    if is_completed:
      _log.info("🎯 检测到回复中包含结束/办结关键词，尝试从 SQLite 提取推荐商品...")
      recommend_text = ""
      
      try:
        # 2. 实例化你的 SQLite 仓库（不传参，自动加载路径）
        repo = SQLiteGoodsRepository()
        
        # 3. 直接调用你已有的同步类函数 _sync_query，只取 1 条记录
        goods_list = repo._sync_query(product_id=None, limit=1)
        
        if goods_list:
          item = goods_list[0]
          desc = item.get("description") or desc
          link = item.get("link") or link
          _log.info(f"🎉 成功通过 Repository 类函数读取到推荐商品: {desc}")
          recommend_text = (
            f"\n\n---\n"
            f"💡 **为您推荐**：如果您有闲置积分或想寻找超值优惠，"
            f"可以看看我们为您精选的 [{desc}]({link})，点击链接即可直接前往体验哦！"
          )
          
      except Exception as e:
        _log.error(f"从 SQLite 提取推荐商品失败 (将使用默认兜底): {e}")
      
      return f"{answer}{recommend_text}"
        
    return answer
  """
  
  def _should_attach_ad(self, messages: List[Any], node_name: str) -> bool:
    """
    统一在此处判断是否符合广告推送条件
    """
    if not self.ad_enabled or not self.ad_template or not messages:
      return False

    msg_type = None
    tool_name = None
    prev_msg = messages[-2]
    if len(messages) >= 2:
      msg_type = getattr(prev_msg, "type", "") or (prev_msg.get("type") if isinstance(prev_msg, dict) else "")
      tool_name = getattr(prev_msg, "name", "") or (prev_msg.get("name") if isinstance(prev_msg, dict) else "")

    # -------------------------------------------------------------
    # 场景 A：立减金下单完成（检查倒数第二条消息 messages[-2] 是否为下单工具返回）
    # -------------------------------------------------------------
    if node_name == "points_exchange_node":    
      if msg_type == "tool" and tool_name == "create_voucher_order":
        _log.info("🎯 [广告推送] 倒数第二条消息匹配到 create_voucher_order 的 ToolMessage，立减金下单成功，准备推送广告！")
        return True

    # -------------------------------------------------------------
    # 场景 B：客服/商品咨询服务办结（检查文本是否命中结束关键词）
    # -------------------------------------------------------------
    if node_name in ["customer_service_node"]:
      if msg_type == "tool" and tool_name not in ("route_back_to_router","route_to_goods_exchange","route_to_points_exchange","route_to_customer_service"):
        # we have tool call previous msg, so add ad here
        _log.info(f"🎯 [广告推送] 节点 {node_name} 命中办结关键词，准备推送广告！")
        return True

    return False
  
  def attach_user_suffix_if_needed(self, messages: List[Any], node_name: str, display_answer: str, user_id: str) -> str:
    user_suffix = self.get_user_suffix(user_id)
    if user_suffix:
      display_answer = f"{display_answer}\n\n{user_suffix}"
      _log.debug(f"为用户 {user_id} 追加了后缀内容")
    
    return display_answer
                    
  def attach_extra_msg(self, messages: List[Any], node_name: str, display_answer: str, user_id: str) -> str:
    if self._should_attach_ad(messages, node_name):
      display_answer = f"{display_answer}\n\n{self.ad_template}"
      _log.debug(f"为节点 {node_name} 追加了广告模板内容")
    
    display_answer = self.attach_user_suffix_if_needed(messages, node_name, display_answer, user_id)
    
    return display_answer
    
  # ========== 新增：获取用户后缀内容的方法 ==========
  def get_user_suffix(self, user_id: str) -> str:
    """
    根据用户ID获取需要追加的后缀内容。
    如果配置未启用或映射为空，返回空字符串。
    """
    if not self.suffix_enabled:
      return ""
    
    if not self.suffix_mapping:
      return ""
    
    # 精确匹配用户ID
    suffix = self.suffix_mapping.get(user_id)
    
    # 如果未匹配到，使用默认后缀
    if suffix is None:
      suffix = self.default_suffix
    
    return suffix or ""
    
  async def stream_chat(self, user_input: str, user_id: str, seq: str, websocket: Any, with_trace: bool = False):
    start_time = time.time()
    
    config_dict = {"configurable": {"thread_id": user_id},"recursion_limit": 10}
    inputs = {
      "messages": [HumanMessage(content=user_input)]
    }
    
    has_sent_final_answer = False
    final_products = []

    try:
      async for event in self.app.astream(inputs, config=config_dict, stream_mode="updates"):
        for node_name, output in event.items():
          # 动态链路状态 Trace 提示
          if with_trace:
            trace_msg = None
            if node_name in ["tools_node"]:
              msgs = output.get("messages", [])
              if msgs and isinstance(msgs[-1], ToolMessage):
                friendly_desc = self.tool_descriptions.get(msgs[-1].name, f"正在处理...")
                trace_msg = {"seq": seq, "type": "chat", "userCode": user_id, "status": "success", "isTrace": True, "answer": friendly_desc}
            elif node_name in ["customer_service_node", "points_exchange_node", "goods_exchange_node"]:
              msgs = output.get("messages", [])
              if msgs and getattr(msgs[-1], 'tool_calls', None):
                trace_msg = {"seq": seq, "type": "chat", "userCode": user_id, "status": "success", "isTrace": True, "answer": "正在为您核实中..."}
            
            if trace_msg:
              await websocket.send_json(trace_msg)

          # 最终回复流提取与隐藏 [PRODUCTS_JSON] 块拆解
          if node_name in ["customer_service_node", "points_exchange_node", "goods_exchange_node"]:
            messages = output.get("messages", [])
            if not messages:
              continue
              
            last_msg = messages[-1]
            if last_msg.content and not getattr(last_msg, 'tool_calls', None):
              raw_text = str(last_msg.content)
              
              # 正则剥离隐藏的商城特惠商品 JSON 数据协议
              product_match = re.search(r'\[PRODUCTS_JSON\](.*?)\[/PRODUCTS_JSON\]', raw_text, re.DOTALL)
              if product_match:
                try:
                  product_data = json.loads(product_match.group(1).strip())
                  final_products = product_data.get("products", [])
                except Exception:
                  final_products = []
                display_answer = re.sub(r'\[PRODUCTS_JSON\].*?\[/PRODUCTS_JSON\]', '', raw_text, flags=re.DOTALL).strip()
              else:
                #display_answer = self.add_recommendation_products(raw_text)
                display_answer = raw_text
              
              # ========== 增加额外内容 ==========
              full_state = await self.app.aget_state(config_dict)
              full_messages = full_state.values.get("messages",[]) if full_state and full_state.values else messages
              display_answer = self.attach_extra_msg(full_messages, node_name, display_answer,user_id)
              
              has_sent_final_answer = True
              await websocket.send_json({
                "seq": seq, "type": "chat", "userCode": user_id, "status": "success", "isTrace": False, "answer": display_answer
              })

      # 3. 闭环交付商品卡片结构体与 End 状态信号
      return_products = []
      for item in final_products:
        return_products.append({
          "source": "wechat",
          "appid": item.get("outAppId", ""),
          "productId": item.get("outId", ""),
          "productPromotionLink": item.get("link", "")
        })
      
      end_msg = {
        "seq": seq, "type": "chat", "userCode": user_id, "status": "end", "isTrace": False,
        "answer": "" if has_sent_final_answer else "好的，请问还有什么我可以帮您的？"
      }
      if return_products:
        end_msg["products"] = return_products  
      
      await websocket.send_json(end_msg)
      
      # metrics 计数器：记录整体请求耗时
      duration = time.time() - start_time
      chat_duration_histogram.record(duration, {"status": "success"})
      chat_request_counter.add(1, {"status": "success"})

    except Exception as e:
      # metrics 计数器：记录异常请求耗时
      duration = time.time() - start_time
      chat_duration_histogram.record(duration, {"status": "fail"})
      chat_request_counter.add(1, {"status": "fail"})
      
      _log.error("流式对话网关异常: {}", e)
      await websocket.send_json({
        "seq": seq, "type": "chat", "userCode": user_id, "status": "fail", "isTrace": False, "errorCode": "500", "errorMsg": "系统繁忙没能正常响应您的请求，请稍后再试。"
      })
      
  async def close_resource(self):
    """清理资源，在 lifespan 的 yield 之后调用"""
    pass