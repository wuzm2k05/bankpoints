# 2 个空格对齐
import ormsgpack
import json
import asyncio
from typing import Any, Optional, Iterator, AsyncIterator, Sequence
from redis.asyncio import Redis # 核心：使用异步 Redis
from langgraph.checkpoint.base import (
  BaseCheckpointSaver,
  SerializerProtocol,
  CheckpointTuple
)
from loguru import logger as _log

# --- 兼容性序列化器 (保持同步，因为内存处理不涉及 IO) ---
class CrossCompatibleSerializer(SerializerProtocol):
  def dumps(self, obj: Any) -> bytes:
    safe_obj = self._clean_for_serialization(obj)
    try:
      return ormsgpack.packb(
        safe_obj,
        option=ormsgpack.OPT_NON_STR_KEYS,
        default=self._default_encoder
      )
    except Exception as e:
      _log.debug("ormsgpack fallback to JSON: {}", e)
      return json.dumps(safe_obj, default=str).encode("utf-8")

  def _clean_for_serialization(self, obj: Any) -> Any:
    if isinstance(obj, dict):
      return {str(k): self._clean_for_serialization(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple, set)):
      return [self._clean_for_serialization(item) for item in obj]
    elif hasattr(obj, "model_dump") and callable(obj.model_dump):
      return self._clean_for_serialization(obj.model_dump())
    elif hasattr(obj, "dict") and callable(obj.dict):
      return self._clean_for_serialization(obj.dict())
    elif hasattr(obj, "__dict__"):
      return self._clean_for_serialization(vars(obj))
    if isinstance(obj, (str, int, float, bool, type(None))):
      return obj
    return str(obj)

  def _default_encoder(self, obj: Any) -> Any:
    return str(obj)

  def loads(self, data: bytes) -> Any:
    try:
      return ormsgpack.unpackb(data)
    except Exception:
      return json.loads(data.decode("utf-8"))

# --- 异步 SimpleRedisSaver ---
class SimpleRedisSaver(BaseCheckpointSaver):
  def __init__(self, redis_client: Redis, ttl: int = 86400):
    super().__init__()
    self.redis_client = redis_client
    self.ttl = ttl
    self.serde = CrossCompatibleSerializer()

  # 1. 异步存储快照
  async def aput(self, config: dict, checkpoint: Any, metadata: Any, new_versions: dict) -> dict:
    thread_id = str(config["configurable"]["thread_id"])
    checkpoint_id = str(checkpoint["id"])
    key = f"checkpoints:{thread_id}"
    
    blob = self.serde.dumps({
      "checkpoint": checkpoint,
      "metadata": metadata,
      "parent_config": config.get("parent_config")
    })

    async with self.redis_client.pipeline() as pipe:
      pipe.hset(key, checkpoint_id, blob)
      pipe.hset(key, "__latest__", checkpoint_id)
      if self.ttl:
        pipe.expire(key, self.ttl)
      await pipe.execute()

    return {
      "configurable": {
        "thread_id": thread_id, 
        "checkpoint_id": checkpoint_id
      }
    }

  # 2. 异步存储中间写入
  async def aput_writes(self, config: dict, writes: Sequence[Any], task_id: str) -> None:
    thread_id = str(config["configurable"]["thread_id"])
    checkpoint_id = str(config["configurable"]["checkpoint_id"])
    key = f"writes:{thread_id}:{checkpoint_id}"
    
    async with self.redis_client.pipeline() as pipe:
      for idx, write in enumerate(writes):
        write_key = f"{task_id}_{idx}"
        blob = self.serde.dumps(write)
        pipe.hset(key, write_key, blob)
      
      if self.ttl:
        pipe.expire(key, self.ttl)
      await pipe.execute()

  # 3. 异步读取快照
  async def aget_tuple(self, config: dict) -> Optional[CheckpointTuple]:
    thread_id = str(config["configurable"]["thread_id"])
    checkpoint_id = config["configurable"].get("checkpoint_id")
    key = f"checkpoints:{thread_id}"

    if not checkpoint_id:
      res = await self.redis_client.hget(key, "__latest__")
      checkpoint_id = res.decode("utf-8") if res else None
    
    if not checkpoint_id:
      return None

    data = await self.redis_client.hget(key, checkpoint_id)
    if data:
      try:
        content = self.serde.loads(data)
        return CheckpointTuple(
          config={"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}},
          checkpoint=content["checkpoint"],
          metadata=content.get("metadata", {}),
          parent_config=content.get("parent_config"),
          pending_writes=[]
        )
      except Exception as e:
        _log.error("Failed to load checkpoint {}: {}", checkpoint_id, e)
    return None

  # 4. 异步流式历史记录
  async def alist(self, config: dict, *, before: Optional[dict] = None, limit: Optional[int] = None) -> AsyncIterator[CheckpointTuple]:
    thread_id = str(config["configurable"]["thread_id"])
    key = f"checkpoints:{thread_id}"
    count = 0
    
    # 使用异步迭代器 hscan_iter
    async for ckpt_id, data in self.redis_client.hscan_iter(key, match="*", count=100):
      if limit and count >= limit: break
      if ckpt_id == b"__latest__": continue
      try:
        content = self.serde.loads(data)
        yield CheckpointTuple(
          config={"configurable": {"thread_id": thread_id, "checkpoint_id": ckpt_id.decode("utf-8")}},
          checkpoint=content["checkpoint"],
          metadata=content.get("metadata", {}),
          parent_config=content.get("parent_config"),
          pending_writes=[]
        )
        count += 1
      except:
        continue