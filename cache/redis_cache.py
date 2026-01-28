import config.config as config


def _refresh_ttl(self, thread_id: str, seconds: int):
  # 假设你使用的是 redis-py 客户端
  # LangGraph RedisSaver 存储的 key 通常带有前缀，例如 "checkpoint:<thread_id>"
  key = f"checkpoint:{thread_id}"
  self.redis_client.expire(key, seconds)
  _log.debug(f"已为用户 {thread_id} 续期 {seconds} 秒")