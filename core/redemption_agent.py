import operator
import re
import redis
from typing import Annotated, List, TypedDict, Dict, Literal, Optional, Any
from langgraph.checkpoint.memory import MemorySaver
from langgraph.checkpoint.redis import RedisSaver
from langgraph.checkpoint.redis import ShallowRedisSaver

from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langgraph.graph import StateGraph, END
#from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, Field, field_validator

import config.config as config
import config.resource as resource
from core import model_factory
from core.icbc_db import ICBCVectorDB
import log.logger  as logger
from core.icbc_points import icbc_points_to_cash, cash_to_icbc_points
from core.jd_api import JDUnionClient
from core.simple_redis_saver import SimpleRedisSaver
import json

_log = logger.get_logger()

# 2 个空格对齐
from typing import List, Any, Union
from pydantic import BaseModel, Field, field_validator

class IntentAnalysis(BaseModel):
  product_keywords: str = Field(description="提取的商品关键词，若无则为空字符串")
  search_terms: List[str] = Field(description="用户意图的关键词列表，用来进行向量数据库搜索，若无则为空列表")
  user_points: int = Field(
    default=-1, 
    description="用户当前积分，若无则为 -1"
  )
  missing_info: List[str] = Field(description="缺失的信息项列表")
  reply: str = Field(description="给用户的追问话术或引导语")
  
  @field_validator('product_keywords', mode='before')
  @classmethod
  def handle_list_or_none(cls, v: Any) -> str:
    # 核心修复：如果模型调皮返回了 [] (list) 或 None，强制转为 ""
    if isinstance(v, list):
      return ", ".join(v) if v else ""
    if v is None:
      return ""
    return str(v)

  @field_validator('user_points', mode='before')
  @classmethod
  def handle_empty_points(cls, v: Any) -> Any:
    if v == "" or v is None:
      return -1
    return v

# --- 1. 定义状态数据结构 ---
class AgentState(TypedDict):
  # 使用 Annotated 和 operator.add 自动累加对话历史
  messages: Annotated[List[BaseMessage], operator.add]
  user_points: int            # 用户当前积分
  product_keywords: str       # 提取的商品名
  search_terms: List[str]   # 建议的搜索关键词列表
  icbc_info: Optional[Dict]             # 工行查询结果
  jd_info: Optional[Dict]               # 京东查询结果
  jd_candidates: List[Dict] # 新增：存储京东返回的多个候选项（用于前端展示）
  missing_info: List[str]     # 缺失的关键信息
  final_recommendation: str   # 最终建议

# --- 2. 核心智能体类 ---
class RedemptionAgent:
  def __init__(self):
    self.model = model_factory.get_model()
    self.structured_llm = self.model.with_structured_output(IntentAnalysis)
    
    # 2. 初始化 Redis 客户端
    # 注意：decode_responses 必须为 False
    self.redis_client = redis.Redis(
      host=config.get_redis_host(),
      port=config.get_redis_port(),
      db=0,
      decode_responses=False
    )
    
    # 3. 实例化我们的自定义 Saver
    # ttl 单位为秒，例如 86400 是 24 小时
    self.checkpointer = SimpleRedisSaver(
      redis_client=self.redis_client,
      ttl=config.get_redis_msg_ttl_in_seconds()
    )
    
    # 编译工作流
    self.app = self._build_workflow().compile(checkpointer=self.checkpointer)
    self.idb = ICBCVectorDB(api_key=config.get_qwen_api_key())
    self.jd_client = JDUnionClient()

  # --- 节点 A: 意图与实体解析 ---
  def _analyze_intent(self, state: AgentState):
    prompt = ChatPromptTemplate.from_messages([
      ("system", resource.get_resource()["default_values"]["analyze_intent_system_prompt"]),
      # 这里是关键：要把历史消息 state["messages"] 传给模型，它才知道第一轮说了什么
      ("placeholder", "{chat_history}"),
      ("human", "{input}")
    ])
    chain = prompt | self.structured_llm
    
    last_message = state["messages"][-1].content
    history = state["messages"][:-1]
    
    # 关键改进：添加错误重试逻辑和 fallback 机制
    max_retries = 3
    res = None
    for attempt in range(max_retries):
      try:
        raw_res = chain.invoke({"input": last_message, "chat_history": history})
        # 情况 A：模型返回的是字符串（包含废话）
        if isinstance(raw_res, str):
          # 使用正则提取最外层的大括号内容
          match = re.search(r"\{.*\}", raw_res, re.DOTALL)
          if match:
            json_str = match.group(0)
            # 手动解析并验证
            res_dict = json.loads(json_str)
            res = IntentAnalysis(**res_dict)
          else:
            raise ValueError("未找到合法的 JSON 结构")
        
        # 情况 B：模型返回的是 IntentAnalysis 对象（正常情况）
        else:
          res = raw_res
        
        _log.debug("analyze result: %s", res)
        _log.debug("previous state: %s", state)
        break
      except Exception as e:
        _log.warning(f"Attempt {attempt+1} failed to parse LLM response: {str(e)}")
        if attempt == max_retries - 1:
          # ✅ 最后一次失败时，返回默认响应而不是崩溃
          _log.error(f"Max retries ({max_retries}) reached, using fallback response")
          res = IntentAnalysis(
            product_keywords="",
            search_terms=[],
            user_points=-1,
            missing_info=["具体品类", "积分数"],
            reply="抱歉，我没有理解您的需求。请告诉我您想要什么商品，以及您大概有多少积分？"
          )
          break
    
    # 修正 3：记忆缝合逻辑
    # 如果本轮 res 有值则更新，否则保留 state 里的旧值
    current_keywords = res.product_keywords if res.product_keywords else state.get("product_keywords", "")
    current_search_terms = res.search_terms if res.search_terms else state.get("search_terms", [])
    
    # 积分处理：-1 代表本轮没提到，则沿用旧分
    current_points = res.user_points if res.user_points >= 0 else state.get("user_points", 0)
    
    ret = {
      "product_keywords": current_keywords,
      "search_terms": current_search_terms,
      "user_points": current_points,
      "missing_info": res.missing_info,
      "icbc_info": None,
      "jd_info": None,
    }
    
    if res.reply:
      ret["final_recommendation"] = res.reply
      ret["messages"] = [AIMessage(content=res.reply)]
    else:
      ret["messages"] = []
     
    return ret

  # --- 节点 B: 查询工行与京东 ---
  def _market_search(self, state: AgentState):
    keywords = state["product_keywords"]
    suggested = state.get("search_terms", [])
    
    search_items = [keywords] if not suggested else suggested
    
    results = []
    for item in search_items:
      icbc_res = self.idb.search(item)
      if icbc_res:
        results.append(icbc_res)
    
    best_match = min(results, key=lambda x: x["distance"]) if results else None
    
    icbc_info = None
    if best_match and best_match.get("distance", 2.0) < 1.1:
      icbc_info = {"name": best_match["name"], "points": best_match["points"]}
    
    # 2. 京东搜索逻辑：调用 get_best_promotion_items
    jd_query = icbc_info["name"] if icbc_info else (suggested[0] if suggested else keywords)
    _log.info(f"正在京东搜索并转链: {jd_query}")
    
    # 获取前 3 个最匹配且已转链的商品
    jd_list = self.jd_client.get_best_promotion_items(jd_query, top_k=3)
    
    # 3. 决策策略：选出第一个作为“官方对比项”
    best_jd = jd_list[0] if jd_list else {"name": jd_query, "price": 0.0, "url": "https://www.jd.com"}
    
    return {
      "icbc_info": icbc_info,
      "jd_info": best_jd,
      "jd_candidates": jd_list # 存储所有结果
    }
    
  # --- 节点 C: 比价决策 ---
  def _compare_and_decide(self, state: AgentState):
    icbc = state.get("icbc_info")
    jd = state.get("jd_info") # 这是我们在上面选出的 best_jd
    jd_candidates = state.get("jd_candidates", [])
    
    report = []
    
    # 场景 A：工行没货
    if not icbc:
      report.append(f"🔍 搜索情况：在工行积分商城暂未找到直接匹配的礼品。")
      report.append(f"🛒 我在京东为您找到了以下方案：")
    # 场景 B：工行有货
    else:
      icbc_pts = icbc["points"]
      jd_price = jd["price"]
      icbc_value_in_cash = icbc_points_to_cash(icbc_pts)
      
      report.append(f"🔍 搜索情况：为您找到了工行“{icbc['name']}”（{icbc_pts}积分）以及京东的同款。")
      
      if jd_price < icbc_value_in_cash:
        diff_pts = int(icbc_pts - cash_to_icbc_points(jd_price))
        report.append(f"💡 对比结果：京东价格（￥{jd_price}）更划算，建议换购京东E卡下单，可省约 {diff_pts} 积分。")
      else:
        report.append(f"💡 对比结果：工行商城积分兑换更优。")
      
      report.append(f"\n🛍️ 更多京东购买选项：")

    # 遍历展示所有的京东候选项
    for item in jd_candidates:
      report.append(f"• **{item['name']}**")
      report.append(f"  价格: ￥{item['price']}  [点击直达领券]({item['url']})")

    return {"final_recommendation": "\n".join(report)}

  # --- 节点 D: RAG 攒分攻略 ---
  def _rag_strategy(self, state: AgentState):
    #1. 确定目标积分
    if state.get("icbc_info"):
      target_pts = state["icbc_info"]["points"]
      product_name = state["icbc_info"]["name"]
    else:
      # 兜底：如果工行没货，按京东价格折算积分目标 (500积分=1元)
      target_pts = cash_to_icbc_points(state["jd_info"]["price"])
      product_name = state["jd_info"]["name"]
      
    # 2. 计算缺口
    user_pts = state.get("user_points", 0)
    gap = int(target_pts - user_pts)
    
    # 3. 检索原始策略 (直接从向量库获取内容)
    # 搜索词使用商品关键词或通用攒分词
    search_query = state.get("product_keywords", "积分活动")
    raw_strategies = self.idb.search_strategy(search_query, limit=2)
    
    # 4. 直接拼接字符串展示给用户
    strategy_header = f"\n\n💡 **工行攒分攻略** (目标:{product_name})\n"
    strategy_header += f"您当前积分为 {user_pts}，距离兑换还差 **{gap}** 分。为您推荐以下路径：\n"
    
    if not raw_strategies:
      strategy_body = "• 目前暂无特定加速活动，建议通过日常刷卡积累（1元积1分）。"
    else:
      strategy_body = ""
      for i, s in enumerate(raw_strategies):
        # s['content'] 是你在 add_strategies 时存入的原始文本
        strategy_body += f"{i+1}. {s['content']}\n"
    
    return {
      "final_recommendation": state["final_recommendation"] + strategy_header + strategy_body
    }
    
    # 1. 计算目标积分和缺口
    #if state.get("icbc_info"):
    #  target_pts = state["icbc_info"]["points"]
    #  product_name = state["icbc_info"]["name"]
    #else:
      # 如果工行没货，按京东价格折算积分目标
    #  target_pts = cash_to_icbc_points(state["jd_info"]["price"])
    #  product_name = state["jd_info"]["name"]
      
    #gap = int(target_pts - state["user_points"])
    
    # 2. 调用向量库的 search_strategy 获取原始攻略
    # 搜索词可以结合“缺口积分”和“商品名称”，增加检索相关度
    #search_query = f"如何快速获得 {gap} 积分兑换 {product_name}"
    #raw_strategies = self.idb.search_strategy(search_query, limit=2)
    
    #if not raw_strategies:
    #  strategy_text = f"目前没有找到特定的加速活动，建议通过日常刷卡积累，每消费 1 元积 1 分。"
    #else:
      # 3. 将检索到的原始片段交给 LLM 进行个性化汇总
      # 提取 content 组成上下文
    #  context = "\n".join([f"- {s['content']}" for s in raw_strategies])
      
    #  prompt = f"""
    #  你是一个专业的工行信用卡积分顾问。
    #  用户想要兑换“{product_name}”，目前还差 {gap} 积分。
      
    #  请根据以下检索到的积分攻略，为用户提供具体的、带有计算过程的建议：
    #  {context}
      
    #  要求：
    #  1. 语言亲和且口语化。
    #  2. 告诉用户具体需要消费多少钱或者参加什么活动能填平这 {gap} 分。
    #  3. 保持简洁，不超过 100 字。
    #  """
      
    #  llm_res = self.model.invoke(prompt)
    #  strategy_text = llm_res.content

    #return {
    #  "final_recommendation": state["final_recommendation"] + "\n\n💡 **专属攒分攻略：**\n" + strategy_text
    #}

  # --- 路由逻辑 ---
  def _router(self, state: AgentState) -> Literal["ask_more", "search", "rag", "end"]:
    if state.get("missing_info"):
      return "ask_more"
  
    # 如果还没查过京东/工行，去搜索
    if not state.get("jd_info"):
      return "search"
    
    # 关键逻辑：无论有没有工行商品，只要用户积分 < (工行所需积分 或 目标价值所需积分)
    # 这里假设即便工行没货，我们也拿京东价格折算的积分作为目标
    target_pts = state["icbc_info"]["points"] if state.get("icbc_info") else cash_to_icbc_points(state["jd_info"]["price"])
    
    if state["user_points"] < target_pts:
      return "rag"
      
    return "end"

  # --- 构建工作流图 ---
  def _build_workflow(self):
    workflow = StateGraph(AgentState)
    
    workflow.add_node("analyze", self._analyze_intent)
    workflow.add_node("market_search", self._market_search)
    workflow.add_node("decide", self._compare_and_decide)
    workflow.add_node("rag_strategy", self._rag_strategy)
    
    workflow.set_entry_point("analyze")
    
    workflow.add_conditional_edges(
      "analyze", 
      self._router, 
      {"ask_more": END, "search": "market_search"}
    )
    workflow.add_edge("market_search", "decide")
    workflow.add_conditional_edges(
      "decide", 
      self._router, 
      {"rag": "rag_strategy", "end": END}
    )
    workflow.add_edge("rag_strategy", END)
    
    return workflow

  # --- 对外统一接口 ---
  def chat(self, user_input: str, thread_id: str):
    config = {"configurable": {"thread_id": thread_id}}
    
    # 运行图流
    events = self.app.invoke(
      {"messages": [HumanMessage(content=user_input)]}, 
      config
    )
    
    # 返回最后的建议或者追问
    if events.get("final_recommendation"):
      return events["final_recommendation"]
    else:
      return "为了给您精准推荐，请问您大概有多少积分？以及具体的商品名称是什么？"

