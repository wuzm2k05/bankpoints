import ormsgpack
import json
from typing import Any, Optional, Iterator, Sequence
from redis import Redis
from langgraph.checkpoint.base import (
  BaseCheckpointSaver,
  SerializerProtocol
)

import log.logger as logger
_log = logger.get_logger()

# --- 兼容性序列化器：具备数据清洗与自动回退功能 ---
class CrossCompatibleSerializer(SerializerProtocol):
  """
  专为 Redis/Valkey 设计的序列化器：
  1. 递归清洗非字符串 Key，防止 ormsgpack 报错。
  2. 优先使用二进制 ormsgpack，失败则回退至 JSON。
  """
  def dumps(self, obj: Any) -> bytes:
    # 在序列化前先进行数据清洗，确保所有字典 Key 均为字符串
    safe_obj = self._clean_for_serialization(obj)
    try:
      return ormsgpack.packb(
        safe_obj,
        option=ormsgpack.OPT_NON_STR_KEYS,
        default=self._default_encoder
      )
    except Exception as e:
      # 如果 ormsgpack 仍然失败，回退到通用的 JSON 编码
      _log.debug(f"ormsgpack fallback to JSON: {e}")
      return json.dumps(safe_obj, default=str).encode("utf-8")

  def _clean_for_serialization(self, obj: Any) -> Any:
    """递归清理对象，确保所有 dict keys 都是字符串，并转换不可序列化类型"""
    if isinstance(obj, dict):
      return {str(k): self._clean_for_serialization(v) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple, set)):
      return [self._clean_for_serialization(item) for item in obj]
    elif hasattr(obj, "model_dump") and callable(obj.model_dump):
      # 适配 Pydantic v2
      return self._clean_for_serialization(obj.model_dump())
    elif hasattr(obj, "dict") and callable(obj.dict):
      # 适配 Pydantic v1 / LangChain 对象
      return self._clean_for_serialization(obj.dict())
    elif hasattr(obj, "__dict__"):
      return self._clean_for_serialization(vars(obj))
    
    # 基础类型直接返回
    if isinstance(obj, (str, int, float, bool, type(None))):
      return obj
    return str(obj)

  def _default_encoder(self, obj: Any) -> Any:
    """ormsgpack 的最后一道防线"""
    return str(obj)

  def loads(self, data: bytes) -> Any:
    try:
      return ormsgpack.unpackb(data)
    except Exception:
      # 尝试解析回退的 JSON 数据
      return json.loads(data.decode("utf-8"))

# --- 生产级 SimpleRedisSaver ---
class SimpleRedisSaver(BaseCheckpointSaver):
  """
  适配 LangGraph 1.0.7 的 Redis 检查点管理器。
  使用 Hash 结构存储，支持自动清理 (TTL) 和最新快照快速定位。
  """
  def __init__(self, redis_client: Redis, ttl: int = 86400):
    super().__init__()
    self.redis_client = redis_client
    self.ttl = ttl
    self.serde = CrossCompatibleSerializer()

  def put(self, config: dict, checkpoint: Any, metadata: Any, new_versions: dict) -> dict:
    """持久化保存当前状态"""
    thread_id = str(config["configurable"]["thread_id"])
    checkpoint_id = str(checkpoint["id"])
    key = f"checkpoints:{thread_id}"
    
    blob = self.serde.dumps({
      "checkpoint": checkpoint,
      "metadata": metadata,
      "parent_config": config.get("parent_config")
    })

    pipe = self.redis_client.pipeline()
    # 存储快照内容
    pipe.hset(key, checkpoint_id, blob)
    # 更新最新指针，方便快速获取
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
    """保存节点执行过程中的中间写入数据 (LangGraph 1.0.x 强制要求)"""
    thread_id = str(config["configurable"]["thread_id"])
    checkpoint_id = str(config["configurable"]["checkpoint_id"])
    key = f"writes:{thread_id}:{checkpoint_id}"
    
    pipe = self.redis_client.pipeline()
    for idx, write in enumerate(writes):
      write_key = f"{task_id}_{idx}"
      blob = self.serde.dumps(write)
      pipe.hset(key, write_key, blob)
    
    if self.ttl:
      pipe.expire(key, self.ttl)
    pipe.execute()

  def get_tuple(self, config: dict) -> Optional[Any]:
    """读取指定的或最新的状态快照"""
    from langgraph.checkpoint.base import CheckpointTuple 
    
    thread_id = str(config["configurable"]["thread_id"])
    checkpoint_id = config["configurable"].get("checkpoint_id")
    key = f"checkpoints:{thread_id}"

    # 如果未指定 ID，则查找最新锚点
    if not checkpoint_id:
      res = self.redis_client.hget(key, "__latest__")
      checkpoint_id = res.decode("utf-8") if res else None
    
    if not checkpoint_id:
      return None

    data = self.redis_client.hget(key, checkpoint_id)
    if data:
      try:
        content = self.serde.loads(data)
        # 脏数据防御机制
        if not isinstance(content, dict) or "checkpoint" not in content:
          return None

        return CheckpointTuple(
          config={"configurable": {"thread_id": thread_id, "checkpoint_id": checkpoint_id}},
          checkpoint=content["checkpoint"],
          metadata=content.get("metadata", {}),
          parent_config=content.get("parent_config"),
          pending_writes=[] # 1.0.7 兼容字段
        )
      except Exception as e:
        _log.error(f"Failed to load checkpoint {checkpoint_id}: {e}")
    return None

  def list(self, config: dict, *, before: Optional[dict] = None, limit: Optional[int] = None) -> Iterator[Any]:
    """流式列出历史状态记录"""
    from langgraph.checkpoint.base import CheckpointTuple
    
    thread_id = str(config["configurable"]["thread_id"])
    key = f"checkpoints:{thread_id}"
    count = 0
    # 使用 HSCAN 以避免在大 Hash 下阻塞 Redis
    for ckpt_id, data in self.redis_client.hscan_iter(key, match="*", count=100):
      if limit and count >= limit: break
      if ckpt_id == b"__latest__": continue
      try:
        content = self.serde.loads(data)
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