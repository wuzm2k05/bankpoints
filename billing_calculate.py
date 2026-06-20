import os
import json
import requests
import math
import smtplib
import glob
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

# 加载 .env 配置文件
load_dotenv()

# ==================== 配置区域 ====================
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY") 
SENDER_PASSWD = os.getenv("EMAIL_PASSWORD")     
RECEIVER_EMAILS = os.getenv("BILLING_RECEIVER_EMAILS")    

# 增强功能：从 env 获取 crontab 日志文件存放的绝对/相对路径目录，默认当前目录
CRON_LOG_DIR = os.getenv("CRON_LOG_DIR") or "."

SINGLE_RECHARGE = os.getenv("BILLING_RECHARGE_AMOUNT") or 200.00                        # 每次固定的充值单笔金额
BUDGET_MULTIPLIER = 1.5                         # 报销额度乘数 (150%)
DATA_DIR = "data"                           
DATA_FILE = os.path.join(DATA_DIR, "billing_record.json")

# 邮件基础服务配置
SMTP_SERVER = "smtp.qq.com"  
SMTP_PORT = 587             
SENDER_EMAIL = "79809620@qq.com"  
# ==================================================

def get_current_balance():
  """调用 API 获取当前总余额（仅筛选 CNY 币种）"""
  if not DEEPSEEK_API_KEY:
    print("❌ 未找到 DEEPSEEK_API_KEY 环境变量")
    return None
  url = "https://api.deepseek.com/user/balance"
  headers = {"Accept": "application/json", "Authorization": f"Bearer {DEEPSEEK_API_KEY}"}
  try:
    response = requests.get(url, headers=headers, timeout=10)
    if response.status_code == 200:
      res_json = response.json()
      # 遍历返回的币种列表，精确查找人类民币 (CNY) 账户
      for info in res_json.get("balance_infos", []):
        if info.get("currency") == "CNY":
          return float(info["total_balance"])
      print("❌ API 成功返回，但未在列表中找到 CNY 账户信息")
      return None
    else:
      print(f"❌ 请求失败，状态码: {response.status_code}")
      return None
  except Exception as e:
    print(f"❌ 请求 DeepSeek 接口异常: {e}")
    return None

def load_last_record():
  """读取历史记录，带损坏校验"""
  if not os.path.exists(DATA_FILE): 
    return None
  try:
    with open(DATA_FILE, 'r', encoding='utf-8') as f:
      data = json.load(f)
    return data["last_date"], float(data["balance"])
  except Exception as e:
    print(f"🚨 【致命错误】数据文件损坏: {e}")
    raise SystemExit("程序因本地数据文件损坏而退出。")

def save_current_record(date_str, balance):
  """
  保存最新记录（必须存真实余额，确保下月计算基准正确）
  增强：增加强行落盘 (fsync) 机制，确保数据确实写入了物理磁盘
  """
  if not os.path.exists(DATA_DIR): 
    os.makedirs(DATA_DIR)
      
  record_payload = {"last_date": date_str, "balance": f"{balance:.2f}"}
  
  # 采用标准文件持久化落盘流程
  with open(DATA_FILE, 'w', encoding='utf-8') as f:
    json.dump(record_payload, f, indent=4)
    f.flush()               # 1. 确保 Python 内部缓冲区刷新到操作系统缓存
    os.fsync(f.fileno())    # 2. 强制操作系统将缓存数据彻底同步刷新到磁盘介质
  print(f"💾 数据记录已安全落盘永久存储: {record_payload}")

def clean_previous_month_log(current_today):
  """
  根据从 env 中读取的日志目录，推算并清理上个月的 crontab 日志文件
  """
  first_day_of_current_month = current_today.replace(day=1)
  last_month_date = first_day_of_current_month - timedelta(days=3)
  last_month_str = last_month_date.strftime("%Y-%m")
  
  log_pattern = os.path.join(CRON_LOG_DIR, f"ds_cron_{last_month_str}.log")
  print(f"🧹 开始在 [{CRON_LOG_DIR}] 目录下检查上月历史日志 (月份: {last_month_str})...")
  
  for log_file in glob.glob(log_pattern):
    try:
      os.remove(log_file)
      print(f"🗑️ 已成功删除上月过期日志文件: {log_file}")
    except Exception as e:
      print(f"⚠️ 尝试删除日志文件 {log_file} 失败: {e}")

def send_billing_email(subject, body_text):
  """群发邮件发送逻辑"""
  if not SENDER_PASSWD or not RECEIVER_EMAILS:
    print("❌ 邮件环境变量配置不完整，无法发送")
    return False

  receiver_list = [email.strip() for email in RECEIVER_EMAILS.split(",") if email.strip()]
  msg = MIMEMultipart()
  msg["From"] = SENDER_EMAIL
  msg["To"] = ", ".join(receiver_list)
  msg["Subject"] = subject
  msg.attach(MIMEText(body_text, "plain", "utf-8"))

  try:
    with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
      server.starttls()
      server.login(SENDER_EMAIL, SENDER_PASSWD)
      server.sendmail(SENDER_EMAIL, receiver_list, msg.as_string())
    print(f"📧 账单邮件已群发至 {len(receiver_list)} 个邮箱")
    return True
  except Exception as e:
    print(f"❌ SMTP 发送异常: {e}")
    return False

def main():
  today = datetime.now()
  today_str = today.strftime("%Y-%m-%d")
  current_month_str = today.strftime("%Y-%m")
  
  record = load_last_record()
  current_balance = get_current_balance()
  
  if current_balance is None:
    print(f"❌ [{today_str}] 余额获取失败，等待明日重试...")
    return

  if record is None:
    print(f"ℹ️ 首次运行，已初始化当前余额并强制落盘。")
    save_current_record(today_str, current_balance)
    return

  last_date_str, last_balance = record
  last_record_month = last_date_str[:7]

  # 判断是否属于日常维护时间
  if last_record_month == current_month_str:
    print(f"⏰ [{today_str}] 本月账单已算过。今日仅更新最新余额储备并落盘。")
    return

  # 跨月或漏算，触发账单结算
  print(f"🔍 检测到跨月结算信号，开始清算...")
  
  total_recharge = 0.0
  if current_balance > last_balance:
    diff = current_balance - last_balance
    recharge_count = math.ceil(diff / SINGLE_RECHARGE)
    total_recharge = recharge_count * SINGLE_RECHARGE

  # 1. 计算真实的官方花费
  real_cost = last_balance + total_recharge - current_balance
  
  # 2. 计算乘以 150% 后的报销花费
  reimbursement_cost = real_cost * BUDGET_MULTIPLIER

  # 组装给财务报销方的邮件内容
  subject = f"【财务报销】玖结点公司调用 DeepSeek API 月度消费账单 ({last_date_str} 至 {today_str})"
  body_text = (
    f"拼乐公司财务及报销相关负责人，您好：\n\n"
    f"以下为您推送上一个周期玖结点公司调用 DeepSeek API 官方接口实际消费对账数据（已包含额外调整额度），请查收：\n"
    f"------------------------------------\n"
    f" 统计周期 : {last_date_str} 至 {today_str}\n"
    f" 应报销总金额: {reimbursement_cost:.2f} 元 (CNY)  <-- (已包含 150% 额度调整)\n"
    f" 实际消耗参考: {real_cost:.2f} 元 (CNY)\n"
    f" 清算时间 : {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    f"------------------------------------\n\n"
    f"玖结点公司开户银行：招商银行 北京雍和宫支行\n\n"
    f"对公账户名称：北京玖结点信息技术有限公司\n\n"
    f"对公账户号码：110954941210801\n\n"
    f"提示：本邮件由系统对账脚本自动清算并投递。"
  )

  # 3. 先执行存储记录并确保安全落盘（核心改动）
  # 提前移到此处，即使后续邮件发送阶段因公网网络闪断，本地月份标志位也已翻篇，保护数据安全。
  save_current_record(today_str, current_balance)
  
  # 4. 后发送邮件与执行清理
  email_success = send_billing_email(subject, body_text)
  
  if email_success:
    print("🎉 本次账单结算及发送完整流程顺利结束。")   
  else:
    print("❌ 警告：本地账单数据已更新落盘，但推送邮件发送失败。请及时手动检查网络或邮件设置！")
  
  # 清理上个月的 crontab 历史日志
  clean_previous_month_log(today)

if __name__ == "__main__":
  main()