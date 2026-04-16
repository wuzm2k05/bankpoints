import asyncio,sys
from tools.icbc_voucher_to_db import VoucherKnowledgeBuilder

from loguru import logger as _log

async def main():
  import log.logger as logger_module
  logger_module.setup_logger()
  
  # 2. 模拟命令行参数解析
  # 预期格式: python main.py <cmd> <text_file_path> <output_mp3_name>
  if len(sys.argv) < 2:
    print("Usage: python script.py <cmd> [args]... <text_content_or_file> <output_mp3_name>")
    return

  cmd = sys.argv[1]
  if cmd == "bvd":
    if len(sys.argv) != 3:
      print("Usage for build_voucher_db: python script.py bvd <voucher_qa_file_path>")
      return 
    builder = VoucherKnowledgeBuilder()
    builder.run_full_sync(sys.argv[2])
    return
  
if __name__ == "__main__":
  try:
    #asyncio.run(query_by_id())
    asyncio.run(main())
    #asyncio.run(query())
  except KeyboardInterrupt:
    pass