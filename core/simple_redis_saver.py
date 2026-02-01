# 2 个空格对齐
import logging
from typing import Any, Optional, Iterator
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
  BaseCheckpointSaver,
  Checkpoint,
  CheckpointMetadata,
  CheckpointTuple,
  SerializerProtocol
)
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

_log = logging.getLogger(__name__)

class SimpleRedisSaver(BaseCheckpointSaver):
  def __init__(
    self, 
    redis_client, 
    ttl: int = 86400, 
    serde: Optional[SerializerProtocol] = None
  ):
    """
    :param redis_client: 需设置 decode_responses=False 的 redis 实例
    :param ttl: 过期时间（秒），默认 24 小时
    :param serde: 序列化器，默认为官方推荐的 JsonPlusSerializer
    """
    super().__init__(serde=serde or JsonPlusSerializer())
    self.client = redis_client
    self.ttl = ttl

  def _make_safe(self, obj: Any) -> Any:
    """递归确保对象可被 msgpack/json 序列化，处理 ChainMap 和 Runtime 对象"""
    if isinstance(obj, dict):
      return {k: self._make_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
      return [self._make_safe(x) for x in obj]
    
    # 处理特殊类型：ChainMap 转 dict, Runtime 等不可序列化对象转字符串
    obj_type_str = str(type(obj))
    if "ChainMap" in obj_type_str:
      return dict(obj)
    if "Runtime" in obj_type_str:
      return f"<Runtime_Object>"
    return obj

  def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
    """获取状态并使用 Pipeline 同步续期最新指针与实体快照"""
    thread_id = config["configurable"]["thread_id"]
    checkpoint_id = config["configurable"].get("checkpoint_id")
    
    key = f"checkpoint:{thread_id}:{checkpoint_id}" if checkpoint_id else f"checkpoint:{thread_id}:latest"

    data = self.client.get(key)
    if not data:
      return None

    try:
      # 使用 loads_typed 还原 LangGraph 对象
      checkpoint, metadata, parent_config = self.serde.loads_typed(data)
      actual_id = checkpoint["id"]
      
      # 性能优化：使用 Pipeline 批量续期，减少 RTT
      pipe = self.client.pipeline()
      pipe.expire(f"checkpoint:{thread_id}:latest", self.ttl)
      pipe.expire(f"checkpoint:{thread_id}:{actual_id}", self.ttl)
      pipe.execute()
      
      # 补全 config
      final_config = {
        "configurable": {
          "thread_id": thread_id,
          "checkpoint_id": actual_id
        }
      }
      return CheckpointTuple(
        config=final_config,
        checkpoint=checkpoint,
        metadata=metadata,
        parent_config=parent_config
      )
    except Exception as e:
      _log.error(f"从 Redis 恢复 Checkpoint 失败: {e}")
      return None

  def list(
    self,
    config: Optional[RunnableConfig],
    *,
    before: Optional[RunnableConfig] = None,
    limit: Optional[int] = None,
  ) -> Iterator[CheckpointTuple]:
    """流式罗列历史快照，内存友好且尊重 before 过滤逻辑"""
    if not config: return
    thread_id = config["configurable"]["thread_id"]
    before_id = before["configurable"].get("checkpoint_id") if before else None
    
    # 1. 使用 scan_iter 避免 O(N) 阻塞 Redis 线程
    pattern = f"checkpoint:{thread_id}:*"
    all_keys = []
    for key in self.client.scan_iter(match=pattern, count=100):
      key_str = key.decode() if isinstance(key, bytes) else key
      if ":latest" not in key_str:
        all_keys.append(key)
    
    # 2. 预排序 Keys（按时间序倒序）
    all_keys.sort(reverse=True)

    count = 0
    found_before = False if before_id else True
    
    # 3. 边读边 yield (Streaming)
    for key in all_keys:
      data = self.client.get(key)
      if not data: continue
      
      checkpoint, metadata, parent_config = self.serde.loads_typed(data)
      curr_id = checkpoint["id"]
      
      # 处理 before 过滤
      if before_id and not found_before:
        if curr_id == before_id:
          found_before = True
        continue
      
      yield CheckpointTuple(
        config={"configurable": {"thread_id": thread_id, "checkpoint_id": curr_id}},
        checkpoint=checkpoint,
        metadata=metadata,
        parent_config=parent_config
      )
      
      count += 1
      if limit and count >= limit:
        break

  def put(
    self,
    config: RunnableConfig,
    checkpoint: Checkpoint,
    metadata: CheckpointMetadata,
    new_versions: Any,
  ) -> RunnableConfig:
    """持久化状态，自动处理不可序列化对象并执行滑动续期"""
    thread_id = config["configurable"]["thread_id"]
    checkpoint_id = checkpoint["id"]
    
    key = f"checkpoint:{thread_id}:{checkpoint_id}"
    latest_key = f"checkpoint:{thread_id}:latest"
    
    # 核心修正：深度清洗数据，防止 msgpack 序列化崩溃
    safe_checkpoint = self._make_safe(checkpoint)
    safe_config = {"configurable": dict(config.get("configurable", {}))}
    
    try:
      # 使用 dumps_typed 进行序列化
      data = self.serde.dumps_typed((safe_checkpoint, metadata, safe_config))
      
      # 写续期：原子化存储
      self.client.setex(key, self.ttl, data)
      self.client.setex(latest_key, self.ttl, data)
      
      _log.info(f"Checkpoint 存入成功: {thread_id} ({checkpoint_id})")
    except Exception as e:
      _log.error(f"序列化 Checkpoint 失败，数据未存入: {e}")
      raise e # 抛出异常防止静默失败
    
    return {
      "configurable": {
        "thread_id": thread_id,
        "checkpoint_id": checkpoint_id
      }
    }