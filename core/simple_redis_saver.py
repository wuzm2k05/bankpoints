# 2 个空格对齐
import logging
import json
from typing import Any, Optional, Iterator
from langchain_core.runnables import RunnableConfig
from langchain_core.load import dumps, loads # 使用官方序列化工具
from langgraph.checkpoint.base import (
  BaseCheckpointSaver, Checkpoint, CheckpointMetadata, CheckpointTuple, SerializerProtocol
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

_log = logging.getLogger(__name__)

class SimpleRedisSaver(BaseCheckpointSaver):
  def __init__(self, redis_client, ttl: int = 86400, serde: Optional[SerializerProtocol] = None):
    super().__init__(serde=serde or JsonPlusSerializer())
    self.client = redis_client
    self.ttl = ttl

  def _to_serializable(self, obj: Any) -> Any:
    """更智能的序列化预处理"""
    if isinstance(obj, dict):
      return {str(k): self._to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
      return [self._to_serializable(x) for x in obj]
    
    # 核心逻辑：如果是 LangChain 对象（如 Message），尝试官方序列化
    if hasattr(obj, "lc_serializable") and obj.lc_serializable:
      try:
        # 转为 json-safe dict
        return json.loads(dumps(obj)) 
      except: pass
      
    # 处理 ChainMap
    if "ChainMap" in str(type(obj)):
      return dict(obj)
      
    # 基础类型
    if isinstance(obj, (str, int, float, bool, type(None))):
      return obj
      
    # 实在不认识的（如 Runtime），转字符串防止崩溃
    return f"<{type(obj).__name__}>"

  def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
    thread_id = config["configurable"]["thread_id"]
    checkpoint_id = config["configurable"].get("checkpoint_id")
    key = f"checkpoint:{thread_id}:{checkpoint_id}" if checkpoint_id else f"checkpoint:{thread_id}:latest"
    data = self.client.get(key)
    if not data: return None
    try:
      checkpoint, metadata, parent_config = self.serde.loads_typed(data)
      # 性能优化：Pipeline 续命
      pipe = self.client.pipeline()
      pipe.expire(f"checkpoint:{thread_id}:latest", self.ttl)
      pipe.expire(f"checkpoint:{thread_id}:{checkpoint['id']}", self.ttl)
      pipe.execute()
      return CheckpointTuple(
        {"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint["id"]}},
        checkpoint, metadata, parent_config
      )
    except Exception as e:
      _log.error(f"Recovery Error: {e}")
      return None

  def list(self, config: Optional[RunnableConfig], *, before: Optional[RunnableConfig] = None, limit: Optional[int] = None) -> Iterator[CheckpointTuple]:
    if not config: return
    thread_id = config["configurable"]["thread_id"]
    before_id = before["configurable"].get("checkpoint_id") if before else None
    pattern = f"checkpoint:{thread_id}:*"
    all_keys = [k for k in self.client.scan_iter(match=pattern, count=100) if b":latest" not in k]
    all_keys.sort(reverse=True)
    count = 0
    found_before = not before_id
    for key in all_keys:
      data = self.client.get(key)
      if not data: continue
      try:
        cp, meta, pc = self.serde.loads_typed(data)
        if before_id and not found_before:
          if cp["id"] == before_id: found_before = True
          continue
        yield CheckpointTuple({"configurable": {"thread_id": thread_id, "checkpoint_id": cp["id"]}}, cp, meta, pc)
        count += 1
        if limit and count >= limit: break
      except: continue

  def put(self, config: RunnableConfig, checkpoint: Checkpoint, metadata: CheckpointMetadata, new_versions: Any) -> RunnableConfig:
    thread_id = config["configurable"]["thread_id"]
    checkpoint_id = checkpoint["id"]
    
    # 预处理数据
    safe_checkpoint = self._to_serializable(checkpoint)
    safe_metadata = self._to_serializable(metadata)
    # 只存关键 config
    safe_config = {"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}}
    
    try:
      data = self.serde.dumps_typed((safe_checkpoint, safe_metadata, safe_config))
      self.client.setex(f"checkpoint:{thread_id}:{checkpoint_id}", self.ttl, data)
      self.client.setex(f"checkpoint:{thread_id}:latest", self.ttl, data)
    except Exception as e:
      _log.error(f"Put failed: {e}")
      raise e
    return {"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}}