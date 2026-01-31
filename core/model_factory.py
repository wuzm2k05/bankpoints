# 2 个空格对齐
import os
from langchain_openai import ChatOpenAI
from config.resource import get_resource
import config.config as config

def get_model():
  resource = get_resource()
  mode_name = resource.get("active_model", "hunyuan")
  model_param = resource["models"][mode_name]
  
  # 获取配置中的 raw_key (可能是真实的 key，也可能是环境变量名)
  raw_key = model_param["api_key"]
  
  # 逻辑：尝试从环境变量读取，如果环境变量不存在，则退而求其次使用 raw_key 本身
  # 这样可以兼容“直接写 Key”和“写环境变量名”两种方式
  real_api_key = os.getenv(raw_key, raw_key)
  
  return ChatOpenAI(
    model=model_param["model_name"],
    api_key=real_api_key,
    base_url=model_param["base_url"],
    temperature=model_param.get("temperature", 0)
  )