# 2 个空格对齐
import logging
import re
from typing import List, Optional, Dict, Any
import sys

import config.config as config

try:
  __import__('pysqlite3')
  sys.modules['sqlite3'] = sys.modules.pop('pysqlite3')
except ImportError:
  pass
import chromadb

from langchain_community.embeddings import DashScopeEmbeddings

from util.singleton import SingletonMeta

_log = logging.getLogger(__name__)

class ICBCVectorDB(metaclass=SingletonMeta):
  def __init__(self):
    # 1. 初始化 ChromaDB 持久化客户端
    self.client = chromadb.PersistentClient(path="./icbc_vector_db")
    
    # 2. 初始化 Qwen Embedding 接口
    self.embeddings = DashScopeEmbeddings(
      model="text-embedding-v3",
      dashscope_api_key=config.get_qwen_api_key()
    )
    
    # 3. 定义商品 Collection
    self.product_collection = self.client.get_or_create_collection(
      name="icbc_products"
    )

    # 4. 定义策略/攻略 Collection
    self.strategy_collection = self.client.get_or_create_collection(
      name="icbc_strategies"
    )
    
    # 5. 定义立减金知识 Collection
    self.voucher_collection = self.client.get_or_create_collection(
      name="icbc_standing_vouchers"
    )
  
  def add_products(self, products: List[Dict[str, Any]]):
    """批量添加商品数据"""
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
    _log.info(f"成功导入 {len(ids)} 条商品数据")
    
  
  def search(self, query: str, limit: int = 3):
    query_vector = self.embeddings.embed_query(query)
    results = self.product_collection.query(
      query_embeddings=[query_vector],
      n_results=limit
    )
    
    output = []
    # 关键点：ChromaDB 的 query 结果是一个嵌套列表
    if results["ids"] and len(results["ids"][0]) > 0:
      for i in range(len(results["ids"][0])):
        # 确保从 metadatas 中获取字典数据
        metadata = results["metadatas"][0][i]
        distance = results["distances"][0][i]
        
        output.append({
          "name": metadata.get("name", "未知商品"),
          "points": metadata.get("points", 0),
          "distance": distance
        })
    return output

  # --- 新增：积分策略相关功能 ---

  def add_strategies(self, strategies: List[Dict[str, Any]]):
    """
    批量添加积分攻略/策略
    strategies 格式: [{"id": "s1", "content": "攻略内容", "category": "活动"}]
    """
    ids = [s["id"] for s in strategies]
    documents = [s["content"] for s in strategies]
    metadatas = [{"category": s.get("category", "default")} for s in strategies]
    
    embeddings = self.embeddings.embed_documents(documents)
    
    self.strategy_collection.add(
      ids=ids,
      embeddings=embeddings,
      documents=documents,
      metadatas=metadatas
    )
    _log.info(f"成功导入 {len(ids)} 条积分策略数据")

  def search_strategy(self, query: str, limit: int = 2) -> List[Dict[str, Any]]:
    """
    搜索最匹配的积分攻略
    返回一个列表，包含最相关的几条策略
    """
    query_vector = self.embeddings.embed_query(query)
    
    results = self.strategy_collection.query(
      query_embeddings=[query_vector],
      n_results=limit
    )
    
    output = []
    if not results["ids"][0]:
      return output

    for i in range(len(results["ids"][0])):
      output.append({
        "content": results["documents"][0][i],
        "category": results["metadatas"][0][i]["category"],
        "distance": results["distances"][0][i]
      })
    
    return output
  
  def add_voucher_knowledge(self, qa_content: str):
    """
    处理原始 Q&A 文本并存入立减金库 (Voucher Store)
    qa_content: 包含 Q: A: 的原始文本
    """
    # 按 Q：拆分
    parts = re.split(r'Q[:：]', qa_content)
    documents = []
    ids = []
    metadatas = []
    
    for idx, part in enumerate(parts):
      if not part.strip():
        continue
      full_content = "Q: " + part.strip()
      ids.append(f"voucher_qa_{idx}")
      documents.append(full_content)
      # 元数据标注为 voucher 类型
      metadatas.append({"source": "official_faq", "type": "standing_voucher"})

    if not documents:
      return

    embeddings = self.embeddings.embed_documents(documents)
    self.voucher_collection.add(
      ids=ids,
      embeddings=embeddings,
      documents=documents,
      metadatas=metadatas
    )
    _log.info(f"成功导入 {len(documents)} 条立减金(Voucher)业务知识")

  def search_voucher_info(self, query: str, limit: int = 2) -> List[str]:
    """搜索立减金(Voucher)相关业务规则"""
    query_vector = self.embeddings.embed_query(query)
    results = self.voucher_collection.query(
      query_embeddings=[query_vector],
      n_results=limit
    )
    
    if results["documents"] and len(results["documents"][0]) > 0:
      return results["documents"][0]
    return []