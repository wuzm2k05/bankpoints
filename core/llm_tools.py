# 2 个空格对齐
from langchain_core.tools import tool
import random
import httpx # 建议用于异步 HTTP 请求
from loguru import logger as _log

import config.config as config
from core.icbc_db import ICBCVectorDB

# --- 1. 定义工具集 (Tools) ---


#@tool
#async def get_ecard_voucher_rules():
#  """
#  获取工银i豆兑换现金等价物（立减金、京东E卡）的基准兑换比率及基本说明。
#  
#  用途：
#  - 获取立减金(Voucher)的兑换比率（voucher_rate）。
#  - 获取京东E卡的比价指引（ecard_benchmark）。
#  - 获取立减金与E卡的使用范围差异说明（note）。
#  """
#  _log.debug("get_ecard_voucher_rules tool: 获取工银i豆兑换规则")
#  return {
#    "voucher_rate": config.get_icbc_voucher_rate(), 
#    "ecard_benchmark": "请通过 vector_search_icbc_mall('京东E卡') 获取实时E卡兑换比率，通常优于立减金",
#    "note": "立减金可用于京东所有商品(含第三方)；京东E卡仅限京东自营。"
#  }

@tool
async def vector_search_wechat_products(query: str):
  """
  【核心指令】调用此工具检索微信小店商城中的商品候选列表，此数据库为向量数据库。
  
  重要操作规范：
  1. 语义筛选：返回结果基于向量相似度，可能包含噪音。你必须作为审计员，剔除任何不符合用户意图的商品。

  Args:
    query (str): 用户的原始需求、意图关键词或具体的商品名称。
    
  Returns:
    list[dict]: 商品字典列表。每个字典包含: id,name, price, distance,outId,outAppId,link
      id: 商品的id
      name：商品的名字
      price：商品的价格，单位是分。
      outId: 用来返回给用户的信息。
      outAppId: 用来返回给用户的信息。
      link: 用来返回给用户的信息。
  """
  _log.info("vector_search_icbc_mall tool: 搜索微信小店商品，查询语句：{}", query)
  
  # 假设 ICBCVectorDB 已经支持异步搜索，或者在内部处理了线程池
  icbc_db = ICBCVectorDB()
  results = await icbc_db.asearch_wechat_products(query, limit=3) 
  ret = []
  for item in results:
    ret.append({
      "id": item["id"],
      "price": item["price"],
      "name": item["title"],
      "distance": item["distance"],
      "outId": item["outId"],
      "outAppId": item["outAppId"],
      "link": item["link"]
    })
  
  return ret


@tool
async def vector_search_icbc_mall(query: str):
  """
  【核心指令】调用此工具检索工银i豆商城中的商品候选列表，此数据库为向量数据库。
  
  重要操作规范：
  1. 语义筛选：返回结果基于向量相似度，可能包含噪音。你必须作为审计员，剔除任何不符合用户意图的商品。

  Args:
    query (str): 用户的原始需求、意图关键词或具体的商品名称。
    
  Returns:
    list[dict]: 商品字典列表。每个字典包含: name, points, distance。
  """
  _log.info("vector_search_icbc_mall tool: 搜索工银i豆商城，查询语句：{}", query)
  
  # 假设 ICBCVectorDB 已经支持异步搜索，或者在内部处理了线程池
  icbc_db = ICBCVectorDB()
  results = await icbc_db.asearch(query, limit=3) 
  
  return results

@tool
async def search_jd_promotion(keyword: str):
  """
  在京东平台搜索指定商品的同款，并获取实时价格的商品链接。
  
  Args:
    keyword (str): 要在京东比价的精确商品名称。
    
  Returns:
    dict: 京东数据。包含 sku_name, price, promo_link, support_ecard 等。
  """
  _log.info("search_jd_promotion tool: 搜索京东，关键词：{}", keyword)
  
  # 模拟异步 IO 操作（实际场景可换成 httpx 请求）
  jd_database = [
    {"name": "霸王茶姬代金券20元", "price": 20.0, "support_ecard": True},
    {"name": "禧天龙保鲜盒两件套H80407", "price": 18.9, "support_ecard": False},
    {"name": "特来电500元余额充值", "price": 500.0, "support_ecard": True},
    {"name": "小米米家桌面暖风机", "price": 89.0, "support_ecard": False},
    {"name": "雪碧 含糖雪碧 200mlx12罐", "price": 15.9, "support_ecard": False},
    {"name": "奈雪的茶代金券10元", "price": 6.6, "support_ecard": True},
    {"name": "华为Mate 60 Pro", "price": 5499.0, "support_ecard": True}
  ]

  match = next((item for item in jd_database if keyword in item["name"]), None)
  
  if match:
    return {
      "sku_name": f"京东自营-{match['name']}",
      "price": match["price"],
      "promo_link": f"https://u.jd.com/p?k={keyword}",
      "source": "JD_MALL",
      "support_ecard": match["support_ecard"]
    }
  
  return None
  
@tool
async def get_points_activities(gap_points: int = 0):
  """
  获取工银i豆的积累攻略、官方活动详情及快速攒豆建议。
  """
  _log.info("get_points_activities tool: 获取活动，gap_points={}", gap_points)
  
  strategies = [
    "【日常必备】手机银行‘任务中心’：每日签到可得 100-500 工银i豆。",
    "【高额奖励】‘工行月月刷’：信用卡消费达标，最高获 5 万工银i豆。",
    "【运动达人】‘步数换i豆’：手机银行同步步数兑换。"
  ]
  
  strategy_text = "\n".join(strategies)
  
  if gap_points <= 0:
    return f"为您汇总了当前主流攒豆方案：\n{strategy_text}"
  
  if gap_points < 10000:
    return f"您的缺口较小（{gap_points}豆），建议：\n1. 连续签到一周 \n2. 参加‘步数换i豆’。"
  
  return f"您的缺口较大（{gap_points}豆），建议关注：\n1. ‘工行月月刷’活动 \n2. 办理特定多倍i豆信用卡。"

@tool
async def query_icbc_voucher_rules(query: str) -> str:
  """
  【业务工具：工行立减金/微信立减金规则查询】
  
  用途：
  当用户咨询工行微信立减金的业务逻辑、兑换流程、兑换比例、报错码（如12003832）、有效期、补发规则时调用。也包括了i豆兑换实物的兑换途径。
  支持模糊语义搜索和错误码精确匹配。

  返回内容说明：
  返回格式为多条业务规则列表。每条规则包含：
  - 【可信度】：基于向量距离计算的相关性评价（高度相关/相关/参考信息）。
  - 【标准问题】：知识库中记录的原始问题。
  - 【规则内容】：该问题的标准官方解答。
  """
  try:
    _log.info("query_icbc_voucher_rules tool: 执行精算级检索, query={}", query)
    db = ICBCVectorDB()
    
    # 1. 调用增强后的异步检索（建议 limit 增加到 3，给模型更多上下文）
    # 注意：此时 asearch_voucher_info 返回的是 List[Dict]
    items = await db.asearch_voucher_info(query, limit=3)
    
    if not items:
      return "【结果】: 知识库中未匹配到相关规则。请告知用户：'暂未查到该问题的具体规定，建议核实e支付状态或咨询人工客服'。"
      
    # 2. 格式化检索结果，带上语义距离（Distance）
    # 告诉大模型哪些是“强相关”，哪些是“参考”
    formatted_results = []
    for item in items:
      dist = item.get("distance", 1.0)
      # 语义距离在 Chroma 中通常 0.2 以内极准，0.5 以上开始偏移
      reliability = "高度相关" if dist < 0.4 else "相关" if dist < 0.6 else "参考信息"
      
      content = (
        f"【可信度】: {reliability} (距离:{dist:.4f})\n"
        f"【标准问题】: {item.get('question', '未知')}\n"
        f"【规则内容】: {item.get('content', '')}"
      )
      formatted_results.append(content)
      
    context = "\n\n---\n\n".join(formatted_results)
    
    # 3. 在返回给大模型的内容中加入引导指令
    return (
      f"为您找到以下立减金业务规则：\n\n{context}\n\n"
      f"【指令】: 请优先根据'高度相关'的内容回答。如果所有内容距离均大于 0.6，"
      f"请委婉告知用户可能无法准确回答，并提供通用性建议。"
    )
    
  except Exception as e:
    _log.error("查询立减金规则工具执行失败: {}", str(e))
    return f"【工具报错】: 内部执行异常，请尝试根据常识回答或引导人工。"