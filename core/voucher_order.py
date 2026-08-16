import hashlib,requests,httpx,json
from langchain_core.tools import tool

from loguru import logger as _log

from util.singleton import SingletonMeta
import config.config as config

from typing import Dict, List, Annotated

async def pinle_issue_egg_voucher(openid:str) -> str:
  """
  发放宜凤园土鸡蛋超级代金券。
  当用户表达想要、同意领取代金券（如“想要”、“好的”、“领一张”）时调用此工具。

  Returns:
    JSON字符串，包含发放结果。
    成功示例：{"code": 0, "message": "发放成功"}
    失败示例：{"code": 1, "message": "发放失败"}
  """
  url = "https://www.pinlenet.com.cn/jifen/distri/13/send"
  params = {
      "openid": openid,
      "code": "P901"
  }

  try:
    async with httpx.AsyncClient() as client:
      response = await client.get(url, params=params, timeout=10.0)
      res_json = response.json()
      _log.debug(f"issue_egg_voucher 接口返回: {res_json}")
      return json.dumps(res_json, ensure_ascii=False)
        
  except Exception as e:
    _log.error(f"issue_egg_voucher 请求异常: {str(e)}")
    return json.dumps({"code": 1, "message": f"代金券发放接口调用失败: {str(e)}"}, ensure_ascii=False)

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
          
  async def query_voucher_order_status(self, order_code: str) -> str:
    r"""
    查询工行立减金兑换订单的实时状态和发放详情。
    
    入参说明:
      order_code (str): 订单编码，通常是一串由纯数字或者数字和字母组成的长字符串。
    返回：
      所有order_code都是一样即查询的order_code
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

    """
    # 1. 构造 sign (内部逻辑，对 LLM 透明)
    salt = self.salt
    str_k = salt + order_code
    token = hashlib.md5(str_k.encode(encoding='UTF-8')).hexdigest()

    # 2. 构造请求 URL
    url = "https://www.pinlenet.com.cn/api/coupon/order/status"
    params = {
      "orderCode": order_code,
      "sign": token
    }
    
    try:
      async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.get(url, params=params)
        
        if response.status_code != 200:
          _log.error(f"接口请求失败，HTTP 状态码: {response.status_code}")
          return json.dumps({"code": 1, "message": f"系统连接异常，HTTP 状态码: {response.status_code}"}, ensure_ascii=False)
      
        # 2. 获取接口原始 JSON 数据
        res_data = response.json()
        
        # 3. 直接将字典序列化为 JSON 字符串返回给 Langchain 框架
        # 框架会自动将其作为 ToolMessage 的 content 输送给大模型
        return json.dumps(res_data, ensure_ascii=False)
            
    except Exception as e:
      _log.error(f"查询订单接口发生异常: {str(e)}")
      return json.dumps({"code": 1, "message": "网络繁忙，订单状态查询暂不可用"}, ensure_ascii=False)  
  
  