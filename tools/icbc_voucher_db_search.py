# 2 个空格对齐
import sys,os

# 1. 核心修改：将父目录（项目根目录）加入系统路径
# os.path.dirname(__file__) 获取 tools 目录路径
# 再取一次 dirname 得到项目根目录
root_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_path not in sys.path:
  sys.path.append(root_path)


# 导入你的核心类和配置
from core.icbc_db import ICBCVectorDB
import config.config as config
import log.logger as logger

_log = logger.get_logger()

def interactive_voucher_test():
  """
  工行微信立减金(Voucher) 业务知识检索测试程序
  """
  # 1. 初始化数据库
  try:
    db = ICBCVectorDB()
    _log.info("=== 工行精算管家 · 立减金业务知识测试系统 ===")
    _log.info("提示：输入 'exit' 或 'quit' 退出。")
  except Exception as e:
    _log.error(f"初始化失败: {e}")
    return

  while True:
    # 2. 获取用户提问
    query = input("\n请输入您想咨询的立减金问题（例如：立减金没到账 / 如何提取待发金额）：\n> ").strip()
    
    if not query:
      continue
    if query.lower() in ['exit', 'quit']:
      _log.info("测试结束，感谢使用！")
      break

    # 3. 执行语义检索
    _log.info(f"正在检索关于 '{query}' 的业务规则...")
    try:
      # 调用专门的 Voucher 检索方法，取前 2 条最相关的知识片段
      results = db.search_voucher_info(query, limit=2)
      
      if not results:
        _log.warning("抱歉，知识库中没有找到相关的业务规则。")
        _log.info("建议检查：1. 是否已运行导入脚本导入了 Q&A；2. 检索阈值设置是否合理。")
        continue

      # 4. 格式化输出检索到的知识
      _log.info("=" * 50)
      _log.info(f"检索到以下 {len(results)} 条相关背景知识：")
      
      for i, content in enumerate(results, 1):
        _log.info("-" * 20)
        _log.info(f"知识片段 {i}:")
        # 打印检索到的 Q&A 原文
        _log.info(content)
      
      _log.info("=" * 50)
      _log.info("【精算提示】：AI Agent 将基于以上内容为客户组织最终回复。")

    except Exception as e:
      _log.error(f"检索过程中发生错误: {e}")

if __name__ == "__main__":
  interactive_voucher_test()