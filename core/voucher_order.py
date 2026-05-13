import hashlib,requests
from langchain_core.tools import tool

from loguru import logger as _log

from util.singleton import SingletonMeta
import config.config as config

class VoucherOrder(metaclass=SingletonMeta):
  def __init__(self):
    self.salt = config.get_voucher_order_salt()

  def query_voucher_order_status(self, order_code: str) -> str:
    """
    查询工行立减金兑换订单的实时状态和发放详情。
    
    入参说明:
      order_code (str): 订单编码，通常是一串由数字和字母组成的长字符串。

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
      response = requests.get(url, params=params, timeout=10)
      response.raise_for_status()
      
      # 4. 返回结果给 LLM
      result = response.json()
      if result.get("code") == 1:
        return f"查询成功：{result.get('msg')}"
      elif result.get("code") == 0:
        return "查询结果：订单不存在，请核对订单号是否正确。注意一定要用商户订单号而非银行订单号。"
      else:
        return f"查询失败：{result.get('msg', '未知错误')}"
        
    except Exception as e:
      return f"接口请求异常: {str(e)}"