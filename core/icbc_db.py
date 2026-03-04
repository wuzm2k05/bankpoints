# 2 个空格对齐
import asyncio
import re
from typing import List, Optional, Dict, Any
import sys
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
  def __init__(self):
    # 1. 初始化 ChromaDB 持久化客户端
    self.client = chromadb.PersistentClient(path="./icbc_vector_db")
    
    # 2. 初始化 Qwen Embedding 接口
    self.embeddings = DashScopeEmbeddings(
      model="text-embedding-v3",
      dashscope_api_key=config.get_qwen_api_key()
    )
    
    # 3. 初始化各 Collection
    self.product_collection = self.client.get_or_create_collection(name="icbc_products")
    self.strategy_collection = self.client.get_or_create_collection(name="icbc_strategies")
    self.voucher_collection = self.client.get_or_create_collection(name="icbc_standing_vouchers")
    _log.info("ICBCVectorDB 向量库连接池初始化成功")

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