from langchain_openai import ChatOpenAI
from config.resource import get_resource
import config.config as config

def get_model():
  resource = get_resource()
  
  mode_name = resource.get("active_model","hunyuan")
  
  model_param = resource["models"][mode_name]
  
  return ChatOpenAI(
      model=model_param["model_name"],
      api_key=model_param["api_key"],
      base_url=model_param["base_url"],
      temperature=model_param.get("temperature",0)
    )
