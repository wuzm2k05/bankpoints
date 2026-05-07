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
    """定义完整测试用例库，包含基础、流程及模糊语义测试"""
    return [
      {
        "id": "TC-01",
        "name": "基础比价-工行划算",
        "dialogs": ["我有 20000 积分，想换霸王茶姬 20 元券，划算吗？"],
        "goal": "应识别 1.71万豆折合17.1元，对比京东20元，推荐兑换。"
      },
      {
        "id": "TC-02",
        "name": "基础比价-京东划算",
        "dialogs": ["我有 150000 积分，想换个小米暖风机，帮我算算。"],
        "goal": "应算出11万豆折合110元，高于京东89元。结论必须推荐「换立减金+京东买」。"
      },
      {
        "id": "TC-03",
        "name": "流程控制-积分索取",
        "dialogs": ["我想买个海尔电风扇，工行换划算吗？"], 
        "goal": "核心法则：未知积分前必须礼貌询问积分数。"
      },
      {
        "id": "TC-04",
        "name": "动态修正-积分变更",
        "dialogs": [
          "我有 50000 积分，想换海尔电风扇。",
          "记错了，我其实有 150000 积分，重新帮我算一下。"
        ],
        "goal": "第二轮必须按 15 万积分重新执行计算。"
      },
      {
        "id": "TC-05",
        "name": "开放式建议-资产配置",
        "dialogs": ["我有 50 万积分，想换个华为 WATCH GT5，或者有更好的建议吗？"],
        "goal": "建议兑换高价值硬通货（如特来电或立减金）。"
      },
      {
        "id": "TC-06",
        "name": "陷阱规避-小额刺客",
        "dialogs": ["我有 30000 积分，想换箱雪碧，可以吗？"],
        "goal": "拦截兑换，推荐立减金。"
      },
      # --- 新增模糊语义测试用例 ---
      {
        "id": "TC-07",
        "name": "模糊语义-父亲生日礼物",
        "dialogs": ["我手里有 20 万积分，想给父亲买个生日礼物，工行商城有什么推荐吗？"],
        "goal": "测试主动检索能力。Agent 应调用搜索工具查询如‘中老年’、‘剃须刀’或‘按摩仪’，给出具体比价方案而非泛泛而谈。"
      },
      {
        "id": "TC-08",
        "name": "模糊语义-大额积分消耗",
        "dialogs": ["我积分快到期了，还有 80 万分，怎么换最不亏？"],
        "goal": "测试策略性。应通过搜索筛选高价值大额商品（如手机、加油卡），并计算利用率，给出‘不亏’的组合建议。"
      }
    ]

  def _judge_with_deepseek(self, case, chat_log):
    """调用 DeepSeek-V3 对对话结果及工具轨迹进行审计"""
    prompt = f"""
    你是一名专业的 AI 审计员。请根据以下「精算准则」评估【管家表现】。
    你需要对比【管家回复】和【工具调用轨迹】，核实其是否有脑补行为。

    【精算准则】：
    1. 汇率强制性：必须使用 1000:1。出现其他比例直接判为 0 分。
    2. 路径最优性：若工行换实物不划算，必须提到「兑换立减金/E卡 + 京东下单」。
    3. 数据严谨性（核心）：严禁脑补。回复中的所有价格/积分必须在 `tool_calls` 的 output 中有据可查。
    4. 模糊处理逻辑：面对模糊需求（如“送礼”），必须先调用 `vector_search_icbc_mall` 检索可能商品，严禁凭空编造商城不存在的礼品。

    【测试用例目标】：{case['goal']}
    【执行全记录（含工具轨迹）】：
    {json.dumps(chat_log, ensure_ascii=False, indent=2)}

    请按以下 JSON 格式返回评分：
    {{
      "score": 0-100,
      "passed": true/false,
      "audit_reason": "详细指出：1.是否漏调工具 2.数字是否对齐 3.模糊需求下是否进行了有效搜索"
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
      thread_id = f"test_case_{case['id']}_{int(time.time())}" 
      chat_log = []

      for user_input in case["dialogs"]:
        _log.info(f"[{case['id']}] 用户输入: {user_input}")
        agent_output, tool_trace = self.agent.chat_with_trace(user_input, thread_id=thread_id)
        
        _log.info(f"[{case['id']}] 管家回复: {agent_output}")
        chat_log.append({
          "user": user_input, 
          "assistant": agent_output,
          "tool_calls": tool_trace 
        })
      
      print("   ⚖️ 正在进行审计...")
      evaluation = self._judge_with_deepseek(case, chat_log)
      evaluation['name'] = case['name']
      final_results.append(evaluation)
      
    self._print_report(final_results)

  def _print_report(self, results):
    print("\n" + "="*70)
    print("           工银i豆精算管家 - 深度审计测试报告")
    print("="*70)
    passed_num = sum(1 for x in results if x['passed'])
    
    for r in results:
      status = "✅ [PASS]" if r['passed'] else "❌ [FAIL]"
      print(f"{status} {r['name']}")
      print(f"      得分: {r['score']} | 审计意见: {r['audit_reason']}")
      print("-" * 70)
    
    print(f"总结：运行 {len(results)} 项，通过 {passed_num} 项。")

if __name__ == "__main__":
  my_agent = RedemptionAgent()
  tester = AgentTester(my_agent)
  tester.run()