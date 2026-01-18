import uuid,importlib,sys,re,time
import json
import aiofiles

import log.logger as logger

_log = logger.get_logger()

_seq_num = 0

def genterate_seq():
  global _seq_num
  
  seq = "wxai" + str(time.time()) + str(_seq_num)
  _seq_num += 1
  
  return seq
  
def generate_32_uuid_string():
  return str(uuid.uuid4()).replace('-','')

def load_class(full_class_name):
  try:
    # get modlue and class name
    module_name, class_name = full_class_name.rsplit('.', 1)
    module = importlib.import_module(module_name)
    import_class = getattr(module,class_name)
    return import_class
      
  except Exception as e:
    _log.critical(f"Failed to create class { full_class_name }instance: {e}")
    sys.exit()

def read_text_file(file_name):
  with open(file_name, 'r', encoding='utf-8') as file:
    return file.read()

def read_binary_file(file_path):
  with open(file_path,'rb') as f:
    return f.read()

async def a_write_binary_data(file_name, binary_data):
    # Open the file in 'wb' (write binary) mode
    async with aiofiles.open(file_name, 'wb') as file:
        # Write binary data to the file
        await file.write(binary_data)


