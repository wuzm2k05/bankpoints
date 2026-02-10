# 2 个空格对齐
import os
import sys
from typing import List, Dict, Any

# 1. 核心修改：将父目录（项目根目录）加入系统路径
# os.path.dirname(__file__) 获取 tools 目录路径
# 再取一次 dirname 得到项目根目录
root_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_path not in sys.path:
  sys.path.append(root_path)

# 假设你的 ICBCVectorDB 类定义在 core/icbc_db.py 中
from core.icbc_db import ICBCVectorDB
import config.config as config
import log.logger as logger

_log = logger.get_logger()

def parse_voucher_faq_file(file_path: str) -> str:
  if not os.path.exists(file_path):
    _log.error(f"知识库文件 {file_path} 不存在")
    return ""

  # 尝试多种编码读取
  encodings = ['utf-8', 'gbk', 'utf-16']
  
  for enc in encodings:
    try:
      with open(file_path, 'r', encoding=enc) as f:
        content = f.read()
        _log.info(f"成功使用 {enc} 编码读取文件")
        return content.strip()
    except (UnicodeDecodeError, Exception):
      continue
  
  _log.error(f"无法使用支持的编码({encodings})读取文件，请检查文件编码。或者文件不存在")
  return ""

# --- 2. 执行主程序 ---

if __name__ == "__main__":
  # 支持从命令行传入文件名，默认读取 materials/voucher_faq.txt
  # 建议将你那一段 Q&A 内容存入该文件
  FILE_PATH = sys.argv[1] if len(sys.argv) > 1 else "materials/voucher_faq.txt"

  _log.info(f"开始加载立减金业务知识: {FILE_PATH}")

  # 1. 解析/读取文本内容
  faq_content = parse_voucher_faq_file(FILE_PATH)

  if faq_content:
    # 2. 初始化数据库连接
    # 注意：确保你的 ICBCVectorDB 类中已经按照之前的建议增加了 add_voucher_knowledge 方法
    try:
      db = ICBCVectorDB()
      
      # 3. 写入 Voucher 专属 Collection
      # 该方法会自动按 Q: A: 结构进行切片并生成向量
      db.add_voucher_knowledge(faq_content)
      
      _log.info("\n" + "="*30)
      _log.info("--- 立减金知识库导入完成 ---")
      _log.info(f"数据来源: {FILE_PATH}")
      _log.info("存储位置: icbc_vector_db (Collection: icbc_standing_vouchers)")
      _log.info("="*30)
      
    except Exception as e:
      _log.error(f"数据库写入失败: {str(e)}")
  else:
    _log.warning("未检测到有效知识内容，取消导入。")