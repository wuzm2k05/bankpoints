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
        print("--- 正在拉取商品列表 ---")
        all_products = bot.get_all_products()
        print(f"橱窗内共有 {len(all_products)} 个商品")

        # 取前两个商品
        target_list = all_products[:2]

        for i, item in enumerate(target_list):
            pid = item.get("product_id")
            print(f"\n[正在获取第 {i+1} 个商品详情, ID: {pid}]")
            
            detail = bot.get_detail(pid)
            if detail:
                print(f"商品ID: {detail.get('product_id')}")
                print(f"商品原来的ID: {detail.get('out_product_id')}")
                print(f"商品图片URL: {detail.get('img_url')}")
                print(f"商品标题: {detail.get('title')}")
                print(f"商品来源AppID: {detail.get('appid')}")
                print(f"售价(分): {detail.get('selling_price')}")
                print(f"销量: {detail.get('sales')}")
                print(f"带货参数: {detail.get('product_promotion_link')}")
                print("-" * 30)
            else:
                print("获取该商品详情失败")
            
            # 频率控制，避免请求过快
            time.sleep(0.5)