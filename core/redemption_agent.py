import operator
import sqlite3
from typing import Annotated, List, TypedDict, Dict, Literal, Optional
from langgraph.checkpoint.memory import MemorySaver

from langchain_openai import ChatOpenAI
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.output_parsers import JsonOutputParser
from langgraph.graph import StateGraph, END
from langgraph.checkpoint.sqlite import SqliteSaver

import config.config as config
import config.resource as resource
from core import model_factory
import log.logger  as logger

_log = logger.get_logger()


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
    
    # 初始化持久化记忆（使用 SQLite 存储对话状态）
    #conn = sqlite3.connect(db_path, check_same_thread=False)
    #self.checkpointer = SqliteSaver(conn)
    self.checkpointer = MemorySaver()
    
    # 编译工作流
    self.app = self._build_workflow().compile(checkpointer=self.checkpointer)

  # --- 节点 A: 意图与实体解析 ---
  def _analyze_intent(self, state: AgentState):
    prompt = ChatPromptTemplate.from_messages([
      ("system", resource.get_resource()["default_values"]["analyze_intent_system_prompt"]),
      # 这里是关键：要把历史消息 state["messages"] 传给模型，它才知道第一轮说了什么
      ("placeholder", "{chat_history}"),
      ("human", "{input}")
    ])
    chain = prompt | self.model | JsonOutputParser()
    
    last_message = state["messages"][-1].content
    history = state["messages"][:-1]
    
    res = chain.invoke({"input": last_message, "chat_history": history})
    _log.debug("analyze result: %s", res)
    _log.debug("previous state: %s", state)
    
    ret = {
      "product_keywords": res.get("product_keywords", state.get("product_keywords")),
      "user_points": res.get("user_points") or state.get("user_points", 0),
      "missing_info": res.get("missing_info", [])
    }
    
    if res.get("reply") != "":
      ret["final_recommendation"] = res.get("reply")
     
    return ret

  # --- 节点 B: 查询工行与京东 (模拟 API) ---
  def _market_search(self, state: AgentState):
    # 实际开发中此处调用工行商城 API 和 京东联盟 API
    item = state["product_keywords"]
    icbc_points = 50000  # 假设模拟数据
    jd_price = 80        # 假设模拟数据
    
    return {
      "icbc_info": {"name": item, "points": icbc_points},
      "jd_info": {"price": jd_price, "url": "https://jd.com/item_id"}
    }

  # --- 节点 C: 比价决策 ---
  def _compare_and_decide(self, state: AgentState):
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
    gap = state["icbc_info"]["points"] - state["user_points"]
    strategy = f"由于您积分缺口较大({gap}分)，建议：1. 参加本月'爱购周末'餐饮5倍积分；2. 绑定微信支付首刷送2000分。"
    return {"final_recommendation": state["final_recommendation"] + "\n\n" + strategy}

  # --- 路由逻辑 ---
  def _router(self, state: AgentState) -> Literal["ask_more", "search", "rag", "end"]:
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

