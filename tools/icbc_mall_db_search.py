# 2 个空格对齐
import sys,os

# 1. 核心修改：将父目录（项目根目录）加入系统路径
# os.path.dirname(__file__) 获取 tools 目录路径
# 再取一次 dirname 得到项目根目录
root_path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if root_path not in sys.path:
  sys.path.append(root_path)

from core.icbc_db import ICBCVectorDB
import config.config as config
import log.logger as logger

_log = logger.get_logger()

def interactive_search():
  # 1. 初始化数据库
  # 确保你的 ICBCVectorDB 类在 core/icbc_db.py 中已经实现了 search 方法
  try:
    db = ICBCVectorDB()
    _log.info("=== 工行积分商城 · 语义搜索系统 ===")
    _log.info("输入 'exit' 或 'quit' 退出程序。")
  except Exception as e:
    _log.error(f"初始化失败: {e}")
    return

  while True:
    # 2. 获取用户输入
    query = input("\n请输入您的需求（例如：送长辈的健康礼品 / 10万分左右的数码产品）：\n> ").strip()
    
    if not query:
      continue
    if query.lower() in ['exit', 'quit']:
      _log.info("感谢使用，再见！")
      break

    # 3. 执行搜索
    # 假设你的 search 方法返回类似 [{'name': '...', 'points': 123, 'distance': 0.4}] 的列表
    _log.info(f"正在为您搜索关于 '{query}' 的商品...")
    try:
      # 我们设置搜索前 3 名最相关的商品
      results = db.search(query, limit=3)
      
      if not results:
        _log.warning("抱歉，没有找到匹配的商品。")
        continue

      # 4. 格式化输出结果
      _log.info("-" * 40)
      for i, item in enumerate(results, 1):
        name = item.get('name', '未知商品')
        points = item.get('points', 0)
        # distance 越小表示语义越接近
        score = item.get('distance', 0)
        
        # 将积分格式化，10000 -> 1万
        if points >= 10000:
          pts_display = f"{points/10000:.2f}万豆"
        else:
          pts_display = f"{points}豆"
          
        _log.info(f"{i}. 【{name}】")
        _log.info(f"   所需积分: {pts_display}")
        _log.info(f"   匹配相关度: {max(0, (1-score)*100):.1f}%")
      _log.info("-" * 40)

    except Exception as e:
      _log.error(f"搜索过程中发生错误: {e}")

if __name__ == "__main__":
  interactive_search()