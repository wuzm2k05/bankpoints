# 2 个空格对齐
import logging
from typing import List, Optional, Dict, Any
import chromadb
from langchain_community.embeddings import DashScopeEmbeddings

_log = logging.getLogger(__name__)

class ICBCVectorDB:
  def __init__(self, api_key: str):
    # 1. 初始化 ChromaDB 持久化客户端
    self.client = chromadb.PersistentClient(path="./icbc_vector_db")
    
    # 2. 初始化 Qwen Embedding 接口
    self.embeddings = DashScopeEmbeddings(
      model="text-embedding-v3",
      dashscope_api_key=api_key
    )
    
    # 3. 定义商品 Collection
    self.product_collection = self.client.get_or_create_collection(
      name="icbc_products"
    )

    # 4. 定义策略/攻略 Collection
    self.strategy_collection = self.client.get_or_create_collection(
      name="icbc_strategies"
    )

  def add_products(self, products: List[Dict[str, Any]]):
    """批量添加商品数据"""
    ids = [p["id"] for p in products]
    documents = [f"{p['name']}: {p['desc']}" for p in products]
    metadatas = [{"name": p["name"], "points": p["points"]} for p in products]
    
    embeddings = self.embeddings.embed_documents(documents)
    
    self.product_collection.add(
      ids=ids,
      embeddings=embeddings,
      documents=documents,
      metadatas=metadatas
    )
    _log.info(f"成功导入 {len(ids)} 条商品数据")

  def search(self, query: str, limit: int = 1) -> Optional[Dict[str, Any]]:
    """语义搜索最匹配的商品"""
    query_vector = self.embeddings.embed_query(query)
    results = self.product_collection.query(
      query_embeddings=[query_vector],
      n_results=limit
    )
    
    if not results["ids"][0]:
      return None
      
    metadata = results["metadatas"][0][0]
    distance = results["distances"][0][0]
    
    return {
      "name": metadata["name"],
      "points": metadata["points"],
      "distance": distance
    }

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