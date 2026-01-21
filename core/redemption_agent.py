import operator
import sqlite3
from typing import Annotated, List, TypedDict, Dict, Literal, Optional, Any
from langgraph.checkpoint.memory import MemorySaver

from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver
from pydantic import BaseModel, Field, field_validator

import config.config as config
import config.resource as resource
from core import model_factory
from core.icbc_db import ICBCVectorDB
import log.logger  as logger

_log = logger.get_logger()


class IntentAnalysis(BaseModel):
  product_keywords: str = Field(description="提取的商品关键词，若无则为空字符串")
  search_terms: List[str] = Field(description="用户意图的关键词列表，用来进行向量数据库搜索，若无则为空列表")
  user_points: int = Field(
    default=-1, 
    description="用户明确提到的积分数。若未提到，设为 -1 且必须在 missing_info 中加入'积分数'，严禁返回空字符串或者null。若用户明确说没有积分或只有0分，设为 0。"
  )
  missing_info: List[str] = Field(description="缺失的信息项列表")
  reply: str = Field(description="给用户的追问话术或引导语")
  
  @field_validator('user_points', mode='before')
  @classmethod
  def handle_empty_points(cls, v: Any) -> Any:
    # 如果混元返回了空字符串 "" 或 None，强制转为默认值 -1
    if v == "" or v is None:
      return -1
    return v

# --- 1. 定义状态数据结构 ---
class AgentState(TypedDict):
  # 使用 Annotated 和 operator.add 自动累加对话历史
  messages: Annotated[List[BaseMessage], operator.add]
  user_points: int            # 用户当前积分
  product_keywords: str       # 提取的商品名
  icbc_info: Dict             # 工行查询结果
  jd_info: Dict               # 京东查询结果
  missing_info: List[str]     # 缺失的关键信息
  final_recommendation: str   # 最终建议

# --- 2. 核心智能体类 ---
class RedemptionAgent:
  def __init__(self, db_path: str = "memory.db"):
    #self.model = ChatOpenAI(
    #  model=config.get_deepseek_model(), 
    #  api_key=config.get_deepseek_api_key(), # 这里填入刚刚申请的 Key
    #  base_url=config.get_deepseek_base_url(),
    #  temperature=0
    #)
    
    #self.model = ChatOpenAI(model="gpt-4o", temperature=0)
    self.model = model_factory.get_model()
    self.structured_llm = self.model.with_structured_output(IntentAnalysis)
    
    # 初始化持久化记忆（使用 SQLite 存储对话状态）
    #conn = sqlite3.connect(db_path, check_same_thread=False)
    #self.checkpointer = SqliteSaver(conn)
    self.checkpointer = MemorySaver()
    
    # 编译工作流
    self.app = self._build_workflow().compile(checkpointer=self.checkpointer)
    
    self.idb = ICBCVectorDB(api_key=config.get_qwen_api_key())

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
    
    res = chain.invoke({"input": last_message, "chat_history": history})
    _log.debug("analyze result: %s", res)
    _log.debug("previous state: %s", state)
    
    ret = {
      "product_keywords": res.product_keywords or state.get("product_keywords"),
      "user_points": res.user_points or state.get("user_points", 0),
      "missing_info": res.missing_info
    }
    
    if res.reply != "":
      ret["final_recommendation"] = res.reply
     
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
    
    
    # 在京东上找同类商品（模拟）
    if icbc_info:
      # 如果icbc有商品，那么使用这个商品的名字去京东搜更准确。
      jd_query = icbc_info["name"]
    else:
      # 如果icbc没有货，那么使用用户的关键词去京东搜，用来推荐。
      jd_query = suggested[0] if suggested else keywords
      
    #do jd query here
    jd_res = self._call_jd_api(jd_query)
      
    return {
      "icbc_info": icbc_info,
      "jd_info": jd_res
    }

  # --- 节点 C: 比价决策 ---
  def _compare_and_decide(self, state: AgentState):
    icbc = state.get("icbc_info")
    jd = state.get("jd_info")
    user_pts = state.get("user_points", 0)
    
    report = []
    
    # 场景 1：工行没货
    if not icbc:
      report.append(f"🔍 搜索情况：在工行积分商城暂未找到与“{state['product_keywords']}”直接匹配的礼品。")
      report.append(f"🛒 替代方案：我在京东为您找到了“{jd['name']}”，价格为 ￥{jd['price']}。")
      report.append(f"🔗 购买链接：{jd['url']}")
      final_rec = "\n".join(report)
      
    # 场景 2：工行有货，进行对比
    else:
      icbc_pts = icbc["points"]
      jd_price = jd["price"]
      # 500积分 = 1元
      icbc_value_in_cash = icbc_pts / 500
      
      report.append(f"🔍 搜索情况：为您找到了工行商城的“{icbc['name']}”（{icbc_pts}积分）以及京东的同款商品。")
      
      if jd_price < icbc_value_in_cash:
        diff_pts = int(icbc_pts - jd_price * 500)
        report.append(f"💡 对比结果：京东的价格更划算（￥{jd_price}）。")
        report.append(f"✅ 建议：换购京东E卡下单，可比直兑省下约 {diff_pts} 积分。")
        report.append(f"🔗 京东链接：{jd['url']}")
      else:
        report.append(f"💡 对比结果：工行商城的积分兑换价优于京东（京东价 ￥{jd_price}）。")
        report.append(f"✅ 建议：直接在工行商城使用积分兑换。")
      
      final_rec = "\n".join(report)

    return {"final_recommendation": final_rec}

    pts = state["icbc_info"]["points"]
    price = state["jd_info"]["price"]
    # 汇率计算：假设 500积分 = 1元
    icbc_value = pts / 500 
    
    if price < icbc_value:
      rec = f"【省钱建议】换京东E卡更划算！京东仅需￥{price}（折合{price*500}积分），比工行商城直兑节省{pts - price*500}积分。"
    else:
      rec = "【推荐直兑】工行商城积分价更优，建议直接下单。"
      
    return {"final_recommendation": rec}

  # --- 节点 D: RAG 攒分攻略 ---
  def _rag_strategy(self, state: AgentState):
    # 模拟 RAG 检索
    target_pts = state["icbc_info"]["points"] if state.get("icbc_info") else state["jd_info"]["price"] * 500
    gap = int(target_pts - state["user_points"])
    
    strategy = f"由于您积分缺口较大({gap}分)，建议：1. 参加本月'爱购周末'餐饮5倍积分；2. 绑定微信支付首刷送2000分。"
    return {"final_recommendation": state["final_recommendation"] + "\n\n" + strategy}

  # --- 路由逻辑 ---
  def _router(self, state: AgentState) -> Literal["ask_more", "search", "rag", "end"]:
    if state.get("missing_info"):
      return "ask_more"
  
    # 如果还没查过京东/工行，去搜索
    if not state.get("jd_info"):
      return "search"
    
    # 关键逻辑：无论有没有工行商品，只要用户积分 < (工行所需积分 或 目标价值所需积分)
    # 这里假设即便工行没货，我们也拿京东价格折算的积分作为目标
    target_pts = state["icbc_info"]["points"] if state.get("icbc_info") else state["jd_info"]["price"] * 500
    
    if state["user_points"] < target_pts:
      return "rag"
      
    return "end"

    if state["missing_info"]:
      return "ask_more"
    
    # 如果还没查过价格，去搜索
    if not state.get("icbc_info"):
      return "search"
    
    # 如果积分不够，去 RAG
    if state["user_points"] < state["icbc_info"]["points"]:
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
      return "为了给您精准推荐，请问您大概有多少积分？或者具体的商品名称是什么？"

