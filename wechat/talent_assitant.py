import os
import requests
import time
from loguru import logger as _log
from util.singleton import SingletonMeta

class WechatTalentAssistant(metaclass=SingletonMeta):
  def __init__(self):
    # 使用 getenv 修正原代码中的 get_env 错误
    self.appid = os.getenv("WECHAT_TALENT_APPID", None)
    self.secret = os.getenv("WECHAT_TALENT_SECRET", None)
    self._access_token = None
    self._expire_at = 0

  def _get_valid_token(self):
    """私有方法：获取并维护有效的 Token"""
    now = int(time.time())
    if self._access_token and now < self._expire_at:
      return self._access_token

    _log.info("Access Token 过期或不存在，开始向微信服务器申请...")
    url = "https://api.weixin.qq.com/cgi-bin/token"
    params = {
      "grant_type": "client_credential",
      "appid": self.appid,
      "secret": self.secret
    }
    
    try:
      res = requests.get(url, params=params, timeout=10).json()
      if "access_token" in res:
        self._access_token = res["access_token"]
        # 提前 5 分钟失效，确保临界点请求安全
        self._expire_at = now + res["expires_in"] - 300
        _log.success("Token 刷新成功")
        return self._access_token
      _log.error("Token 获取失败: {}", res)
      return None
    except Exception as e:
      _log.exception("请求微信 Token 发生异常: {}", e)
      return None

  def get_all_products(self, page_size: int = 500, page_index: int = None, last_buffer: str = None):
    """
    获取达人橱窗商品列表 (支持分页调用)
    :param page_size: 单页商品数，最大 500
    :param page_index: 页面下标，从 1 开始
    :param last_buffer: 翻页凭证
    :return: dict 包含 products 列表和用于下一页查询的 last_buffer
    """
    token = self._get_valid_token()
    if not token:
      return {"products": [], "last_buffer": None}

    url = f"https://api.weixin.qq.com/channels/ec/talent/window/product/list/get?access_token={token}"
    
    # 构造请求体，优先使用 last_buffer
    payload = {"page_size": page_size}
    if last_buffer:
      payload["last_buffer"] = last_buffer
    elif page_index:
      payload["page_index"] = page_index
    else:
      payload["page_index"] = 1

    try:
      res = requests.post(url, json=payload, timeout=15).json()

      if res.get("errcode") == 0:
        return {
          "products": res.get("products", []),
          "last_buffer": res.get("last_buffer", None)
        }
      
      _log.error("获取商品列表失败: {}", res)
      return {"products": [], "last_buffer": None}
    except Exception as e:
      _log.exception("请求商品列表异常: {}", e)
      return {"products": [], "last_buffer": None}

  def get_detail(self, product_id):
    """获取单个商品详情"""
    token = self._get_valid_token()
    if not token:
      return None

    url = f"https://api.weixin.qq.com/channels/ec/talent/window/product/get?access_token={token}"
    payload = {"product_id": str(product_id)}

    try:
      res = requests.post(url, json=payload, timeout=10).json()
      
      # 处理 Token 失效重试
      if res.get("errcode") == 40001:
        self._access_token = None
        return self.get_detail(product_id)

      if res.get("errcode") == 0:
        return res.get("product")
      
      _log.error("获取商品详情失败 [ID:{}]: {}", product_id, res)
      return None
    except Exception as e:
      _log.exception("请求商品详情异常: {}", e)
      return None