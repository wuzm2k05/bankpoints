# 2 个空格对齐
import time
import hashlib
import requests
import json
import config.config as config
import log.logger as logger

_log = logger.get_logger()

class JDUnionClient:
  def __init__(self):
    self.app_key = config.get_jd_app_key()
    self.app_secret = config.get_jd_app_secret()
    self.site_id = config.get_jd_site_id()
    self.position_id = config.get_jd_position_id()
    self.api_url = "https://api.jd.com/routerjson"

  def _generate_sign(self, params: dict) -> str:
    sorted_params = sorted(params.items())
    query_str = self.app_secret
    for k, v in sorted_params:
      query_str += f"{k}{v}"
    query_str += self.app_secret
    return hashlib.md5(query_str.encode("utf-8")).hexdigest().upper()

  def _request(self, method: str, biz_params: dict) -> dict:
    # 关键修改：ensure_ascii=False
  # separators=(',', ':') 也是必须的，去掉多余空格
    param_json_str = json.dumps(
      biz_params, 
      ensure_ascii=False, 
      separators=(',', ':')
    )
    
    params = {
      "method": method,
      "app_key": self.app_key,
      "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
      "format": "json",
      "v": "1.0",
      "sign_method": "md5",
      "param_json": param_json_str
    }
    params["sign"] = self._generate_sign(params)
    
    try:
      # 显式指定 GET 请求，京东 API 也可以用 POST
      _log.info(f"JD API Request: method={method}, params={params}")
      resp = requests.get(self.api_url, params=params, timeout=10)
      
      # 打印原始返回，这是排查的关键
      _log.debug(f"JD API Response Text: {resp.text}")
      
      return resp.json()
    except Exception as e:
      _log.error(f"JD API Request Failed: {e}")
      return {}

  def get_best_promotion_items(self, query: str, top_k: int = 3) -> list:
    """
    Mock 京东 API 返回值，用于跳过 43 错误，测试 Agent 流程
    """
    _log.warning(f"⚠️ [MOCK] 正在模拟京东搜索结果: {query}")
    
    # 模拟几种不同档位的商品价格
    mock_data = {
      "小米": [
        {"name": "小米14 Pro 12GB+256GB 黑色", "price": 4999.0, "skuId": "100083201328"},
        {"name": "小米手环 8 标准版", "price": 239.0, "skuId": "100055104832"},
        {"name": "小米移动电源 20000mAh", "price": 159.0, "skuId": "10001043978"}
      ],
      "默认": [
        {"name": f"【京东精选】{query} 优质商品", "price": 998.0, "skuId": "999999"}
      ]
    }

    raw_list = mock_data.get(query, mock_data["默认"])
    results = []
    for it in raw_list[:top_k]:
      results.append({
        "name": it["name"],
        "price": it["price"],
        "url": f"https://item.jd.com/{it['skuId']}.html", # 模拟转链
        "skuId": it["skuId"]
      })
    return results
  
  def _get_best_promotion_items(self, query: str, top_k: int = 3) -> list:
    """
    搜索并筛选出最匹配的多个商品，并分别转为返佣链接
    TODO: 可以假如LRU Cache来对热门搜索词进行缓存，减少API调用次数
    """
    # 1. 扩大搜索范围，取前 10 个进行内部筛选
    search_biz = {
      "goodsReqDTO": {
        "keyword": query,
        "pageSize": 10,
        "sortName": "price", # 依然按价格升序，方便比价
        "sort": "asc"
      }
    }
    search_res = self._request("jd.union.open.goods.query", search_biz)
    
    inner_search = search_res.get("jd_union_open_goods_query_responce", {})
    query_result = json.loads(inner_search.get("queryResult", "{}"))
    goods_list = query_result.get("data", [])

    if not goods_list:
      _log.info(f"JD Search: No results for query '{query}'")
      return []

    # 2. 筛选逻辑：过滤掉标题完全不相关的（例如搜“手机”出“手机壳”）
    # 你也可以在这里加入 LLM Rerank 逻辑
    filtered_items = []
    query_words = [w for w in query.split() if len(w) > 1] # 提取 query 中的核心词
    
    for item in goods_list:
      name = item.get("skuName", "")
      # 简单的相关性检查：如果 query 里的关键词在标题里一个都没出现，则跳过
      if query_words and not any(word.lower() in name.lower() for word in query_words):
        continue
      filtered_items.append(item)
    
    # 取筛选后的前 top_k 个
    selected_items = filtered_items[:top_k] if filtered_items else goods_list[:1]

    # 3. 批量转链
    results = []
    for it in selected_items:
      raw_url = f"https://item.jd.com/{it['skuId']}.html"
      price = it.get("priceInfo", {}).get("lowestPrice") or it.get("priceInfo", {}).get("price")
      
      # 调用转链接口
      promo_biz = {
        "promotionCodeReq": {
          "materialId": raw_url,
          "siteId": self.site_id,
          "positionId": self.position_id
        }
      }
      promo_res = self._request("jd.union.open.promotion.common.get", promo_biz)
      inner_promo = promo_res.get("jd_union_open_promotion_common_get_responce", {})
      promo_data = json.loads(inner_promo.get("getResult", "{}")).get("data", {})
      click_url = promo_data.get("clickURL")

      results.append({
        "name": it.get("skuName"),
        "price": float(price) if price else 0.0,
        "url": click_url if click_url else raw_url,
        "skuId": it.get("skuId")
      })
      
    return results
  
  def test_jd_api_permission(self):
    # 找一个京东商城的真实 SKU ID（例如：100012043978 是小米充电宝）
    search_biz = {
      "goodsReqDTO": {
        "skuIds": ["100012043978"], 
        "pageSize": 1
      }
    }
    
    biz_params = {
    "goodsReqDTO": {
      "keyword": "小米"
    }
}
    # 调用 jd.union.open.goods.query
    res = self._request("jd.union.open.goods.query", biz_params)
    _log.info(f"SKU ID 搜索测试结果: {res}")
  
  def test_promotion_api(self):
    biz_params = {
      "promotionCodeReq": {
        "materialId": "https://item.jd.com/100012043978.html",
        "siteId": "4103034594" # 换成你真实的 siteId
      }
    }
    res = self._request("jd.union.open.promotion.common.get", biz_params)
    _log.info(f"转链接口测试结果: {res}")