import requests
import json
import time

class WechatTalentAssistant:
    def __init__(self, appid, secret):
        self.appid = appid
        self.secret = secret
        self.access_token = None

    def get_token(self):
        """1. 获取后台接口调用凭据"""
        url = "https://api.weixin.qq.com/cgi-bin/token"
        params = {
            "grant_type": "client_credential",
            "appid": self.appid,
            "secret": self.secret
        }
        res = requests.get(url, params=params).json()
        if "access_token" in res:
            self.access_token = res["access_token"]
            return True
        print(f"Token获取失败: {res}")
        return False

    # 2 个空格对齐
    def create_product_url_link(self, product_id):
      url = f"https://api.weixin.qq.com/wxa/generate_urllink?access_token={self.access_token}"
      
      # 拼装跳转参数
      # 注意：generate_urllink 的参数字段与 generatescheme 略有差异
      payload = {
        "path": "plugin-private://wx6e370ef37e04de68/pages/productDetail/productDetail",
        "query": f"productId={product_id}",
        "env_version": "release", # 正式版
        "is_expire": True,
        "expire_type": 1,
        "expire_interval": 30     # 30天有效
      }
      
      try:
        response = requests.post(url, json=payload)
        data = response.json()
        
        if data.get("errcode") == 0:
          # URL Link 返回的字段名是 url_link
          return data.get("url_link") 
        else:
          print(f"生成 URL Link 失败: {data}")
          
      except Exception as e:
        print(f"请求 generate_urllink 异常: {str(e)}")
        
      return None

    def get_all_products(self):
        """2. 获取达人橱窗商品列表"""
        url = f"https://api.weixin.qq.com/channels/ec/talent/window/product/list/get?access_token={self.access_token}"
        # 设置单页返回数量，根据文档最大500
        payload = {
            "page_size": 10, 
            "page_index": 1
        }
        res = requests.post(url, json=payload).json()
        if res.get("errcode") == 0:
            return res.get("products", [])
        print(f"列表获取失败: {res}")
        return []

    def get_detail(self, product_id):
        """3. 获取单个商品详情"""
        url = f"https://api.weixin.qq.com/channels/ec/talent/window/product/get?access_token={self.access_token}"
        payload = {"product_id": str(product_id)}
        res = requests.post(url, json=payload).json()
        if res.get("errcode") == 0:
            return res.get("product")
        return None

# --- 执行脚本 ---
if __name__ == "__main__":
    # 替换为你自己的 AppID 和 Secret
    APP_ID = "wxf89318237fa044fe"
    APP_SECRET = "3fd5d61771b495df971c4e288f0a8ecf"

    bot = WechatTalentAssistant(APP_ID, APP_SECRET)

    if bot.get_token():
      # 使用你返回数据中的 product_id
      schema_url = bot.create_product_url_link("14000720468982")
      print(schema_url)
      