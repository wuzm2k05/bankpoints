import sqlite3,os
import asyncio
import json
import uuid
from contextlib import closing
from typing import List, Dict, Any, Optional
from .db_base import BaseGoodsRepository
import config.config as config

class SQLiteGoodsRepository(BaseGoodsRepository):
  def __init__(self):
    # 🌟 核心改进 1：将任何传入的相对路径，安全地转换为基于项目根目录的“绝对路径”
    # 这样不管在哪个目录下运行命令，路径都不会偏移
    db_path = config.get_sqlite_db_path()
    if not os.path.isabs(db_path):
      # 寻找当前 repository 文件所在位置的上一级（即项目根目录）
      base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
      self.db_path = os.path.abspath(os.path.join(base_dir, db_path))
    else:
      self.db_path = os.path.abspath(db_path)

    # 🌟 核心改进 2：再次确保其父目录物理存在
    db_dir = os.path.dirname(self.db_path)
    if db_dir and not os.path.exists(db_dir):
      os.makedirs(db_dir, exist_ok=True)
      
    self._init_db()

  def _get_connection(self):
    try:
      # 1. 设置 timeout=10.0 秒
      conn = sqlite3.connect(self.db_path, timeout=10.0)
      conn.row_factory = sqlite3.Row
      return conn
    except sqlite3.OperationalError as e:
      # 🌟 核心改进 3：如果依然打不开，精准打印出到底是哪个“绝对路径”无法打开
      print(f"\n❌ [SQLite 错误] 无法打开或创建数据库文件！")
      print(f"👉 尝试访问的绝对路径为: {self.db_path}")
      print(f"👉 系统报错原因: {e}\n")
      raise e

  def _init_db(self):
    """初始化表结构，并强行开启多进程安全的 WAL 模式"""
    with closing(self._get_connection()) as conn:
      cursor = conn.cursor()
      
      # 开启 WAL 模式：允许多进程读写并发（读写互不阻塞）
      cursor.execute("PRAGMA journal_mode=WAL;")
      # 降低磁盘同步级别：提高多进程高频写入性能
      cursor.execute("PRAGMA synchronous=NORMAL;")

      # 创建商品表：主键为 product_id
      cursor.execute('''
        CREATE TABLE IF NOT EXISTS goods (
          product_id TEXT PRIMARY KEY,
          description TEXT,
          link TEXT,
          category TEXT,
          extra TEXT
        )
      ''')
      cursor.execute("CREATE INDEX IF NOT EXISTS idx_goods_category ON goods(category)")
      conn.commit()

  async def query(self, product_id: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    return await asyncio.to_thread(self._sync_query, product_id, limit)

  def _sync_query(self, product_id: Optional[str], limit: Optional[int]) -> List[Dict[str, Any]]:
    with closing(self._get_connection()) as conn:
      cursor = conn.cursor()
      if product_id:
        cursor.execute("SELECT * FROM goods WHERE product_id = ?", (product_id,))
      elif limit:
        cursor.execute("SELECT * FROM goods LIMIT ?", (limit,))
      else:
        cursor.execute("SELECT * FROM goods")
      
      results = []
      for row in cursor.fetchall():
        item = dict(row)
        if item.get("extra"):
          try:
            item["extra"] = json.loads(item["extra"])
          except Exception:
            pass
        results.append(item)
      return results

  async def add(self, data: Dict[str, Any]) -> str:
    return await asyncio.to_thread(self._sync_add, data)

  def _sync_add(self, data: Dict[str, Any]) -> str:
    data_copy = data.copy()
    
    # 1. 自动生成一个全局唯一的 product_id (例如: 'e3b0c44298fc1c149afbf4c8996fb924')
    generated_id = uuid.uuid4().hex
    data_copy["product_id"] = generated_id

    if "extra" in data_copy and isinstance(data_copy["extra"], dict):
      data_copy["extra"] = json.dumps(data_copy["extra"], ensure_ascii=False)

    keys = ", ".join(data_copy.keys())
    placeholders = ", ".join(["?" for _ in data_copy])

    with closing(self._get_connection()) as conn:
      cursor = conn.cursor()
      try:
        cursor.execute("BEGIN IMMEDIATE TRANSACTION;")
        # 严格执行插入操作
        cursor.execute(f"INSERT INTO goods ({keys}) VALUES ({placeholders})", tuple(data_copy.values()))
        conn.commit()
        return generated_id
      except Exception as e:
        conn.rollback()
        raise e

  async def update(self, product_id: str, data: Dict[str, Any]) -> bool:
    return await asyncio.to_thread(self._sync_update, product_id, data)

  def _sync_update(self, product_id: str, data: Dict[str, Any]) -> bool:
    data_copy = data.copy()
    
    # 保证更新数据中不包含主键
    data_copy.pop("product_id", None)

    if "extra" in data_copy and isinstance(data_copy["extra"], dict):
      data_copy["extra"] = json.dumps(data_copy["extra"], ensure_ascii=False)

    fields = ", ".join([f"{k} = ?" for k in data_copy.keys()])
    values = list(data_copy.values()) + [product_id]

    with closing(self._get_connection()) as conn:
      cursor = conn.cursor()
      try:
        cursor.execute("BEGIN IMMEDIATE TRANSACTION;")
        
        # 严格更新：如果商品不存在，更新行数为 0
        cursor.execute(f"UPDATE goods SET {fields} WHERE product_id = ?", tuple(values))
        
        if cursor.rowcount == 0:
          # 不存在该商品，直接回滚并返回 False，防止误新增或产生脏数据
          conn.rollback()
          return False
        
        conn.commit()
        return True
      except Exception as e:
        conn.rollback()
        raise e

  async def delete(self, product_id: str) -> bool:
    return await asyncio.to_thread(self._sync_delete, product_id)

  def _sync_delete(self, product_id: str) -> bool:
    with closing(self._get_connection()) as conn:
      cursor = conn.cursor()
      try:
        cursor.execute("BEGIN IMMEDIATE TRANSACTION;")
        cursor.execute("DELETE FROM goods WHERE product_id = ?", (product_id,))
        conn.commit()
        return cursor.rowcount > 0
      except Exception as e:
        conn.rollback()
        raise e