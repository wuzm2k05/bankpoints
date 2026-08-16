import hashlib
from typing import List, Dict, Any
from core.icbc_db import ICBCVectorDB
import log.logger as logger

_log = logger.get_logger()

# 纯 JSON 结构的鸡蛋特色知识库数据
EGG_KNOWLEDGE_DATA: List[Dict[str, Any]] = [
  {
    "id": "egg_001",
    "content": "【基本介绍与产品卖点】宜凤园·富硒散养土鸡蛋，产自北纬30°生态山林，180-280天黄金慢养。具备20倍高天然硒+天然叶黄素加持，高蛋白高营养，天然小分子易吸收。0抗生素、0激素、低胆固醇，蛋清浓稠拉丝，蛋黄沙糯不噎人无腥味，采用防震礼盒包装，是适合全家日常囤货的高品质好鸡蛋。",
    "category": "general",
    "topic": "产品概述与核心卖点"
  },
  {
    "id": "egg_002",
    "content": "【产地与生态环境】产地来自湖北宜昌北纬30°生态山林，公认的黄金农业带。这里气候宜人、光照充足、土壤天然富硒、空气清新、水源干净，远离污染。土鸡100%山林散养、低密度放养，拥有超大活动空间，每天在林间自由跑动，吃虫子、青草和天然谷物，源头干净安心。",
    "category": "origin",
    "topic": "产地与山林散养环境"
  },
  {
    "id": "egg_003",
    "content": "【发酵床生态养殖】鸡舍采用特色干撒式发酵床技术，用稻壳+锯末铺足15公分厚，撒上菌种自然发酵自动循环。能自动分解母鸡排泄物，鸡舍完全无氨气、无臭味，不刺激呼吸道，母鸡少生病更健康，养殖过程全程生态干净。",
    "category": "origin",
    "topic": "干撒式发酵床养殖技术"
  },
  {
    "id": "egg_004",
    "content": "【黄金产蛋期与自然饲养】坚持180-280天自然黄金产蛋周期，遵循母鸡生长规律，绝不催产、不打激素、不喂劣质合成饲料。母鸡日常食用玉米、大豆、谷物、南瓜、红薯、豆粕、青绿野菜及山间自然觅食，营养沉淀浓缩，蛋香浓郁，还原正宗土鸡蛋风味。",
    "category": "origin",
    "topic": "慢养周期与天然饲料"
  },
  {
    "id": "egg_005",
    "content": "【权威富硒认证】具备官方权威机构富硒认证，检测数据真实。天然富硒含量高达普通鸡蛋的20倍，且为天然温和好吸收的有机硒，无身体负担。硒元素有助于提升免疫力、保护心血管、调节身体状态，适合体弱老人、长身体的孩子、熬夜上班族及孕妇。",
    "category": "nutrition",
    "topic": "权威富硒认证与功效"
  },
  {
    "id": "egg_006",
    "content": "【天然叶黄素与护眼】鸡蛋天然自带叶黄素，犹如眼睛的天然防护伞，能辅助过滤蓝光、缓解用眼疲劳、保护视力健康，非常适合学生、上班族、发育期儿童及老人食用。",
    "category": "nutrition",
    "topic": "天然叶黄素与护眼功效"
  },
  {
    "id": "egg_007",
    "content": "【品质安全与适用人群】严格坚持0抗生素、0激素、无有害残留，生态养殖全程可控并经过层层严格精选。低胆固醇无负担，优质高蛋白细腻易消化，老人、宝宝、孕妇、减脂人群及三高人群均可放心食用。",
    "category": "safety",
    "topic": "安全认证与适用人群"
  },
  {
    "id": "egg_008",
    "content": "【新鲜度与时效保障】坚持现捡现发、当日发货，不囤货不存放。当天山林捡蛋、人工精选、打包发货，从山林鸡窝直达餐桌，最大程度锁住新鲜度、香气与营养。",
    "category": "logistics",
    "topic": "新鲜度与现捡现发"
  },
  {
    "id": "egg_009",
    "content": "【包装与防破损保障】采用精美礼盒包装，内置加厚珍珠棉蛋托 + 独立卡槽定位 + 双层硬纸箱加固，具备防震、防摔、防碰撞、防挤压保护，遇暴力快递也能最大程度保证完好无损。",
    "category": "logistics",
    "topic": "珍珠棉包装与防破损"
  },
  {
    "id": "egg_010",
    "content": "【口感与品质特征】蛋清清亮剔透、浓稠绵密，拎起来能拉丝，滑嫩Q弹不稀散；蛋黄天然橙黄圆鼓、挺立不散，入口沙糯绵密、香浓细腻化渣、完全不噎人、无腥味。",
    "category": "taste",
    "topic": "蛋清蛋黄口感特征"
  },
  {
    "id": "egg_011",
    "content": "【烹饪与食用建议】推荐吃法：1. 白水煮蛋：口感绵密弹牙，蛋香浓郁不流失营养；2. 蒸蛋羹：滑嫩不腥，适合宝宝辅食与老人；3. 煎蛋/炒蛋（番茄/韭菜炒蛋）：金黄软香，鲜味拉满；4. 煮面/蛋花汤：清爽鲜美。",
    "category": "taste",
    "topic": "烹饪吃法与辅食建议"
  }
]

def build_egg_db():
  db = ICBCVectorDB()
  for item in EGG_KNOWLEDGE_DATA:
    if "id" not in item:
      hash_str = hashlib.md5(item["content"].encode('utf-8')).hexdigest()[:10]
      item["id"] = f"egg_{hash_str}"

  db.build_egg_knowledge(EGG_KNOWLEDGE_DATA)
  _log.success("宜凤园鸡蛋知识库（JSON 数据）向量化构建成功！")
  
def search_egg_db(query:str):
  db = ICBCVectorDB()
  _log.info(db.search_egg_info(query))