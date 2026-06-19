# 2 个空格对齐
import operator
import json,re
from typing import Annotated, List, TypedDict, Optional, Dict, Any
from pydantic import BaseModel,Field
from loguru import logger as _log

# 异步组件导入
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage, SystemMessage,RemoveMessage
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
  vector_search_wechat_products,
  query_voucher_order_status,
  create_voucher_order
)

class RouterDecision(TypedDict):
  next_agent: str
  flow_status: str

# --- 1. 状态定义 ---
class AgentState(TypedDict):
  # 这里的 operator.add 用于合并消息历史
  messages: Annotated[List[BaseMessage], operator.add]
  current_agent: str
  router_decision: Optional[RouterDecision]

class RedemptionAgent:
  def __init__(self,saver: SimpleRedisSaver):
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
    
    # 2. 动态拼接各个子 Agent 的专属提示词 (追加全局规范)
    router_agent_system_prompt = self._replace_prompt_variables(config_resource['ROUTER_AGENT_PROMPT'])
    customer_service_agent_system_prompt = self._replace_prompt_variables(config_resource['CUSTOMER_SERVICE_AGENT_PROMPT'])
    points_exchange_agent_system_prompt = self._replace_prompt_variables(config_resource['POINTS_EXCHANGE_AGENT_PROMPT'])
    goods_exchange_agent_system_prompt = self._replace_prompt_variables(config_resource['GOODS_EXCHANGE_AGENT_PROMPT'])
    
    self.slide_window = config_resource['agent_settings']['slide_window']
 
    #获取支持异步的 LLM 实例
    self.base_llm = model_factory.get_model()

    #初始化异步持久化层
    self.checkpointer = saver
    
    #注册工具
    self.tools = [
      #get_ecard_voucher_rules,
      vector_search_icbc_mall,
      #search_jd_promotion,
      vector_search_wechat_products,
      get_points_activities,
      query_icbc_voucher_rules,
      query_voucher_order_status,
      create_voucher_order
    ]
    
    self.agent_tools_config = {
      "router": [], # 路由网关纯聊天/做决策，不需要任何工具
      "customer_service": [query_icbc_voucher_rules, query_voucher_order_status, get_points_activities],
      "points_exchange": [create_voucher_order],
      "goods_exchange": [vector_search_icbc_mall, vector_search_wechat_products,get_points_activities] # 商品导购也可以查询攒豆活动，作为辅助信息
    }
    
    self.agent_system_prompts = {
      "router": router_agent_system_prompt, # 不绑定工具
      "customer_service": customer_service_agent_system_prompt,
      "points_exchange": points_exchange_agent_system_prompt,
      "goods_exchange": goods_exchange_agent_system_prompt
    }
    
    self.runnable_agents = {
      "router": self.base_llm, # 不绑定工具
      "customer_service": self.base_llm.bind_tools(self.agent_tools_config["customer_service"]),
      "points_exchange": self.base_llm.bind_tools(self.agent_tools_config["points_exchange"]),
      "goods_exchange": self.base_llm.bind_tools(self.agent_tools_config["goods_exchange"])
    }

    #绑定工具并构建异步工作流
    self.tool_node = ToolNode(self.tools)
    self.app = self._build_workflow().compile(
      checkpointer=self.checkpointer
    )
     
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
    unified_json_spec = config_resource["UNIFIED_JSON_SPEC"]

    # 2. 拓扑级组装【中转拦截规则】（先把原子能力注入到拦截器文本中）
    goto_customer_service = config_resource["GOTO_CUSTOMER_SERVICE_RULE"]\
        .replace("{CUSTOMER_SERVICE_AGENT_CAPABILITY}", capability_cs)
        
    goto_points_exchange = config_resource["GOTO_POINTS_EXCHANGE_RULE"]\
        .replace("{POINTS_EXCHAGNGE_AGENT_CAPABILITY}", capability_points)
        
    goto_goods_exchange = config_resource["GOTO_GOODS_EXCHANGE_RULE"]\
        .replace("{GOODS_EXCHAGE_AGENT_CAPABILITY}", capability_goods)
        
    goto_router = config_resource["GOTO_ROUTER_RULE"]

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
        .replace("{GOTO_CUSTOMER_SERVICE_RULE}", goto_customer_service)\
        .replace("{GOTO_POINTS_EXCHANGE_RULE}", goto_points_exchange)\
        .replace("{GOTO_GOODS_EXCHANGE_RULE}", goto_goods_exchange)\
        .replace("{GOTO_ROUTER_RULE}", goto_router)\
        .replace("{UNIFIED_JSON_SPEC}", unified_json_spec)\
        .replace("{{voucher_rate}}", voucher_rate_str)
        
    return rendered_prompt
  
  def _build_workflow(self):
    """构建 LangGraph 异步状态机"""
    workflow = StateGraph(AgentState)

    workflow.add_node("router_node", self._process_agent_node)
    workflow.add_node("customer_service_node", self._process_agent_node)
    workflow.add_node("points_exchange_node", self._process_agent_node)
    workflow.add_node("goods_exchange_node", self._process_agent_node)
    workflow.add_node("tools", self.tool_node)
    
    #workflow.set_entry_point("router_node")
    workflow.add_conditional_edges("router_node", self._central_conditional_router)
    workflow.add_conditional_edges("customer_service_node", self._central_conditional_router)
    workflow.add_conditional_edges("points_exchange_node", self._central_conditional_router)
    workflow.add_conditional_edges("goods_exchange_node", self._central_conditional_router)
    workflow.add_conditional_edges("tools", self._tool_return_router)

    workflow.set_conditional_entry_point(self._global_entry_router)

    return workflow

  def _global_entry_router(self, state: AgentState) -> str:
    """
    【全局条件大门】：根据历史状态快照（current_agent），
    决定是走“直达快车道”，还是走“前台网关研判”。
    """
    # 1. 顺藤摸瓜：看上一轮最后死在哪个房间
    last_node = state.get("current_agent", "router")
    return f"{last_node}_node"
    
  async def _process_agent_node(self, state: AgentState):
    decision = state.get("router_decision") or {}
    curr = decision.get("next_agent") or state.get("current_agent") or "router"
    _log.debug(f"--- [节点响应中: {curr}] ---")
    
    # 长对话滑动窗口裁剪，避免上下文无限膨胀
    raw_messages = state["messages"]
    if len(raw_messages) > self.slide_window:
      truncated_messages = raw_messages[-self.slide_window:]
      _log.debug(f"针对 [{curr}] 触发长对话裁剪，历史从 {len(raw_messages)} 压至 {self.slide_window} 条")
    else:
      truncated_messages = raw_messages
    
    # 1. 组装基础上下文：大模型专属系统提示词 + 历史消息
    base_messages = [self.agent_system_prompts[curr]] + truncated_messages
    _log.debug(f"送给大模型的消息: {base_messages}")
    
    MAX_RETRY = 1
    retry_count = 0
    
    # 💥 关键点：用一个独立的局部列表来临时滚雪球积累重试上下文，绝不污染全局
    local_retry_context = []
    
    # 首次尝试获取大模型响应
    response = await self.runnable_agents[curr].ainvoke(base_messages)
    clean_res = response
    _log.debug(f"原始响应: {response}")
    
    while retry_count <= MAX_RETRY:
      # 🌟 拦截优先：如果大模型突然想要调用工具，工具调用不参与 JSON 协议格式校验，直接放行
      if getattr(response, "tool_calls", None):
        _log.debug(f" -> [{curr}] 发射 tool_calls 意图，优先引流至工具链")
        return {
          "current_agent": curr,
          "messages": [response] # 工具意图正常存入历史
        }
      
      # 🌟【纯 JSON 契约解析验证】：直接验证标准格式
      try:
        clean_res = re.sub(r'```json\n?|\n?```', '', response.content).strip()
        parsed_json = json.loads(clean_res)
        router_decision = parsed_json.get("router_decision", {"next_agent": "router", "flow_status": "FINAL_RESPONSE"})
        break
      except Exception as e:
        retry_count += 1
        if retry_count <= MAX_RETRY:
          _log.warning(f"⚠️ [{curr}] 第 {retry_count} 次解析 JSON 契约失败，触发自我纠错...")
          fix_hint = HumanMessage(content="【格式错误】你的上一次回复不符合 JSON Schema 规范！请移除任何 Markdown 标记（如 ```json），直接输出符合 Schema 的纯 JSON 字符串，不要包含任何解释性文字。")
          local_retry_context.extend([response, fix_hint])
          response = await self.runnable_agents[curr].ainvoke(base_messages + local_retry_context)
        else:
          _log.error(f"❌ [{curr}] 历经纠错后依旧失败，触发降级兜底。{e}")

    # 2. 判定最终结果
    if retry_count > MAX_RETRY:
      # 💥 极端情况：重试也完全失败了。我们人工造一个干净的兜底话术 AIMessage，替换掉大模型的乱码
      fallback_text = "抱歉，系统处理时遇到了技术问题，请您尝试重新发送指令或联系客服人员。"
      fake_json_contract = {
        "router_decision": {
          "next_agent": curr,               # 留在当前房间
          "flow_status": "FINAL_RESPONSE"   # 交付给用户
        },
        "reply": fallback_text,
        "products": []
      }
      
      perfect_response = AIMessage(content=json.dumps(fake_json_contract, ensure_ascii=False))
      router_decision = fake_json_contract["router_decision"] 
    else:
      # 💥 正常/重试成功情况：将解析转换好的干净内容强行挂载到 response 消息中
      # 关键秘密：我们用最后这个完美的 response，直接替换并代表整个重试过程！
      perfect_response = AIMessage(content=clean_res)

    # 3. 决定是否将消息存入 LangGraph 历史大池子
    flow_status = router_decision.get("flow_status", "FINAL_RESPONSE") if isinstance(router_decision, dict) else "FINAL_RESPONSE"
    saved_messages = [perfect_response] 
    
    # ToolMessage 垃圾回收 与 消息脱水
    if flow_status == "FINAL_RESPONSE": 
      #【倒序极速扫描】：从最新的一条消息开始往前瞅
      for msg in reversed(state.get("messages",[])):
        # 工业级防御：兼容从持久化层读出来的 dict 格式或标准 Message 对象
        msg_type = getattr(msg, "type", None) or msg.get("type") if isinstance(msg, dict) else None
        msg_id = getattr(msg, "id", None) or msg.get("id") if isinstance(msg, dict) else None
        has_tool_calls = getattr(msg, "tool_calls", None) or msg.get("tool_calls") if isinstance(msg, dict) else None

        # 情况 A：如果是工具返回的结果，物理删除
        if msg_type == "tool" and msg_id:
          saved_messages.append(RemoveMessage(id=msg_id))
          _log.debug(f"🧹 已向状态机发射删除信号：ToolMessage (ID: {msg_id})")
          
        # 情况 B：如果是本轮大模型吐出的、带有 tool_calls 的中间态思考消息，也物理删除
        elif msg_type == "ai" and has_tool_calls and msg_id:
          saved_messages.append(RemoveMessage(id=msg_id))
          _log.debug(f"🧹 已向状态机发射删除信号：带有 tool_calls 的 AIMessage (ID: {msg_id})")
          
        # 情况 C：💥【核心刹车点】一旦撞到了人类的发言，或者撞到了上一轮老AI发言
        # 说明本轮产生的工具流碎片已经“全部全员靠岸并扫描完毕”了，立刻切断循环！
        elif msg_type in ["human", "user"] or msg_type == "ai":
          _log.debug(f"⚡ 倒序扫描本轮碎片完成，在消息 [{msg_type}] 处触发高性能刹车")
          break
          
        else:
          continue
    
    return {
      "current_agent": curr,
      "router_decision": router_decision,
      # 🌟 绝不在 messages 里返回 local_retry_context！
      # 我们只返回 perfect_response。在外部（历史记录和图状态）看来，这个 Agent 极其完美，
      # 永远是一次性就吐出了正确格式的消息，中间狼狈的纠错痕迹随风蒸发，绝对不落盘！
      "messages": saved_messages if flow_status == "FINAL_RESPONSE" else []
    }
    
  def _central_conditional_router(self, state: AgentState):
    """【全新升级】解耦的唯一中央条件路由分流控制核心"""
    last_msg = state["messages"][-1] if state["messages"] else None
    
    # 最高优先截获：如果有未完成的工具意图，闭着眼送往 tools_node 节点
    if last_msg and hasattr(last_msg, "tool_calls") and last_msg.tool_calls:
      return "tools"
      
    decision = state.get("router_decision")
    if not decision: 
      return END
      
    curr_node = state.get("current_agent", "router")
    next_node = decision.get("next_agent", "router")
    flow_status = decision.get("flow_status", "FINAL_RESPONSE")
    
    _log.debug(f"🧭 路由研判 -> 驻留地: [{curr_node}] | 意图去往: [{next_node}] | 模式: [{flow_status}]")
    
    # 判定 1：大模型要求最终交付 (FINAL_RESPONSE)
    # 无论 next_agent 填了谁，只要是 FINAL_RESPONSE，就代表本轮要对人类说话了，必须退出图，冻结本轮生命周期。
    if flow_status == "FINAL_RESPONSE":
      _log.debug(f"🏁 交付状态确立：在 [{curr_node}] 输出最终文本，成功闭环，挂起等待用户下一次发言。")
      return END
        
    # 判定 2：大模型要求后台静默中转 (INTERIM)
    if flow_status == "INTERIM":
      # 如果大模型犯糊涂，指明了 INTERIM 却不切房间（自循环），强制退出防止死循环
      if next_node == curr_node:
        _log.warning(f"⚠️ 警告：Agent [{curr_node}] 触发了自循环的 INTERIM 中转，为防止后台死循环，强制切断并退出。")
        return END
          
      target_node = "router_node" if next_node == "router" else f"{next_node}_node"
      _log.debug(f"🔄 路由总线触发【后台无感连线】：[{curr_node}] -> [{target_node}]")
      return target_node
        
    # 保底拦截
    return END
  
  def _tool_return_router(self, state: AgentState):
    """工具链处理结束后，顺着 current_agent 母体位置印记精准回归"""
    return f"{state.get('current_agent', 'router')}_node"
  
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
      for msg in state.values["messages"]:
        # 获取基础属性
        content = getattr(msg, 'content', '') or (msg.get('content', '') if isinstance(msg, dict) else '')
        msg_type = getattr(msg, 'type', '') or (msg.get('type', '') if isinstance(msg, dict) else '')
        tool_calls = getattr(msg, 'tool_calls', None) or (msg.get('tool_calls') if isinstance(msg, dict) else None)

        # --- 1. 基础过滤 ---
        if not content or not str(content).strip() or msg_type == "tool" or tool_calls:
          continue

        # --- 2. 角色判定与内容清洗 ---
        if msg_type == "human":
          history.append({"role": "user", "content": content})
          
        elif msg_type == "ai":
          try:
            # 为什么我们还要清洗？纯粹防御性的。。。
            clean_res = re.sub(r'```json\n?|\n?```', '', str(content)).strip()
            js_data = json.loads(clean_res) if clean_res else {}

            if isinstance(js_data, dict) and "reply" in js_data:
              clean_content = js_data.get("reply", "")
              history.append({"role": "assistant", "content": clean_content})
            else:
              history.append({"role": "assistant", "content": str(content)})
        
          except Exception:
            history.append({"role": "assistant", "content": str(content)})
              
    return history
  
  async def stream_chat(self, user_input: str, user_id: str, seq: str, websocket: Any, with_trace: bool = False):
    """
    异步流式对话接口：
    1. 在 updates 模式下，节点执行完才会推送结果，因此 content 是完整的。
    2. 使用正则从 content 中分离出隐藏的 [PRODUCTS_JSON] 块。
    3. answer 字段仅保留纯文字，结构化数据放入 products 字段。
    """
    _log.debug("stream_chat 开始执行, seq: {}, user: {}", seq, user_id)
    
    config_dict = {"configurable": {"thread_id": user_id}}
    
    # 💥【控制重置点】：每轮新开启对话前，将可能残留的决策缓冲区彻底抹掉，洗净图的数据状态
    inputs = {
      "messages": [HumanMessage(content=user_input)],
      "router_decision": None
    }
    
    has_sent_final_answer = False
    final_products = []

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
            # 💥 扩展适配点：只要任何一个专业子 Agent 节点内部触发了 tool_calls 思考，立刻捕获发送思考 Trace
            elif node_name in ["customer_service_node", "points_exchange_node", "goods_exchange_node"]:
              msgs = output.get("messages", [])
              if msgs:
                last_msg = msgs[-1]
                if getattr(last_msg, 'tool_calls', None):
                  trace_msg = {
                    "seq": seq, "type": "chat", "userCode": user_id,
                    "status": "success", "isTrace": True, "answer": "正在思考..."
                  }
            
            if trace_msg:
              await websocket.send_json(trace_msg)

          # --- 2. 处理 Agent nodes 的最终文本输出 ---
          if node_name in ["router_node", "customer_service_node", "points_exchange_node", "goods_exchange_node"]:
            messages = output.get("messages", [])
            if not messages:
              continue
              
            last_msg = messages[-1]
            decision = output.get("router_decision") or {}
            flow_status = decision.get("flow_status", "FINAL_RESPONSE") 
            
            if last_msg.content and not getattr(last_msg, 'tool_calls', None) and flow_status == "FINAL_RESPONSE":
              try:
                # 为什么我们还要清洗？纯粹防御性的。。。
                clean_res = re.sub(r'```json\n?|\n?```', '', str(last_msg.content)).strip()
                contract_data = json.loads(clean_res) if clean_res else {}
                
                display_answer = contract_data.get("reply", "")
                final_products = contract_data.get("products", [])
                has_sent_final_answer = True
                
                _log.debug(f"通过契约成功解析回传文本: {display_answer}")
                await websocket.send_json({
                  "seq": seq,
                  "type": "chat",
                  "userCode": user_id,
                  "status": "success",
                  "isTrace": False,
                  "answer": display_answer
                })
              except Exception as parse_err:
                _log.error(f"stream_chat 实时解析契约 JSON 出现意外错误: {parse_err}")   

      # --- 3. 发送结束信号 (Status: end) ---
      return_products = []
      try:  
        for item in final_products:
          return_products.append({
            "source": "wechat",
            "appid": item.get("outAppId",""),
            "productId": item.get("outId",""),
            "productPromotionLink": item.get("link","")
          })
      except Exception as e:
        _log.error(f"compose products after llm error : {str(e)}")
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
      
      _log.debug(f"send msg back: {msg}")
      await websocket.send_json(msg)

    except Exception as e:
      _log.error("流式对话链路异常: {}", e)
      error_str = str(e)
      if "400" in error_str and "tool_calls" in error_str:
        _log.warning(f"检测到致命协议错误，正在重置用户 {user_id} 的历史记录...")
        try:
          await self.checkpointer.adelete(user_id)
          user_hint = "系统状态已重置，请尝试重新发送您的请求。"
        except Exception as redis_e:
          _log.error(f"重置历史记录失败: {redis_e}")
          user_hint = f"系统繁忙({error_str[:20]})，请稍后再试。"
      else:
        user_hint = f"请求处理异常，请稍后再试。"
      
      await websocket.send_json({
        "seq": seq,
        "type": "chat",
        "userCode": user_id,
        "status": "fail",
        "isTrace": False,
        "errorCode": "500",
        "errorMsg": user_hint
      })
  
  async def close_resource(self):
    """清理资源，在 lifespan 的 yield 之后调用"""
    pass