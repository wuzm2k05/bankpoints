from sqldb.sqlite_respository import SQLiteGoodsRepository
from config import config

async def query_data_from_sqlite():
  # 1. 初始化数据库适配器
  db_repo = SQLiteGoodsRepository()

  print(f"正在从数据库 中查询商品数据...")

  # 2. 查询前 10 条商品数据（可以根据需要调整限制数量）
  results = await db_repo.query(limit=10)

  if results:
    print(f"🎉 成功查询到 {len(results)} 条商品数据：")
    for index, goods in enumerate(results, start=1):
      print(f"\n--- 商品 #{index} ---")
      # 注意：由于主键已改为 product_id，这里使用 goods['product_id']
      print(f"商品ID: {goods.get('product_id')}")
      print(f"分类: {goods.get('category')}")
      print(f"描述: {goods.get('description')}")
      print(f"链接: {goods.get('link')}")
      if goods.get("extra"):
        print(f"扩展属性 (extra): {goods.get('extra')}")
  else:
    print("ℹ️ 数据库中暂无商品数据。")

async def add_data_to_sqlite():
  # 1. 从配置或直接指定本地数据库文件名实例化适配器
  db_repo = SQLiteGoodsRepository()
  
  # 2. 准备一条要插入的商品数据
  product_data = {
    "description": "最低价的爱奇艺/腾讯/优酷/剪影/喜马拉雅等电子券",
    "link": "https://wx.mail.qq.com/xmspamcheck/xmsafejump?func=1&check_src=2&key=N7dsdjUvodm2bGAtu%2FV9o4q2ZlUN6FKHfqag%2FAptTLNwYWCcTemPf%2F6%2Fx79Z%2Bd3BGuSLgAyytVFbFXvsc0Me3ZXv5zyo1wW4neD2cwd5VkvrAydDhiR9eZ2AWl12mmkL77EUCuQ2lzwwDi8NlD6IHjY2uxc5cBGgmrsPNnPxhaSSAinwwWKLVwyjbCuQ3Rir1HCYdFoJwqpo3IuzwrpEy10%3D&spam_err_code=0",
    "category": "电子券",
    "extra": {
      "points_required": 5000,
      "stock": 100
    }
  }

  print(f"正在向数据库 中添加商品...")
  
  # 3. 调用适配器的 add 接口写入数据（id 会由系统自动生成）
  generated_id = await db_repo.add(data=product_data)
  
  if generated_id:
    print(f"🎉 商品数据插入成功！生成的 product_id 为: {generated_id}")
    
    # 4. 验证是否能成功查询出来
    print("\n正在验证查询该商品...")
    results = await db_repo.query(product_id=generated_id)
    if results:
      goods = results[0]
      print("--- 检索到的商品详情 ---")
      # 使用已对齐的 product_id 字段
      print(f"商品ID: {goods['product_id']}")
      print(f"分类: {goods['category']}")
      print(f"描述: {goods['description']}")
      print(f"链接: {goods['link']}")
      print(f"扩展属性 (extra): {goods['extra']}")
    else:
      print("❌ 未查询到该商品！")
  else:
    print("❌ 商品数据插入失败。")