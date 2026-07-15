from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional

from util.singleton import SingletonABCMeta

class BaseGoodsRepository(metaclass=SingletonABCMeta):
  """商品数据访问适配层抽象基类"""

  @abstractmethod
  async def query(self, product_id: Optional[str] = None, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    统一查询接口
    :param product_id: 如果传入，则按 ID 精确查询（返回含有一个元素的列表或空列表）
    :param limit: 如果传入，则限制返回的商品数量
    """
    pass

  @abstractmethod
  async def add(self, data: Dict[str, Any]) -> str:
    """
    新增商品，product_id 由系统自动生成
    :param data: 商品字段字典（不包含 product_id）
    :return: 自动生成的唯一 product_id
    """
    pass

  @abstractmethod
  async def update(self, product_id: str, data: Dict[str, Any]) -> bool:
    """
    严格更新商品信息（仅在商品存在时生效）
    :param product_id: 商品唯一标识
    :param data: 需要更新的字段字典
    :return: 是否更新成功（不存在则返回 False）
    """
    pass

  @abstractmethod
  async def delete(self, product_id: str) -> bool:
    """
    删除商品
    :param product_id: 商品唯一标识
    """
    pass