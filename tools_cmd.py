import asyncio,sys
from tools.icbc_voucher_to_db import VoucherKnowledgeBuilder
from dotenv import load_dotenv
load_dotenv()

from loguru import logger as _log

async def main():
  import log.logger as logger_module
  logger_module.setup_logger()
  
  # 2. 模拟命令行参数解析
  if len(sys.argv) < 2:
    print("Usage: python tools_cmd.py <cmd> [args]...")
    return

  cmd = sys.argv[1]
  if cmd == "bvd":
    if len(sys.argv) != 3:
      print("Usage for build_voucher_db: python tools_cmd.py bvd <voucher_qa_file_path>")
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