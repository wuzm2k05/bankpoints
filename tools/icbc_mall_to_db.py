# 2 个空格对齐
import re
import os
import sys
from typing import List, Dict, Any

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

# --- 1. 价格解析增强工具 ---

def convert_zh_price(price_str: str) -> int:
  """
  将包含中文单位的价格字符串转换为整数积分。
  支持：豆、万豆、十万豆、百万豆
  """
  # 去除空格
  price_str = price_str.strip().replace(' ', '')
  
  # 提取数字部分（包括小数点）
  num_match = re.search(r"[\d\.]+", price_str)
  if not num_match:
    return 0
  
  num = float(num_match.group())
  
  # 单位换算逻辑
  if '百万豆' in price_str:
    return int(num * 1000000)
  elif '十万豆' in price_str:
    return int(num * 100000)
  elif '万豆' in price_str:
    return int(num * 10000)
  elif '豆' in price_str:
    return int(num)
  
  return int(num)

# --- 3. 解析逻辑 ---

def parse_icbc_file(file_path: str) -> List[Dict[str, Any]]:
  if not os.path.exists(file_path):
    _log.error(f"文件 {file_path} 不存在")
    return []

  with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

  # 更新正则：匹配包含数字和中文单位的价格行
  # 匹配模式：商品名行 \n 价格行(包含数字和豆字)
  pattern = r"([^\n]+)\n([\d\.]+[万|十万|百万]?豆)"
  matches = re.findall(pattern, content)
  
  products = []
  for idx, (name, price_raw) in enumerate(matches):
    name = name.strip()
    if not name: continue
    
    points = convert_zh_price(price_raw)
    
    if points > 0:
      products.append({
        "id": f"item_{idx:04d}",
        "name": name,
        "points": points
      })
      
  return products

# --- 4. 执行主程序 ---

if __name__ == "__main__":
  # 替换为你的 API Key
  FILE_PATH = sys.argv[1] if len(sys.argv) > 1 else "materials\icbcdou.txt"

  # 1. 解析
  product_list = parse_icbc_file(FILE_PATH)
  print(f"解析完成，成功提取 {len(product_list)} 个商品。")

  # 打印前 3 个示例确认转换正确
  for p in product_list[:3]:
    print(f"已解析: {p['name']} -> {p['points']} 积分")

  if product_list:
    # 2. 写入数据库
    db = ICBCVectorDB()
    db.add_products(product_list)
    
    print("\n--- 写入数据完成 ---")
  else:
    print("未提取到有效商品数据，未进行数据库写入。")