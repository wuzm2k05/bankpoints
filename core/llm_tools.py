# 2 个空格对齐
import json
from typing import Dict, List, Annotated, Any, Optional
from pydantic import BaseModel, Field, ConfigDict

from langchain_core.tools import tool, InjectedToolArg
from langchain.tools import ToolRuntime
from loguru import logger as _log

import config.config as config
from core.icbc_db import ICBCVectorDB
from core.voucher_order import VoucherOrder, pinle_issue_egg_voucher

class VoucherItem(BaseModel):
  amount: int = Field(description="面额，例如 1, 10, 100")
  card_type: str = Field(description="卡片类型，例如 '信用卡' 或 '借记卡'")
  quantity: int = Field(description="兑换张数")

class CreateVoucherOrderSchema(BaseModel):
  total_points: int = Field(description="本次消耗的i豆总数，例如 123200")
  vouchers: List[VoucherItem] = Field(description="兑换清单，每个元素必须包含 amount, card_type, quantity 三个属性")
  runtime: Annotated[ToolRuntime, InjectedToolArg]

  model_config = ConfigDict(
    extra="forbid",
    arbitrary_types_allowed=True,
  )

class QueryVoucherOrderStatusSchema(BaseModel):
  order_code: str = Field(description="需要查询进度的微信立减金兑换订单号,通常是一串由数字和字母组成的长字符串。例如：'e7164e61d584613960fd49e11ebfa68','400073730129000772605140951276'等")

  class Config:
    extra = "forbid"

class QueryIcbcVoucherRulesSchema(BaseModel):
  query: str = Field(description="用户的原始问题或业务核心关键词（例如：“换实名立减金还在吗”、“月额度超了怎么补发”、“e支付怎么开通”、“订单退款怎么算”）。禁止传入空白字符或无关代号")

  class Config:
    extra = "forbid"

class GetPointsActivitiesSchema(BaseModel):
  gap_points: int = Field(default=0, description="用户需要的i豆点数")

  class Config:
    extra = "forbid"

class VectorSearchIcbcMallSchema(BaseModel):
  query: str = Field(description="用户的原始需求、意图关键词或具体的商品名称")

  class Config:
    extra = "forbid"
      
class VectorSearchWechatProductsSchema(BaseModel):
  query: str = Field(description="用户的原始需求、意图关键词或具体的商品名称")

  class Config:
    extra = "forbid"

class EmptyArgsSchema(BaseModel):
  class Config:
    extra = "forbid"  # 强制告诉大模型：这个工具绝对不能传入任何参数

class IssueEggVoucherSchema(BaseModel):
  runtime: Annotated[ToolRuntime, InjectedToolArg]

  model_config = ConfigDict(
    extra="forbid",
    arbitrary_types_allowed=True,
  )

class QueryEggInfoSchema(BaseModel):
  query: str = Field(description="用户关于鸡蛋的疑问或关键词，例如：'产地在哪里'、'孕妇能不能吃'、'顺丰发货吗'、'口感怎么样'等")

  class Config:
    extra = "forbid"

@tool(args_schema=QueryEggInfoSchema)
async def query_egg_info(query: str) -> str:
  """
  查询宜凤园土鸡蛋的产地环境、营养成分、安全认证、发货包装及口感吃法等知识库信息。
  当用户询问关于宜凤园鸡蛋的任何具体问题时调用。
  """
  try:
    _log.info("query_egg_info tool: 执行鸡蛋知识库检索, query={}", query)
    db = ICBCVectorDB()
    results = await db.asearch_egg_info(query, limit=2)
    
    if not results:
      return "【信息】: 知识库中未查到与该鸡蛋提问相匹配的详细说明。"

    formatted_results = []
    for item in results:
      formatted_results.append(f"【参考知识】: {item['content']}")
      
    return "\n\n".join(formatted_results)
    
  except Exception as e:
    _log.error("查询鸡蛋知识库失败: {}", str(e))
    return f"【工具报错】: 检索鸡蛋知识库时出现异常: {str(e)}"
        
# --- 1. 定义工具集 (Tools) ---
@tool(args_schema=IssueEggVoucherSchema)
async def issue_egg_voucher(runtime: Annotated[ToolRuntime, InjectedToolArg]) -> str:
  """
  发放宜凤园土鸡蛋超级代金券。
  当用户表达想要、同意领取代金券（如“想要”、“好的”、“领一张”）时调用此工具。

  Returns:
    JSON字符串，包含发放结果。
    成功示例：{"code": 0, "message": "发放成功"}
    失败示例：{"code": 1, "message": "发放失败"}
  """
  configurable = runtime.config.get("configurable", {})
  openid = configurable.get("thread_id")
  _log.info(f"issue_egg_voucher: 开始为 openid={openid} 发放鸡蛋代金券")
  if not openid:
    _log.error("issue_egg_voucher: 未能从 runtime 中获取到有效的 thread_id/openid")
    return json.dumps({"code": 1, "message": "Openid错误"}, ensure_ascii=False)

  return await pinle_issue_egg_voucher(openid)
  
@tool(args_schema=CreateVoucherOrderSchema)
async def create_voucher_order(total_points: int, vouchers: List[VoucherItem], runtime: Annotated[ToolRuntime, InjectedToolArg]) -> str:
  """
  创建工行立减金兑换订单。
  用户确认兑换方案后调用，一次提交完整订单。
   
  Returns:
    JSON字符串，包含订单的支付链接。这个链接一次支付所有立减金的兑换。
    成功例子：{
      "code": 0,
      "message": "success",
      "data": {
        "pay_url": "https://www.pinlenet.com.cn/jifen/lijianjin/pay?orderCode=xxx",
        "order_code": "2223234123423423423423"
      }
    }
    失败例子：{
      "code": 1,
      "message": "兑换失败，原因：xxxx"
    }
  """
  # ==================== 🛠️ 红线审计逻辑开始 ====================
  _log.debug(f"total_points: {total_points}, vouchers: {vouchers}")
  total_amount = 0
  batch_counts = {}  # 用于统计不同 (amount, card_type) 组合（即一个批次）的总张数
  ten_vouchers_nbr = 0
  one_vouchers_nbr = 0
  vouchers_list = []
  CARD_TYPE_MAP = {"信用卡": "credit", "借记卡": "debit"}
  for v in vouchers:
    amount = v.amount
    quantity = v.quantity
    #card_type = v.card_type
    card_type = CARD_TYPE_MAP.get(v.card_type, v.card_type)
    vouchers_list.append({"amount": amount, "quantity": quantity, "card_type": card_type}) #下游函数使用
    
    if amount not in [1, 10, 100]:
      # 理论上不会发生，但如果出现了，可以记录日志或提前返回错误
      _log.debug(f"create_voucher_order: Error: 兑换失败：检测到不支持的面额 {amount} 元。 ")
      return json.dumps({"code": 1, "message": f"兑换失败：检测到不支持的面额 {amount} 元。"}, ensure_ascii=False)
        
    if amount == 10:
      ten_vouchers_nbr += quantity
    elif amount == 1:
      one_vouchers_nbr += quantity
    
    # 累计总金额
    total_amount += amount * quantity
    
    # 按 (金额, 卡类型) 维度作为批次进行统计
    batch_key = (amount, card_type)
    batch_counts[batch_key] = batch_counts.get(batch_key, 0) + quantity
      
  # 检查大额原则：10元的和1元的不能超过9张
  if one_vouchers_nbr >= 10:
    _log.debug(f"create_voucher_order: Error: 兑换失败：1元面额张数累计已达10张或以上。系统规则要求每10张1元必须合并为1张10元，请重新调整方案。 ")
    return json.dumps({
      "code": 1,
      "message": "兑换失败：1元面额张数累计已达10张或以上。系统规则要求每10张1元必须合并为1张10元，请重新调整方案。"
    }, ensure_ascii=False)
    
  if ten_vouchers_nbr >= 10:
    _log.debug(f"create_voucher_order: Error: 兑换失败：10元面额张数累计已达10张或以上。系统规则要求每10张10元必须合并为1张100元，请重新调整方案。 ")
    return json.dumps({
      "code": 1,
      "message": "兑换失败：10元面额张数累计已达10张或以上。系统规则要求每10张10元必须合并为1张100元，请重新调整方案。"
    }, ensure_ascii=False)

  # 1. 检查红线一：总立减金金额不超过 5000 元
  if total_amount > 5000:
      return json.dumps({
          "code": 1,
          "message": f"兑换失败，原因：单笔兑换总金额（当前 {total_amount} 元）已超过最大风控限制 5000 元，请重新计算并缩减方案。"
      }, ensure_ascii=False)

  # 2. 检查红线二：任何批次（相同金额且相同卡类型）不能超过 60 张
  for (amount, card_type), count in batch_counts.items():
      if count > 60:
          card_type_cn = "借记卡" if card_type == "debit" else "信用卡" if card_type == "credit" else card_type
          return json.dumps({
              "code": 1,
              "message": f"兑换失败，原因：单个批次不能超过 60 张。当前“{amount}元-{card_type_cn}”批次张数达到了 {count} 张，请重新计算方案，引导用户升级大面额或缩减数量。"
          }, ensure_ascii=False)
  # ==================== 🛠️ 红线审计逻辑结束 ====================
  user_id = runtime.config.get("configurable").get("thread_id")
  _log.info(f"create_voucher_order: user_id={user_id}")

  voucher_order = VoucherOrder()
  return_result = await voucher_order.create_voucher_order(user_id, total_points, vouchers_list)
  _log.debug(f"create_voucher_order return: {return_result}")
  return return_result
    
@tool(args_schema=QueryVoucherOrderStatusSchema)
async def query_voucher_order_status(order_code: str) -> str:
  """
  查询工行立减金兑换订单的实时状态和发放详情。

  返回内容示例：
    成功
      {
          "code": 0,
          "message": "success",
          "data": [
              {
                  "order_code": "order_code",
                  "status": "status",
                  "total": "300元",
                  "payable": "3张100元",
                  "payed": "3张100元",
                  "usage": "xxxx已使用,xxxx已使用,xxxx已使用",
              }，
              {
                  "order_code": "order_code",
                  "status": "status",
                  "total": "30元",
                  "payable": "3张10元",
                  "payed": "3张10元",
                  "usage": "xxxx已使用,xxxx已使用,xxxx已使用",
              },
              {
                  "order_code": "order_code",
                  "status": "status",
                  "total": "3元",
                  "payable": "3张1元",
                  "payed": "3张1元",
                  "usage": "xxxx已使用,xxxx已使用,xxxx已使用",
              }        
          ]
      }

      失败
      {
          "code": 1,
          "message": "错误信息"
      }
    
    注意：
      订单状态(status)说明：
      -  等待银行支付结果： 已下单等待用户支付，用户支付成功后银行支付系统会回调平台接口发送支付信息，该状态表示还没有收到银行支付系统的回调。
      - 订单已过期：用户下单后平台会在银行支付系统形成对应的“支付订单”，有效期为30分钟，用户需在30分钟内操作支付。订单已过期表明用户在支付单有效期内没有成功支付。
      - 立减金发放失败：用户成功支付，平台收到银行支付系统的回调，但发放微信立减金失败，后续平台会进行自动补发。
      - 立减金补发失败：平台自动补发也失败了。
      - 立减金发放成功：立减金发放成功，用户可在微信卡包查看。
      
      券状态(usage)说明：
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
  result = await voucher_order.query_voucher_order_status(order_code)
  _log.debug(f"订单查询结果: {result}")
  return result
  
@tool(args_schema=VectorSearchWechatProductsSchema)
async def vector_search_wechat_products(query: str):
  """
  【核心指令】调用此工具检索微信小店商城中的商品候选列表，此数据库为向量数据库。
  
  重要操作规范：
  1. 语义筛选：返回结果基于向量相似度，可能包含噪音。你必须作为审计员，剔除任何不符合用户意图的商品。
    
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

@tool(args_schema=VectorSearchIcbcMallSchema)
async def vector_search_icbc_mall(query: str):
  """
  【核心指令】调用此工具检索工银i豆商城中的商品候选列表，此数据库为向量数据库。
  
  重要操作规范：
  1. 语义筛选：返回结果基于向量相似度，可能包含噪音。你必须作为审计员，剔除任何不符合用户意图的商品。
    
  Returns:
    list[dict]: 商品字典列表。每个字典包含: name, points, distance。
  """
  _log.debug("vector_search_icbc_mall tool: 搜索工银i豆商城，查询语句：{}", query)
  
  # 假设 ICBCVectorDB 已经支持异步搜索，或者在内部处理了线程池
  icbc_db = ICBCVectorDB()
  results = await icbc_db.asearch(query, limit=3) 
  
  _log.debug("vector_search_icbc_mall tool: 搜索到 {} 条结果，返回给模型的内容:\n{}", len(results), results)
  
  return results

@tool(args_schema=GetPointsActivitiesSchema)
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

@tool(args_schema=QueryIcbcVoucherRulesSchema)
async def query_icbc_voucher_rules(query: str) -> str:
  """
  【业务工具：工行i豆与微信立减金综合规则及售后政策检索】
  
  用途：
  当用户咨询任何与工行i豆、微信立减金相关的业务规则、系统报错、额度限制及售后对账问题时必须调用此工具来获得最新的官方信息。
  
  大模型触发场景提示：
  1. 渠道与流程：查询i豆商城入口、微信立减金兑换流程、e支付开通/重置密码指南。
  2. 核心规则：立减金有效期（10天）、面额选择（1/10/100元）、单笔最大叠加数（8张）、满减门槛（过面额一分钱）。
  3. 限额与风控：每月上限5000元或单批次60笔导致的“发放失败”、“数量不对”、“提取失败”及“当日早中晚人工补发”机制。
  4. 待发与延期：如何利用拼乐小程序购买“自行提取”来延长立减金有效期、待发金额如何提取。
  5. 售后与对账：实名认证变更券失效、购物退款（券过期不退）、e支付和i豆明细对账、如何截图利用微信识别文字功能复制订单号、人工客服热线（4006-705-057）及工作时间。

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
  
@tool(args_schema=EmptyArgsSchema)
def route_to_customer_service() -> str:
  """当用户需要基础咨询、查豆路径、查订单状态、找人工、攒豆攻略、立减金规则、寒暄打招呼时，触发此工具。"""
  return "ROUTE_CUSTOMER_SERVICE"

@tool(args_schema=EmptyArgsSchema)
def route_to_points_exchange() -> str:
  """当用户明确表达【纯粹想用i豆兑换成微信立减金/直兑券】的诉求，且不涉及具体商品比价时，触发此工具。"""
  return "ROUTE_POINTS"

@tool(args_schema=EmptyArgsSchema)
def route_to_goods_exchange() -> str:
  """当用户表达【商品购买、比价导购、想用立减金买商城东西、商城有什么大米】等明确的商品消费意图时，触发此工具。"""
  return "ROUTE_GOODS"

@tool(args_schema=EmptyArgsSchema)
def route_back_to_router() -> str:
  """
  当你发现用户的输入/诉求超出了你的核心业务能力范围，或者用户转向了其他话题时，
  请立刻触发此工具，将控制权交还给调度中心。
  """
  return "ROUTE_BACK"