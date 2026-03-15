# 2 个空格对齐
import uuid
from typing import Optional
from redis.asyncio import Redis

import config.config as config
from loguru import logger as _log

class TokenManager:
  def __init__(self, ttl: int = 7200):
    self.ttl = ttl
    self.prefix = config.get_token_redis_prefix()
    self.redis_client: Optional[Redis] = None

  def set_client(self, client: Redis):
    """从外部注入共享的异步 Redis 客户端"""
    self.redis_client = client

  def _get_key(self, token_str: str) -> str:
    return f"{self.prefix}{token_str}"

  async def get_new_token(self) -> dict:
    """
    cmd: getNewToken (异步版)
    """
    try:
      new_token = str(uuid.uuid4().hex)
      key = self._get_key(new_token)
      # 异步设置过期键
      await self.redis_client.setex(key, self.ttl, "1")
      
      return {
        "token": new_token,
        "expireTimeInSeconds": self.ttl,
        "status": "success"
      }
    except Exception as e:
      _log.error("Token 生成失败: {}", e)
      return {"status": "fail", "errorCode": "REDIS_ERROR", "errorMsg": str(e)}

  async def cancel_token(self, token_str: str) -> dict:
    """
    cmd: cancelToken (异步版)
    """
    try:
      key = self._get_key(token_str)
      result = await self.redis_client.delete(key)
      if result > 0:
        return {"status": "success"}
      return {"status": "fail", "errorCode": "NOT_FOUND"}
    except Exception as e:
      _log.error("Token 取消失败: {}", e)
      return {"status": "fail", "errorMsg": str(e)}

  async def verify_token(self, token_str: str) -> bool:
    """
    异步校验逻辑
    """
    if not token_str or not self.redis_client:
      return False
    try:
      return await self.redis_client.exists(self._get_key(token_str)) > 0
    except Exception as e:
      _log.error("Token 校验异常: {}", e)
      return False