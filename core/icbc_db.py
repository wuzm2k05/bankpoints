# 2 个空格对齐
import asyncio,time
import re,datetime,json
from typing import List, Optional, Dict, Any
import sys
from multiprocessing import Value
from loguru import logger as _log

import config.config as config

# 针对 Linux 环境下的库兼容处理
try:
  __import__('pysqlite3')
  sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
except ImportError:
  pass
import chromadb

from langchain_community.embeddings import DashScopeEmbeddings
from util.singleton import SingletonMeta

class ICBCVectorDB(metaclass=SingletonMeta):
  # 增加一个类属性，用于存储跨进程共享的变量
  # 注意：这个变量必须在主进程初始化单例前创建并赋值
  _shared_version = Value('i', 0)
  
  def __init__(self):
    # 1. 初始化 ChromaDB 持久化客户端
    self.client = chromadb.PersistentClient(path="./icbc_vector_db")
    
    # --- 1. 自愈清理逻辑 ---
    all_colls = [c.name for c in self.client.list_collections()]
    # 情况 A: 正式表丢失，但旧表还在（说明切换中途崩了）
    if "wechat_talent_products" not in all_colls and "wechat_talent_products_old" in all_colls:
      _log.warning("检测到非正常中断，正在从备份恢复正式 Collection...")
      old_coll = self.client.get_collection("wechat_talent_products_old")
      old_coll.modify(name="wechat_talent_products")
      
    # 情况 B: 清理残留的临时表
    for name in all_colls:
      if name.startswith("wechat_tmp_"):
        self.client.delete_collection(name)
          
    # 2. 初始化 Qwen Embedding 接口
    self.embeddings = DashScopeEmbeddings(
      model="text-embedding-v3",
      dashscope_api_key=config.get_qwen_api_key()
    )
    
    # 3. 初始化各 Collection
    self.product_collection = self.client.get_or_create_collection(name="icbc_products")
    self.strategy_collection = self.client.get_or_create_collection(name="icbc_strategies")
    self.voucher_collection = self.client.get_or_create_collection(name="icbc_standing_vouchers")
    
    # 新增：微信达人商品 collection
    self.wechat_collection = self.client.get_or_create_collection(name="wechat_talent_products")
    
    # 进程本地记录的版本号
    self.local_version = -1 
    # 指向共享内存中的版本号
    self.shared_version = ICBCVectorDB._shared_version
    
    _log.info("ICBCVectorDB 向量库连接池初始化成功")

  def start_rebuild_session(self) -> str:
    """第一步：创建一个空的临时集合，返回名称"""
    temp_name = f"wechat_tmp_{int(time.time())}"
    self.client.create_collection(name=temp_name)
    
    # delete old collection if it exists
    try:
      self.client.delete_collection("wechat_talent_products_old")
    except: pass
    
    return temp_name

  def append_to_rebuild_session(self, temp_name: str, products: List[Dict[str, Any]]):
    """第二步：分批写入数据。此时内存中只会有当前这一小批数据"""
    if not products: return
    
    temp_coll = self.client.get_collection(name=temp_name)
    
    ids = [p["id"] for p in products]
    documents = [p.get("desc") or p["title"] for p in products]
    metadatas = [{
      "title": p["title"],
      "price": p["price"],
      "link": p.get("link", ""),
      "outId": p["outId"],
      "outAppId": p["outAppId"],
      "id": p["id"]
    } for p in products]
    
    # 这一步最耗内存（计算向量），但因为 batch 很小，所以受控
    embeddings = self.embeddings.embed_documents(documents)
    
    temp_coll.add(
      ids=ids,
      embeddings=embeddings,
      documents=documents,
      metadatas=metadatas
    )

  def finalize_rebuild_session(self, temp_name: str):
    """第三步：瞬间切换名称"""
    temp_coll = self.client.get_collection(name=temp_name)
    
    # 蓝绿切换逻辑
    try:
      self.client.delete_collection("wechat_talent_products_old")
    except: pass
    
    try:
      self.wechat_collection.modify(name="wechat_talent_products_old")
    except: pass
    try:  
      temp_coll.modify(name="wechat_talent_products")
      self.wechat_collection = self.client.get_collection(name="wechat_talent_products")
      
      with self.shared_version.get_lock():
        self.shared_version.value += 1
      
      self.local_version = self.shared_version.value
      
      _log.success("微信库流式重建完成并已上线")
    except Exception as e:
      _log.error(f"重建wechat products 失败: {str(e)}")
      raise e
      
  def get_product_by_id(self, product_id: str) -> Optional[Dict[str, Any]]:
    """
    根据 ID 从正式库查询商品信息，用于判断是否需要重新调用 LLM
    """
    try:
      # 注意：永远去正式表 wechat_talent_products 查询
      # 使用 self.wechat_collection 即可，它在初始化和 finalize 时都会指向正式表
      res = self.wechat_collection.get(ids=[product_id])
      if res and "ids" in res and len(res["ids"]) > 0 :
        return {
          "id": res["ids"][0],
          "title": res["metadatas"][0].get("title"),
          "desc": res["documents"][0] # document 存的就是 enhance_description
        }
    except Exception as e:
      _log.debug("查询旧商品 {} 失败（可能库尚未建立） {}", product_id, e)
    return None
  
  # --- 搜索逻辑 ---
  async def asearch_wechat_products(self, query: str, limit: int = 5):
    """异步搜索微信达人商品"""
    return await asyncio.to_thread(self.search_wechat_products, query, limit)

  def search_wechat_products(self, query: str, limit: int = 5):
    _log.debug("正在执行微信达人商品搜索: {}", query)
    if self.local_version != self.shared_version.value:
      _log.info("检测到共享内存版本变化，同步 Collection 引用...")
      try:
        self.wechat_collection = self.client.get_collection(name="wechat_talent_products")
        self.local_version = self.shared_version.value
      except Exception as e:
        _log.error("同步引用失败: {}", e)
        
    query_vector = self.embeddings.embed_query(query)
    
    # 防止另外一个进程删除了collection（因为rebuild，所以重试一下）
    try:
      results = self.wechat_collection.query(
        query_embeddings=[query_vector],
        n_results=limit
      )
    except Exception as e:
      # 2. 如果报错（例如 Collection 被改名或删除），说明进程 A 可能刚刚完成了 rebuild
      _log.warning("检测到 Collection 引用可能失效(进程间同步)，正在刷新引用... Error: {}", e)
      # 重新从磁盘加载最新的正式版引用（这一步会读取最新的磁盘元数据）
      self.wechat_collection = self.client.get_collection(name="wechat_talent_products")
      self.local_version = self.shared_version.value
      _log.info("进程内存引用已更新至最新 Collection")
      # 3. 用新的引用重试
      results = self.wechat_collection.query(
        query_embeddings=[query_vector],
        n_results=limit
      )
        
    output = []
    if results["ids"] and len(results["ids"][0]) > 0:
      for i in range(len(results["ids"][0])):
        metadata = results["metadatas"][0][i]
        ai_desc = results["documents"][0][i] if results.get("documents") else ""
        output.append({
          "id": results["ids"][0][i],
          "title": metadata.get("title"),
          "price": metadata.get("price"),
          "link": metadata.get("link"),
          "outId": metadata.get("outId"),
          "outAppId": metadata.get("outAppId"),
          "distance": results["distances"][0][i],
          "desc": ai_desc
        })
    
    # 判断是否我们没有这款商品，如果没有就记录下来    
    DISTANCE_THRESHOLD = 0.55
    
    # 判定：完全没结果 OR 第一名的距离太大
    is_not_found = not output
    is_low_relevance = output and output[0]["distance"] > DISTANCE_THRESHOLD

    if is_not_found or is_low_relevance:
      # 使用 bind 绑定 extra 字段，触发特殊的 logger sink
      log_data = {
        "query": query,
        "reason": "not_found" if is_not_found else "low_relevance",
        "best_distance": output[0]["distance"] if output else 1.0,
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "best_match": output[0]["title"] if output else None
      }
      
      _log.bind(missing_product=True).info(json.dumps(log_data, ensure_ascii=False))
      _log.debug("已记录缺失商品 Query: {}", query)
    
    return output
  

  # --- 异步包装方法 ---
  async def asearch(self, query: str, limit: int = 3):
    """异步搜索商品"""
    return await asyncio.to_thread(self.search, query, limit)

  async def asearch_voucher_info(self, query: str, limit: int = 2) -> List[str]:
    """异步搜索立减金规则"""
    return await asyncio.to_thread(self.search_voucher_info, query, limit)

  async def asearch_strategy(self, query: str, limit: int = 2) -> List[Dict[str, Any]]:
    """异步搜索积分策略"""
    return await asyncio.to_thread(self.search_strategy, query, limit)

  # --- 原有同步方法（被 asearch 系列内部调用） ---

  def search(self, query: str, limit: int = 3):
    _log.debug("正在执行商品向量搜索: {}", query)
    query_vector = self.embeddings.embed_query(query)
    results = self.product_collection.query(
      query_embeddings=[query_vector],
      n_results=limit
    )
    
    output = []
    if results["ids"] and len(results["ids"][0]) > 0:
      for i in range(len(results["ids"][0])):
        metadata = results["metadatas"][0][i]
        distance = results["distances"][0][i]
        output.append({
          "name": metadata.get("name", "未知商品"),
          "points": metadata.get("points", 0),
          "distance": distance
        })
    return output

  def search_voucher_info(self, query: str, limit: int = 2) -> List[str]:
    _log.debug("正在搜索立减金规则: {}", query)
    query_vector = self.embeddings.embed_query(query)
    results = self.voucher_collection.query(
      query_embeddings=[query_vector],
      n_results=limit
    )
    if results["documents"] and len(results["documents"][0]) > 0:
      return results["documents"][0]
    return []

  def search_strategy(self, query: str, limit: int = 2) -> List[Dict[str, Any]]:
    _log.debug("正在搜索积分策略: {}", query)
    query_vector = self.embeddings.embed_query(query)
    results = self.strategy_collection.query(
      query_embeddings=[query_vector],
      n_results=limit
    )
    
    output = []
    if not results["ids"] or not results["ids"][0]:
      return output

    for i in range(len(results["ids"][0])):
      output.append({
        "content": results["documents"][0][i],
        "category": results["metadatas"][0][i]["category"],
        "distance": results["distances"][0][i]
      })
    return output

  # --- 数据维护方法 (通常在离线脚本中使用，保持同步即可) ---

  def add_products(self, products: List[Dict[str, Any]]):
    ids = [p["id"] for p in products]
    documents = [f"商品名称: {p['name']}。所需积分: {p['points']}豆。" for p in products]
    metadatas = [{"name": p["name"], "points": p["points"]} for p in products]
    embeddings = self.embeddings.embed_documents(documents)
    
    self.product_collection.add(
      ids=ids,
      embeddings=embeddings,
      documents=documents,
      metadatas=metadatas
    )
    _log.info("成功导入 {} 条商品数据", len(ids))

  def add_voucher_knowledge(self, qa_content: str):
    parts = re.split(r'Q[:：]', qa_content)
    documents = []
    ids = []
    metadatas = []
    
    for idx, part in enumerate(parts):
      if not part.strip(): continue
      ids.append(f"voucher_qa_{idx}")
      documents.append("Q: " + part.strip())
      metadatas.append({"source": "official_faq", "type": "standing_voucher"})

    if documents:
      embeddings = self.embeddings.embed_documents(documents)
      self.voucher_collection.add(
        ids=ids, embeddings=embeddings, documents=documents, metadatas=metadatas
      )
      _log.info("成功导入 {} 条业务知识", len(documents))