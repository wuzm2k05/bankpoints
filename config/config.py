import os
import configparser

"""
_default_thread_pool_num = 10
_default_thread_pool_queue_size = 10
"""

def parse_config():
  file_path = 'data/config.ini'
  
  config = configparser.ConfigParser()
  config.read(file_path,encoding='utf-8')
  return config

# if we need to reload the config file, need to use another configparser to read the file again
config = parse_config()
################################################################################################
### deepseek api config
def get_deepseek_api_key():
  return config.get('deepseek', 'api_key', fallback=os.environ.get('DEEPSEEK_API_KEY',"sk-xxxxxxxxxxxx"))

def get_deepseek_base_url():
  return config.get('deepseek', 'base_url', fallback=os.environ.get('DEEPSEEK_BASE_URL',"https://api.deepseek.com"))

def get_deepseek_model():
  return config.get('deepseek', 'model', fallback=os.environ.get('DEEPSEEK_MODEL',"deepseek-chat")) 

################################################################################################
### hunyuan api config
def get_hunyuan_api_key():
  return config.get('hunyuan', 'api_key', fallback=os.environ.get('HUNYUAN_API_KEY',"sk-xxxxxxxxxxxx"))

################################################################################################
### qwen api config
def get_qwen_api_key():
  return config.get('qwen', 'api_key', fallback=os.environ.get('QWEN_API_KEY',"sk-xxxxxxxxxxxx"))

################################################################################################
### logging system
def get_log_file_name():
  return config.get('logging', 'file_name', fallback=os.environ.get('LOGGING_FILE_NAME',"icbcmall.log"))

def get_log_backup_file_num():
  return config.getint('logging', 'backup_file_num',fallback=int(os.environ.get('LOGGING_BACKUP_FILE_NUM',3)))

def get_log_backup_file_size():
  return config.getint('logging', 'backup_file_size',fallback=int(os.environ.get('LOGGING_BACKUP_FILE_SIZE',10000000)))

# logging destination: console,file console file
def get_log_destination():
  return config.get('logging', 'destination', fallback=os.environ.get('LOGGING_DESTINATION',"console"))

# logging level: debug info warning error critical
def get_log_level():
  return config.get('logging', 'level', fallback=os.environ.get('LOGGING_LEVEL',"info"))

################################################################################################
### redis configurations
def get_redis_host():
  return config.get('redis', 'host', fallback=os.environ.get('REDIS_HOST',"127.0.0.1"))

def get_redis_port():
  return config.getint('redis', 'port', fallback=int(os.environ.get('REDIS_PORT',6379)))

def get_redis_msg_ttl_in_seconds():
  return config.getint('redis', 'msg_ttl_in_seconds', fallback=int(os.environ.get('REDIS_MSG_TTL_IN_SECONDS',7200)))

################################################################################################
### jd configurations
def get_jd_app_key():
  return config.get('jd', 'app_key', fallback=os.environ.get('JD_APP_KEY',""))

def get_jd_app_secret():
  return config.get('jd', 'app_secret', fallback=os.environ.get('JD_APP_SECRET',""))

def get_jd_site_id():
  return config.get('jd', 'site_id', fallback=os.environ.get('JD_SITE_ID',""))

def get_jd_position_id():
  return config.get('jd', 'position_id', fallback=os.environ.get('JD_POSITION_ID',""))

################################################################################################
### for files location
def get_resource_file():
  return config.get('files', 'resource', fallback=os.environ.get('FILES_RESOURCE',"data/resource.yaml"))

### output all configrations
def output_configs(log):
  log.info("=================configs=====================")
  for section in config.sections():
    for key, value in config.items(section):
        log.info(f"{section}.{key} = {value}")
  log.info("\n")
        