import yaml
import aiofiles
import util.util as util

from . import config
import log.logger as logger
"""
resource module used to read txt content for user. especailly for wechat mini app
"""

_log = logger.get_logger()
_resource = {}

def _load_yaml_file():
  global _resource
  with open(config.get_resource_file(),'r',encoding='utf-8') as file:
    _resource  = yaml.safe_load(file)

try:
  _load_yaml_file()
except Exception as e:
  _log.critical("load resource yaml file error: %s",e)
  import os
  os._exit(1)

def get_resource():
  return _resource

  
