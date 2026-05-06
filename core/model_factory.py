# 2 个空格对齐
import os
from langchain_openai import ChatOpenAI
from config.resource import get_resource
import config.config as config
from loguru import logger as _log

# 使用全局变量实现简单的进程内单例
_model_instances = {}

def get_model(model_name = None):
  """
  获取大模型实例。
  采用进程内单例模式，确保每个子进程只维护一个连接池。
  """
  resource = get_resource()
  
  if model_name is None:
    model_name = resource.get("active_model", "hunyuan")
  
  if model_name in _model_instances:
    return _model_instances[model_name]
    
  if model_name not in resource["models"]:
    _log.error("配置中未找到模型 {} 的定义", model_name)
    raise ValueError(f"Model {model_name} not configured")

  model_param = resource["models"][model_name]
  
  # 获取 API Key
  raw_key = model_param["api_key"]
  real_api_key = os.getenv(raw_key, raw_key)
  
  _log.info("正在初始化 LLM 实例: {} (BaseURL: {})", 
            model_param["model_name"], 
            model_param["base_url"])

  model_instance = ChatOpenAI(
    model=model_param["model_name"],
    api_key=real_api_key,
    base_url=model_param["base_url"],
    temperature=model_param.get("temperature", 0),
    # 增加超时配置，防止异步链路死锁
    timeout=60,
    # 开启流式支持
    streaming=True 
  )
  
  _model_instances[model_name] = model_instance
  
  return model_instance