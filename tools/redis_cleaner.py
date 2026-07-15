# -*- coding: utf-8 -*-
# 2 个空格对齐
import asyncio
from redis.asyncio import Redis
from loguru import logger as _log

# 配置你的 Redis 连接信息，可根据实际项目修改
REDIS_HOST = "127.0.0.1"
REDIS_PORT = 6379
REDIS_DB = 0
REDIS_PASSWORD = None  # 若无密码设为 None

class RedisDataCleaner:
  def __init__(self, redis_client: Redis):
    self.redis_client = redis_client

  async def delete_single_user(self, user_id: str) -> bool:
    """清除指定用户的所有快照与中间写状态数据"""
    if not user_id or not user_id.strip():
      _log.warning("无效的 user_id")
      return False

    checkpoint_key = f"checkpoints:{user_id}"
    writes_pattern = f"writes:{user_id}:*"
    
    try:
      _log.info(f"🚀 开始清理用户数据 -> user_id: {user_id}")
      
      # 1. 扫描并找出所有匹配该用户的 writes 键
      write_keys = []
      async for key in self.redis_client.scan_iter(match=writes_pattern, count=100):
        write_keys.append(key)
        
      # 2. 使用 Pipeline 批量安全地执行删除
      async with self.redis_client.pipeline() as pipe:
        pipe.delete(checkpoint_key)
        if write_keys:
          _log.info(f"发现关联写缓存键 {len(write_keys)} 个，准备一并删除...")
          pipe.delete(*write_keys)
        results = await pipe.execute()
      
      # 第一个执行的是删除 checkpoints 键，通过结果判断是否真正移除了数据
      checkpoint_deleted = results[0] > 0
      _log.success(f"✨ 用户 {user_id} 数据清理完毕！(快照已被清空)")
      return checkpoint_deleted
      
    except Exception as e:
      _log.error(f"❌ 清理用户 {user_id} 数据失败: {e}")
      return False

  async def clear_all_users(self) -> int:
    """一键清除 Redis 中所有用户的快照(checkpoints:*)与中间写状态(writes:*)"""
    try:
      _log.info("⚠️ 正在扫描全库中的用户快照和中间缓存数据...")
      
      target_keys = []
      # 扫描快照键
      async for key in self.redis_client.scan_iter(match="checkpoints:*", count=100):
        target_keys.append(key)
      # 扫描写缓存键
      async for key in self.redis_client.scan_iter(match="writes:*", count=100):
        target_keys.append(key)

      if not target_keys:
        _log.info(" Redis 中没有发现任何用户的持久化数据，无需清理。")
        return 0

      _log.warning(f"🚨 警告：共扫描到 {len(target_keys)} 个用户相关数据键，准备执行批量删除！")
      
      # 分批删除防止一次性删除过多键导致 Redis 阻塞 (分批大小: 500)
      batch_size = 500
      deleted_count = 0
      
      for i in range(0, len(target_keys), batch_size):
        batch = target_keys[i:i + batch_size]
        async with self.redis_client.pipeline() as pipe:
          pipe.delete(*batch)
          results = await pipe.execute()
          deleted_count += sum(results)

      _log.success(f"🏁 轰炸式清理完成！成功删除 {deleted_count} 个数据键。")
      return deleted_count

    except Exception as e:
      _log.error(f"❌ 一键清空全库用户数据失败: {e}")
      return 0

async def main():
  # 初始化异步 Redis 客户端
  redis_client = Redis(
    host=REDIS_HOST, 
    port=REDIS_PORT, 
    db=REDIS_DB, 
    password=REDIS_PASSWORD
  )
  cleaner = RedisDataCleaner(redis_client)

  print("=========================================")
  print("      LangGraph Redis 数据管理工具")
  print("=========================================")
  print("1. 清除指定用户(UserCode/ThreadId)的所有数据")
  print("2. 一键清空全库所有用户的数据（危险操作）")
  print("3. 退出")
  print("=========================================")
  
  choice = input("请选择操作编号 (1/2/3): ").strip()

  if choice == "1":
    user_id = input("请输入需要清除的用户 userCode (thread_id): ").strip()
    if user_id:
      confirm = input(f"确认要删除用户 {user_id} 的全部历史记录和快照吗？(y/n): ").strip().lower()
      if confirm == 'y':
        await cleaner.delete_single_user(user_id)
      else:
        print("操作已取消。")
    else:
      print("输入的用户 ID 不能为空。")

  elif choice == "2":
    print("\n🚨🚨🚨 极其危险的操作 🚨🚨🚨")
    confirm = input("确定要清空全库【所有用户】的对话上下文和快照快照吗？此操作不可逆！(请输入 'YES' 确认): ").strip()
    if confirm == "YES":
      await cleaner.clear_all_users()
    else:
      print("操作已取消。")
      
  elif choice == "3":
    print("已退出。")
  else:
    print("无效的输入。")

  # 关闭 Redis 物理连接
  await redis_client.close()

if __name__ == "__main__":
  # 运行异步主函数
  asyncio.run(main())