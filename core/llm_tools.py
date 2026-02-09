from langchain_core.tools import tool
import random

import config.config as config

from core.icbc_db import ICBCVectorDB
import log.logger as logger

_log = logger.get_logger()

# --- 1. 定义工具集 (Tools) ---
# 每个工具都包含详尽的 Docstring，这是大模型理解工具的唯一途径

@tool
def calculate_exchange_value(points: int = 0, rmb_amount: float = 0.0):
  """
  进行工行积分与人民币现金价值的对等换算。
  
  Args:
    points (int): 需要计算价值的工行积分总数。示例：100000。
    rmb_amount (float): 需要折算为积分的现金金额。示例：200.5。
    
  Returns:
    dict: 包含换算结果。
      - result (float/int): 换算后的数值。
      - unit (str): 单位（'元' 或 '积分'）。
      - msg (str): 描述性话术。
  """
  _log.info(f"calculate_exchange_value tool: 计算兑换价值：points={points}, rmb_amount={rmb_amount}")
  EXCHANGE_RATE = config.get_icbc_mall_point_rate() 
  if points > 0:
    val = round(points / EXCHANGE_RATE, 2)
    return {"result": val, "unit": "元", "msg": f"{points}积分约价值{val}元"}
  if rmb_amount > 0:
    pts = int(rmb_amount * EXCHANGE_RATE)
    return {"result": pts, "unit": "积分", "msg": f"{rmb_amount}元商品约需{pts}积分"}
  return {"error": "参数无效，请提供积分或金额"}

@tool
def vector_search_icbc_mall(query: str):
  """
  【核心指令】调用此工具检索工行积分商城中的商品候选列表，此数据库为向量数据库。
  
  重要操作规范：
  1. 语义筛选：返回结果基于向量相似度，可能包含噪音（如搜“电影”出现“肯德基”）。你必须作为审计员，剔除任何不符合用户意图的商品。

  Args:
    query (str): 用户的原始需求、意图关键词或具体的商品名称。
    
  Returns:
    list[dict]: 商品字典列表。每个字典包含:
      - name (str): 商品官方全称。
      - points (int): 兑换该商品所需的工行积分数。
      - distance (float): 语义距离（仅供你参考相关度，越小越相关）。
  """
  _log.info(f"vector_search_icbc_mall tool: 搜索工行积分商城，查询语句：{query}")
  icbc_db = ICBCVectorDB()
  results = icbc_db.search(query, limit=3)
  
  """
  # 定义相似度阈值
  # 对于 DashScope 向量模型，通常 distance < 0.4 或 0.5 属于比较相关的范围
  # 你可以根据实际测试结果微调这个数字
  DISTANCE_THRESHOLD = 0.5
  
  output = []
  for item in results:
    # 你需要根据 distance 来判断这个商品是否真正相关
    # 如果 distance 超过阈值，说明这个商品可能只是表面相关，但实际上不符合用户需求
    if item["distance"] <= DISTANCE_THRESHOLD:
      output.append(item)
  
  return output
  """
  return results

@tool
def search_jd_promotion(keyword: str):
  """
  在京东平台搜索指定商品的同款，并获取实时价格和带佣金的推广链接。
  
  Args:
    keyword (str): 要在京东比价的精确商品名称。
    
  Returns:
    dict: 京东数据。
      - sku_name (str): 京东商品名。
      - price (float): 券后到手价。
      - promo_link (str): 推广URL。
      - source (str): 商品来源。
    None: 若无精确匹配则返回 None。
  """
  _log.info(f"search_jd_promotion tool: 搜索京东，关键词：{keyword}")
  
  # 模拟纯净的京东数据库：只包含京东本身的数据
  jd_database = [
    {"name": "霸王茶姬代金券20元", "price": 20.0},
    {"name": "禧天龙保鲜盒两件套H80407", "price": 18.9},
    {"name": "特来电500元余额充值", "price": 500.0},
    {"name": "小米米家桌面暖风机", "price": 89.0},
    {"name": "雪碧 含糖雪碧 200mlx12罐", "price": 15.9},
    {"name": "奈雪的茶代金券10元", "price": 6.6},
    {"name": "华为Mate 60 Pro", "price": 5499.0}
  ]

  match = next((item for item in jd_database if keyword in item["name"]), None)
  
  if match:
    return {
      "sku_name": f"京东自营-{match['name']}",
      "price": match["price"],
      "promo_link": f"https://u.jd.com/p?k={keyword}",
      "source": "JD_MALL" # 明确来源，不包含任何建议
    }
  
  return None
  
@tool
def get_points_activities(gap_points: int = 0):
  """
  获取工行积分的积累攻略、官方活动详情及快速攒分建议。
  
  【触发场景】：
  1. 用户主动询问“如何获得积分”、“怎么攒分”、“有什么活动”。
  2. 用户积分不足以兑换目标商品时，用于计算补齐方案。
  3. 寻求积分最大化积累策略时。

  Args:
    gap_points (int): 可选。用户当前缺少的积分差额。若为 0 则返回常规积累攻略。
    
  Returns:
    str: 包含具体活动名称、奖励额度及操作建议的详细话术。
  """
  _log.info(f"get_points_activities tool: 获取积分活动，gap_points={gap_points}")
  # 模拟从数据库或配置中读取的最新活动信息
  strategies = [
    "【日常必备】手机银行‘任务中心’：每日签到、浏览产品可得 100-500 积分。",
    "【高额奖励】‘工行月月刷’：信用卡消费达标，单月最高获 5 万积分奖励。",
    "【低门槛】‘微信支付/支付宝立减金转积分’：部分商户活动可通过消费返分。",
    "【运动达人】‘步数换积分’：通过手机银行同步步数，每日可兑换少量积分。"
  ]
  
  # 在工具返回中植入汇率概念
  rate = config.get_icbc_mall_point_rate()
  rate_info = f"\n注：当前工行积分精算基准为 {rate} 积分 = 1 元。"
  strategy_text = "\n".join(strategies) + rate_info
  
  if gap_points <= 0:
    return f"为您汇总了当前主流攒分方案：\n{strategy_text}"
  
  if gap_points < 10000:
    return f"您的积分缺口较小（{gap_points}分），建议：\n1. 连续签到一周 \n2. 参加‘步数换积分’活动，可快速补齐。"
  
  return f"您的缺口较大（{gap_points}分），建议重点关注：\n1. ‘工行月月刷’活动（最高5万分）\n2. 办理特定多倍积分信用卡。"