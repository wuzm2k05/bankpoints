# 2 个空格对齐
import logging
import ormsgpack
from typing import Any, Optional, Iterator, Union
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
  BaseCheckpointSaver,
  Checkpoint,
  CheckpointMetadata,
  CheckpointTuple
)

_log = logging.getLogger(__name__)

class SimpleRedisSaver(BaseCheckpointSaver):
  def __init__(self, redis_client, ttl: int = 86400):
    super().__init__()
    self.client = redis_client
    self.ttl = ttl
    self.schema_version = "v1" # 预留迁移能力

  def _get_id(self, obj: Any) -> Optional[str]:
    """风险 1 修复：稳健获取 checkpoint_id，兼容 dict 或 dataclass"""
    if isinstance(obj, dict):
      return obj.get("id")
    return getattr(obj, "id", None)

  def _serialize(self, obj: Any) -> bytes:
    return ormsgpack.packb(
      obj,
      option=(
        ormsgpack.OPT_NON_STR_KEYS | 
        ormsgpack.OPT_SERIALIZE_DATACLASS | 
        ormsgpack.OPT_PASSTHROUGH_DATETIME
      ),
      default=self._default_encoder
    )

  def _deserialize(self, data: bytes) -> Any:
    return ormsgpack.unpackb(data)

  def _default_encoder(self, obj: Any) -> Any:
    """风险 3 修复：Fail-Fast 策略，拒绝静默数据腐化"""
    if hasattr(obj, "dict") and callable(obj.dict):
      return obj.dict()
    
    obj_str = str(type(obj))
    if "ChainMap" in obj_str:
      return dict(obj)
    if "Runtime" in obj_str:
      return "<Runtime_Context>"
    
    # 强制抛错：确保在开发阶段就暴露出未适配的复杂类型
    raise TypeError(f"[Serialization Error] Unsupported type: {type(obj)}. Add it to _default_encoder.")

  def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
    thread_id = config["configurable"]["thread_id"]
    checkpoint_id = config["configurable"].get("checkpoint_id")
    key = f"checkpoint:{thread_id}:{checkpoint_id}" if checkpoint_id else f"checkpoint:{thread_id}:latest"
    
    data = self.client.get(key)
    if not data: return None

    try:
      # 支持 Schema Versioning 的解包
      raw = self._deserialize(data)
      payload = raw["p"] if isinstance(raw, dict) and "v" in raw else raw
      checkpoint, metadata, parent_config = payload
      
      actual_id = self._get_id(checkpoint)
      
      pipe = self.client.pipeline()
      pipe.expire(f"checkpoint:{thread_id}:latest", self.ttl)
      pipe.expire(f"checkpoint:{thread_id}:{actual_id}", self.ttl)
      pipe.execute()
      
      return CheckpointTuple(
        config={"configurable": {"thread_id": thread_id, "checkpoint_id": actual_id}},
        checkpoint=checkpoint,
        metadata=metadata,
        parent_config=parent_config
      )
    except Exception as e:
      _log.error(f"Checkpoint recovery failed: {e}")
      return None

  def list(
    self,
    config: Optional[RunnableConfig],
    *,
    before: Optional[RunnableConfig] = None,
    limit: Optional[int] = None,
  ) -> Iterator[CheckpointTuple]:
    """风险 2 修复：基于 checkpoint 逻辑顺序排序而非 Key 字符串顺序"""
    if not config: return
    thread_id = config["configurable"]["thread_id"]
    before_id = before["configurable"].get("checkpoint_id") if before else None
    
    pattern = f"checkpoint:{thread_id}:*"
    keys = [k for k in self.client.scan_iter(match=pattern, count=100) if b":latest" not in k]
    
    all_tuples = []
    for key in keys:
      data = self.client.get(key)
      if not data: continue
      try:
        raw = self._deserialize(data)
        payload = raw["p"] if isinstance(raw, dict) and "v" in raw else raw
        cp, meta, pc = payload
        all_tuples.append(CheckpointTuple(
          config={"configurable": {"thread_id": thread_id, "checkpoint_id": self._get_id(cp)}},
          checkpoint=cp, metadata=meta, parent_config=pc
        ))
      except: continue

    # 真正的逻辑排序：按 checkpoint ID 或时间戳降序
    # 注意：LangGraph 的 ID 通常包含版本序，这里按 ID 降序通常符合 list() 语义
    all_tuples.sort(key=lambda x: self._get_id(x.checkpoint) or "", reverse=True)

    count = 0
    found_before = not before_id
    for t in all_tuples:
      curr_id = self._get_id(t.checkpoint)
      if before_id and not found_before:
        if curr_id == before_id: found_before = True
        continue
      yield t
      count += 1
      if limit and count >= limit: break

  def put(
    self,
    config: RunnableConfig,
    checkpoint: Checkpoint,
    metadata: CheckpointMetadata,
    new_versions: Any,
  ) -> RunnableConfig:
    thread_id = config["configurable"]["thread_id"]
    checkpoint_id = self._get_id(checkpoint)
    
    safe_conf = {"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}}
    
    try:
      # 增加版本包装，方便未来做数据迁移 (🔧 建议 5)
      envelope = {
        "v": self.schema_version,
        "p": (checkpoint, metadata, safe_conf)
      }
      data = self._serialize(envelope)
      
      self.client.setex(f"checkpoint:{thread_id}:{checkpoint_id}", self.ttl, data)
      self.client.setex(f"checkpoint:{thread_id}:latest", self.ttl, data)
    except Exception as e:
      _log.error(f"Critical: Failed to persist checkpoint: {e}")
      raise e
      
    return {"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}}