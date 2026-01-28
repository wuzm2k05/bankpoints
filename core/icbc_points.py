def icbc_points_to_cash(points: int) -> float:
  """
  将工行积分转换为现金价值
  假设兑换比例为 500 积分 = 1 元人民币
  """
  return points / 500.0

def cash_to_icbc_points(cash: float) -> int:
  """
  将现金价值转换为工行积分
  假设兑换比例为 500 积分 = 1 元人民币
  """
  return int(cash * 500)