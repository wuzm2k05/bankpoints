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

  def get_tuple(self, config: RunnableConfig) -> Optional[CheckpointTuple]:
    """获取状态并同步续期最新指针与实体快照 (Pipeline 优化版)"""
    thread_id = config["configurable"]["thread_id"]
    checkpoint_id = config["configurable"].get("checkpoint_id")
    
    # 优先查找具体 ID，否则找最新的 latest 指针
    key = f"checkpoint:{thread_id}:{checkpoint_id}" if checkpoint_id else f"checkpoint:{thread_id}:latest"

    data = self.client.get(key)
    if not data:
      return None

    try:
      # JsonPlusSerializer 必须使用 loads_typed 以正确还原消息对象
      checkpoint, metadata, parent_config = self.serde.loads_typed(data)
      actual_id = checkpoint["id"]
      
      # 使用 Pipeline 减少网络往返时间 (RTT)，同步续期指针和实体
      pipe = self.client.pipeline()
      pipe.expire(f"checkpoint:{thread_id}:latest", self.ttl)
      pipe.expire(f"checkpoint:{thread_id}:{actual_id}", self.ttl)
      pipe.execute()
      
      # 补全 config 中的 checkpoint_id，确保版本追踪准确
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
      _log.error(f"反序列化 Checkpoint 失败: {e}")
      return None

  def list(
    self,
    config: Optional[RunnableConfig],
    *,
    before: Optional[RunnableConfig] = None,
    limit: Optional[int] = None,
  ) -> Iterator[CheckpointTuple]:
    """流式罗列历史快照，尊重 before 参数并节省内存 (边扫边产出)"""
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
    
    # 2. 预排序 Keys（按 ID 倒序，通常符合时间序）
    all_keys.sort(reverse=True)

    count = 0
    found_before = False if before_id else True
    
    # 3. 边读边 yield，防止大列表占用内存
    for key in all_keys:
      data = self.client.get(key)
      if not data: continue
      
      checkpoint, metadata, parent_config = self.serde.loads_typed(data)
      curr_id = checkpoint["id"]
      
      # 处理 before 逻辑：跳过直到匹配到 before_id 之后
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
    """持久化状态并执行滑动续期"""
    thread_id = config["configurable"]["thread_id"]
    checkpoint_id = checkpoint["id"]
    
    key = f"checkpoint:{thread_id}:{checkpoint_id}"
    latest_key = f"checkpoint:{thread_id}:latest"
    
    # 使用 JsonPlus 序列化 typed 版本，确保消息类 (HumanMessage等) 能被还原
    data = self.serde.dumps_typed((checkpoint, metadata, config))
    
    # 写续期：SETEX 原子化完成存储与过期设置
    self.client.setex(key, self.ttl, data)
    self.client.setex(latest_key, self.ttl, data)
    
    _log.info(f"Checkpoint 存入完成: {thread_id} ({checkpoint_id}), TTL: {self.ttl}s")
    
    return {
      "configurable": {
        "thread_id": thread_id,
        "checkpoint_id": checkpoint_id
      }
    }