# 2 个空格对齐
from typing import Dict, List

from langchain_core.tools import tool
from loguru import logger as _log

import config.config as config
from core.icbc_db import ICBCVectorDB
from core.voucher_order import VoucherOrder

# --- 1. 定义工具集 (Tools) ---
@tool
def create_voucher_order(total_amount: int, vouchers: List[Dict]) -> str:
  ...


@tool
def query_voucher_order_status(order_code: str) -> str:
  """
  查询工行立减金兑换订单的实时状态和发放详情。
  
  入参说明:
    order_code (str): 订单编码，通常是一串由数字和字母组成的长字符串。例如："e7164e61d584613960fd49e11ebfa68","400073730129000772605140951276"等。

  返回内容示例：
    "查询成功。订单编码:2039dfee008d40549c6f5764ad21b28c;订单状态:立减金发放成功;兑换金额:3元;应发:3张1元;实发:3张1元;券状态:(168393866353已实扣,168396062864已实扣,168395241324已实扣)"
    "查询结果：订单不存在，请核对订单号是否正确。"
    "查询失败：失败原因"
    
    注意：
      订单状态说明：
      -  等待银行支付结果： 已下单等待用户支付，用户支付成功后银行支付系统会回调平台接口发送支付信息，该状态表示还没有收到银行支付系统的回调。
      - 订单已过期：用户下单后平台会在银行支付系统形成对应的“支付订单”，有效期为30分钟，用户需在30分钟内操作支付。订单已过期表明用户在支付单有效期内没有成功支付。
      - 立减金发放失败：用户成功支付，平台收到银行支付系统的回调，但发放微信立减金失败，后续平台会进行自动补发。
      - 立减金补发失败：平台自动补发也失败了。
      -立减金发放成功：立减金发放成功，用户可在微信卡包查看。
      
      券状态说明：
      - 未使用，立减金已发放但未使用
      - 已使用，立减金已发放且已使用
      - 已过期，立减金已发放但过了10天有效期
      - 已回收，立减金过期未使用被收回
      - 已失效，立减金因更换实名等原因不再可用
      
  调用要求：
    1. 拿到返回字符串后，请发挥你的语义理解能力，提取出‘订单状态’和‘兑换金额’等关键信息，并以友好的 Markdown 格式呈现给用户。对于订单的状态说明必须严格使用本接口的说明，绝对禁止自己添加任何解释性的文字，以免引起用户误解。
  """
  _log.debug(f"正在查询订单状态: {order_code}")
  voucher_order = VoucherOrder()
  result = voucher_order.query_voucher_order_status(order_code)
  _log.debug(f"订单查询结果: {result}")
  return result
  
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
  _log.debug("vector_search_icbc_mall tool: 搜索微信小店商品，查询语句：{}", query)
  
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
  
  _log.debug("vector_search_wechat_products tool: 搜索到 {} 条结果，返回给模型的内容:\n{}", len(results), ret)
  
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
  _log.debug("vector_search_icbc_mall tool: 搜索工银i豆商城，查询语句：{}", query)
  
  # 假设 ICBCVectorDB 已经支持异步搜索，或者在内部处理了线程池
  icbc_db = ICBCVectorDB()
  results = await icbc_db.asearch(query, limit=3) 
  
  _log.debug("vector_search_icbc_mall tool: 搜索到 {} 条结果，返回给模型的内容:\n{}", len(results), results)
  
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
  _log.debug("search_jd_promotion tool: 搜索京东，关键词：{}", keyword)
  
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
  【业务工具：工行i豆与微信立减金综合规则及售后政策检索】
  
  用途：
  当用户咨询任何与工行i豆、微信立减金相关的业务规则、系统报错、额度限制及售后对账问题时调用。
  
  大模型触发场景提示：
  1. 渠道与流程：查询i豆商城入口、微信立减金兑换流程、e支付开通/重置密码指南。
  2. 核心规则：立减金有效期（10天）、面额选择（1/10/100元）、单笔最大叠加数（8张）、满减门槛（过面额一分钱）。
  3. 限额与风控：每月上限5000元或单批次60笔导致的“发放失败”、“数量不对”、“提取失败”及“当日早中晚人工补发”机制。
  4. 待发与延期：如何利用拼乐小程序购买“自行提取”来延长立减金有效期、待发金额如何提取。
  5. 售后与对账：实名认证变更券失效、购物退款（券过期不退）、e支付和i豆明细对账、如何截图利用微信识别文字功能复制订单号、人工客服热线（4006-705-057）及工作时间。
  
  参数规范：
  - query (str): 用户的原始问题或业务核心关键词（例如：“换实名立减金还在吗”、“月额度超了怎么补发”、“e支付怎么开通”、“订单退款怎么算”）。禁止传入空白字符或无关代号。

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
    items = await db.asearch_voucher_info(query, limit=config.get_db_voucher_rules_number())
    
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
    
    _log.debug(f"query_icbc_voucher_rules tool: 检索到 {len(items)} 条结果，返回给模型的内容:\n{context}")
    
    # 3. 在返回给大模型的内容中加入引导指令
    return (
      f"为您找到以下立减金业务规则：\n\n{context}\n\n"
      f"【指令】: 请优先根据'高度相关'的内容回答。如果所有内容距离均大于 0.6，"
      f"请委婉告知用户可能无法准确回答，并提供通用性建议。"
    )
    
  except Exception as e:
    _log.error("查询立减金规则工具执行失败: {}", str(e))
    return f"【工具报错】: 内部执行异常，请尝试根据常识回答或引导人工。"