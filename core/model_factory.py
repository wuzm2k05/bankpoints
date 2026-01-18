from langchain_openai import ChatOpenAI
from config.resource import get_resource
import config.config as config


def _get_hunyuan_model():
  return ChatOpenAI(
      model="hunyuan-2.0-instruct-20251111", # 这个型号最适合你的 Agent 节点
      api_key=config.get_hunyuan_api_key(),
      base_url="https://api.hunyuan.cloud.tencent.com/v1",
      temperature=0
    )

def _get_qwen_model():
  return ChatOpenAI(
      model="qwen-plus",
      api_key=config.get_qwen_api_key(), # 在百炼控制台获取
      base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
      temperature=0
    )

def get_model():
  resource = get_resource()
  
  mode_name = resource.get("active_model","hunyuan")

  if mode_name == "hunyuan":
    return _get_hunyuan_model()
  elif mode_name == "qwen":
    return _get_qwen_model()
  else:
    raise ValueError(f"Unknown model name: {mode_name}")
