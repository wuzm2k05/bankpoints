# 2 个空格对齐
import sys
import os
from loguru import logger
import config.config as config

# 建立日志级别映射
_level_map = {
  'debug': 'DEBUG',
  'info': 'INFO',
  'warning': 'WARNING',
  'error': 'ERROR',
  'critical': 'CRITICAL'
}

def setup_logger():
  """
  在子进程启动时调用，初始化全局 logger 配置
  """
  # 1. 清除 loguru 默认的 stderr 配置
  logger.remove()

  # 2. 获取配置
  log_level = _level_map.get(config.get_log_level().lower().strip(), "INFO")
  log_file = config.get_log_file_name()
  max_size = config.get_log_backup_file_size()
  backup_num = config.get_log_backup_file_num()
  destination = config.get_log_destination().strip().split(',')

  # 3. 基础格式：包含进程 ID (PID)，这在多进程 WSS 中至关重要
  log_format = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>PID:{process}</cyan> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
  )

  # 4. 根据配置添加输出目标
  if "console" in destination:
    logger.add(sys.stdout, level=log_level, format=log_format)

  # 定义两个过滤器
  def is_missing_product(record):
    return "missing_product" in record["extra"]

  def is_normal_log(record):
    return "missing_product" not in record["extra"]

  # 1. 配置正常业务日志 (app.log)
  if "file" in destination:
    logger.add(
      log_file,
      level=log_level,
      format=log_format,
      filter=is_normal_log,  # <--- 关键：排除掉缺失商品的记录
      rotation=f"{max_size} B",
      enqueue=True
    )

  # 2. 配置缺失商品清单
  logger.add(
    "data/wechat_missing_products.jsonl",
    level="INFO",
    format="{message}",      # 只要纯 JSON 内容
    filter=is_missing_product, # <--- 关键：只记录缺失商品的记录
    enqueue=True,
    delay=True
  )

def get_logger():
  """
  为了兼容你现有的代码逻辑，返回全局单例 logger
  """
  return logger