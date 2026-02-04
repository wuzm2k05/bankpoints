import ormsgpack
import json
from typing import Any, Optional, Iterator, Sequence
from redis import Redis
from langgraph.checkpoint.base import (
  BaseCheckpointSaver,
  SerializerProtocol
)

# 1.0.7 版本中，Checkpoint 和相关元组通常在运行时动态注入
# 我们通过通用类型来避免导入错误

import log.logger as logger
_log = logger.get_logger()

# --- 兼容性序列化器 ---
class CrossCompatibleSerializer(SerializerProtocol):
  def dumps(self, obj: Any) -> bytes:
    try:
      return ormsgpack.packb(
        obj,
        option=ormsgpack.OPT_NON_STR_KEYS,
        default=self._default_encoder
      )
    except Exception as e:
      _log.debug(f"ormsgpack fallback to JSON: {e}")
      return json.dumps(obj, default=str).encode("utf-8")

  def _default_encoder(self, obj: Any) -> Any:
    if hasattr(obj, "dict") and callable(obj.dict):
      return obj.dict()
    if hasattr(obj, "__dict__"):
      return vars(obj)
    obj_type = str(type(obj))
    if any(x in obj_type for x in ["ChainMap", "Runtime", "ToolCall", "Message"]):
      try: return dict(obj)
      except: return str(obj)
    return str(obj)

  def loads(self, data: bytes) -> Any:
    try:
      return ormsgpack.unpackb(data)
    except Exception:
      return json.loads(data.decode("utf-8"))

# --- 针对 1.0.7 优化的 SimpleRedisSaver ---
class SimpleRedisSaver(BaseCheckpointSaver):
  def __init__(self, redis_client: Redis, ttl: int = 86400):
    # 1.0.7 的 BaseCheckpointSaver 构造函数通常只需要 serde
    # 我们这里手动赋值以确保兼容
    super().__init__()
    self.client = redis_client
    self.ttl = ttl
    self.serde = CrossCompatibleSerializer()

  def put(self, config: dict, checkpoint: Any, metadata: Any, new_versions: dict) -> dict:
    thread_id = str(config["configurable"]["thread_id"])
    checkpoint_id = str(checkpoint["id"])
    key = f"checkpoints:{thread_id}"
    
    blob = self.serde.dumps({
      "checkpoint": checkpoint,
      "metadata": metadata,
      "parent_config": config.get("parent_config")
    })

    pipe = self.client.pipeline()
    pipe.hset(key, checkpoint_id, blob)
    pipe.hset(key, "__latest__", checkpoint_id)
    if self.ttl:
      pipe.expire(key, self.ttl)
    pipe.execute()

    return {
      "configurable": {
        "thread_id": thread_id, 
        "checkpoint_id": checkpoint_id
      }
    }

  def put_writes(self, config: dict, writes: Sequence[Any], task_id: str) -> None:
    thread_id = str(config["configurable"]["thread_id"])
    checkpoint_id = str(config["configurable"]["checkpoint_id"])
    key = f"writes:{thread_id}:{checkpoint_id}"
    
    pipe = self.client.pipeline()
    for idx, write in enumerate(writes):
      write_key = f"{task_id}_{idx}"
      # 在 1.0.7 中，write 可能是元组也可能是对象，直接序列化即可
      blob = self.serde.dumps(write)
      pipe.hset(key, write_key, blob)
    
    if self.ttl:
      pipe.expire(key, self.ttl)
    pipe.execute()

  def get_tuple(self, config: dict) -> Optional[Any]:
    from langgraph.checkpoint.base import CheckpointTuple 
    
    thread_id = str(config["configurable"]["thread_id"])
    checkpoint_id = config["configurable"].get("checkpoint_id")
    key = f"checkpoints:{thread_id}"

    if not checkpoint_id:
      res = self.client.hget(key, "__latest__")
      checkpoint_id = res.decode("utf-8") if res else None
    
    if not checkpoint_id:
      return None

    data = self.client.hget(key, checkpoint_id)
    if data:
      try:
        content = self.serde.loads(data)
        # --- 防御性检查开始 ---
        if not isinstance(content, dict) or "checkpoint" not in content:
          _log.warning(f"发现不兼容的脏数据，已跳过: {checkpoint_id}")
          return None
        # --- 防御性检查结束 ---

        return CheckpointTuple(
          config={"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}},
          checkpoint=content["checkpoint"],
          metadata=content.get("metadata", {}),
          parent_config=content.get("parent_config"),
          pending_writes=[]
        )
      except Exception as e:
        _log.error(f"解析 Checkpoint 失败: {checkpoint_id}, {e}")
    return None

  def list(self, config: dict, *, before: Optional[dict] = None, limit: Optional[int] = None) -> Iterator[Any]:
    from langgraph.checkpoint.base import CheckpointTuple
    
    thread_id = str(config["configurable"]["thread_id"])
    key = f"checkpoints:{thread_id}"
    count = 0
    # 使用 hscan_iter 避免阻塞
    for ckpt_id, data in self.client.hscan_iter(key, match="*", count=100):
      if limit and count >= limit: break
      if ckpt_id == b"__latest__": continue
      try:
        content = self.serde.loads(data)
        # 同样的防御性检查
        if isinstance(content, dict) and "checkpoint" in content:
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