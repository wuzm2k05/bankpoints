import hashlib,requests,httpx,json
from langchain_core.tools import tool

from loguru import logger as _log

from util.singleton import SingletonMeta
import config.config as config

from typing import Dict, List, Annotated

class VoucherOrder(metaclass=SingletonMeta):
  def __init__(self):
    self.salt = config.get_voucher_order_salt()
  
  async def create_voucher_order(self, openid: str, total_points: int, vouchers: List[Dict]):
    """
    创建工行立减金兑换订单。
    用户确认兑换方案后调用，一次提交完整订单。
    
    Args:
      total_points: 本次消耗的i豆总数，例如 123200
      vouchers: 兑换清单，例如：
        [
          {"amount": 100, "card_type": "debit",  "quantity": 1},
          {"amount": 10,  "card_type": "debit",  "quantity": 2},
          {"amount": 1,   "card_type": "credit", "quantity": 2}
        ]
    """
    url = "https://www.pinlenet.com.cn/api/coupon/order/create"
    
    # 2. 映射拼装为渠道侧接口定义的标准报文（将 vouchers 映射回接口要的 coupons）
    payload = {
      "openid": openid,
      "total_points": total_points,
      "coupons": vouchers
    }
     
    try:
      # 3. 发起异步 POST 请求
      async with httpx.AsyncClient() as client:
        response = await client.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return response.text
             
    except Exception as e:
      _log.error(f"下单接口异常: {str(e)}")
      error_msg = f"❌ 抱歉，立减金下单通道暂时发生系统异常。原因: {str(e)}"
      return json.dumps({
        "code": 2,
        "message": error_msg
      }, ensure_ascii=False)
          
  def query_voucher_order_status(self, order_code: str) -> str:
    """
    查询工行立减金兑换订单的实时状态和发放详情。
    
    入参说明:
      order_code (str): 订单编码，通常是一串由纯数字或者数字和字母组成的长字符串。

    """
    # 1. 构造 sign (内部逻辑，对 LLM 透明)
    salt = self.salt
    str_k = salt + order_code
    token = hashlib.md5(str_k.encode(encoding='UTF-8')).hexdigest()

    # 2. 构造请求 URL
    url = "https://www.pinlenet.com.cn/jifen/lijianjin/order/status"
    params = {
      "orderCode": order_code,
      "sign": token
    }

    try:
      # 3. 发起请求
      response = requests.get(url, params=params, timeout=30)
      response.raise_for_status()
      
      # 4. 返回结果给 LLM
      result = response.json()
      _log.debug(result)
      if result.get("code") == 1:
        return f"查询成功：{result.get('msg')}"
      elif result.get("code") == 0:
        return "查询结果：订单不存在，请核对订单号是否正确。"
      else:
        return f"查询失败：{result.get('msg', '未知错误')}"
        
    except Exception as e:
      return f"接口请求异常: {str(e)}"