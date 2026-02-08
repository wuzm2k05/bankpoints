# 2 个空格对齐
import json
import os
import time
from core.redemption_agent import RedemptionAgent
from openai import OpenAI
import config.resource as resource
import log.logger as logger

_log = logger.get_logger()

# 加载配置
res_data = resource.get_resource()
model_param = res_data["models"]["deepseek"]

# 获取 API Key
raw_key = model_param["api_key"]
real_api_key = os.getenv(raw_key, raw_key)
  
# 1. 初始化 DeepSeek 裁判
client = OpenAI(
  api_key=real_api_key, 
  base_url=model_param["base_url"]
)

class AgentTester:
  def __init__(self, agent_instance):
    self.agent = agent_instance
    self.judge_model = "deepseek-chat" # 使用 DeepSeek-V3

  def get_cases(self):
    """定义完整测试用例库"""
    return [
      {
        "id": "TC-01",
        "name": "基础比价-工行划算",
        "dialogs": ["我有 20000 积分，想换霸王茶姬 20 元券，划算吗？"],
        "goal": "应识别 1.71万豆折合15.5元，对比京东20元利用率>100%，推荐兑换。"
      },
      {
        "id": "TC-02",
        "name": "基础比价-京东划算",
        "dialogs": ["我有 150000 积分，想换个小米暖风机，帮我算算。"],
        "goal": "应算出11万豆折合100元，高于京东89元。结论必须推荐「换立减金+京东买」。"
      },
      {
        "id": "TC-03",
        "name": "流程控制-积分索取",
        "dialogs": ["我想买个海尔电风扇，工行换划算吗？"], 
        "goal": "核心法则约束：在不知道积分数前，必须礼貌拒绝推荐并询问积分。"
      },
      {
        "id": "TC-04",
        "name": "动态修正-积分变更",
        "dialogs": [
          "我有 50000 积分，想换海尔电风扇。",
          "记错了，我其实有 150000 积分，重新帮我算一下。"
        ],
        "goal": "测试状态一致性。第二轮必须丢弃5万假设，按15万积分重新执行比价计算。"
      },
      {
        "id": "TC-05",
        "name": "开放式建议-资产配置",
        "dialogs": ["我有 50 万积分，想换个华为 WATCH GT5，或者有更好的建议吗？"],
        "goal": "需对比 GT5(109.9万豆) 和 特来电(46.5万豆) 的利用率，建议兑换高价值硬通货。"
      },
      {
        "id": "TC-06",
        "name": "陷阱规避-小额刺客",
        "dialogs": ["我有 30000 积分，想换箱雪碧和手帕纸，可以吗？"],
        "goal": "雪碧折算18.45元高于京东15.9元。必须拦截此兑换，推荐立减金方案。"
      }
    ]

  def _judge_with_deepseek(self, case, chat_log):
    """调用 DeepSeek-V3 对对话结果及工具轨迹进行审计"""
    prompt = f"""
    你是一名专业的 AI 审计员。请根据以下「精算准则」评估【管家表现】。
    你需要对比【管家回复】和【工具调用轨迹】，核实其是否有脑补行为。

    【精算准则】：
    1. 汇率强制性：必须使用 1000:1。出现 500:1 或 1000:1 直接判为 0 分。
    2. 路径最优性：若兑换实物不划算，必须提到「兑换立减金/E卡 + 京东下单」。
    3. 数据严谨性（核心）：严禁脑补。回复中的价格/积分必须在 `tool_calls` 的 output 中有据可查。
    4. 流程合规性：未知积分前不得推荐商品；积分变更后必须重新计算。

    【测试用例目标】：{case['goal']}
    【执行全记录（含工具轨迹）】：
    {json.dumps(chat_log, ensure_ascii=False, indent=2)}

    请按以下 JSON 格式返回评分：
    {{
      "score": 0-100,
      "passed": true/false,
      "audit_reason": "请详细指出：1.是否漏调工具 2.数字是否对齐轨迹 3.汇率是否正确"
    }}
    """
    
    response = client.chat.completions.create(
      model=self.judge_model,
      messages=[{"role": "user", "content": prompt}],
      response_format={"type": "json_object"}
    )
    return json.loads(response.choices[0].message.content)

  def run(self):
    print("🔔 开始执行全链路自动化测试（含 Trace 审计模式）...\n")
    all_cases = self.get_cases()
    final_results = []

    for case in all_cases:
      print(f"👉 测试中: {case['name']}")
      # 每次测试使用全新的 thread_id 保证隔离
      thread_id = f"test_case_{case['id']}_{int(time.time())}" 
      chat_log = []

      for user_input in case["dialogs"]:
        _log.info(f"[{case['id']}] 用户输入: {user_input}")
        
        # 调用带轨迹捕获的对话接口
        agent_output, tool_trace = self.agent.chat_with_trace(user_input, thread_id=thread_id)
        
        _log.info(f"[{case['id']}] 管家回复: {agent_output}")
        chat_log.append({
          "user": user_input, 
          "assistant": agent_output,
          "tool_calls": tool_trace # 将后台工具执行详情塞入日志，供裁判查阅
        })
      
      # 裁判打分
      print("   ⚖️ 正在调用 DeepSeek-V3 审计工具轨迹与回复一致性...")
      evaluation = self._judge_with_deepseek(case, chat_log)
      evaluation['name'] = case['name']
      final_results.append(evaluation)
      
    self._print_report(final_results)

  def _print_report(self, results):
    print("\n" + "="*70)
    print("           工行积分精算管家 - 深度审计测试报告")
    print("="*70)
    passed_num = sum(1 for x in results if x['passed'])
    
    for r in results:
      status = "✅ [PASS]" if r['passed'] else "❌ [FAIL]"
      print(f"{status} {r['name']}")
      print(f"      得分: {r['score']}")
      print(f"      审计意见: {r['audit_reason']}")
      print("-" * 70)
    
    print(f"总结：共运行 {len(results)} 项，通过 {passed_num} 项。")
    if passed_num == len(results):
      print("🎉 恭喜！Agent 表现完美，计算严谨且无任何脑补行为。")

# --- 执行入口 ---
if __name__ == "__main__":
  # 实例化 RedemptionAgent
  # 注意：请确保你的 RedemptionAgent 类中已经按照前文建议添加了 chat_with_trace 方法
  my_agent = RedemptionAgent()
  
  # 运行测试
  tester = AgentTester(my_agent)
  tester.run()