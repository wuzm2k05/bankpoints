import datetime
import json
import os
import re
import smtplib
import sys
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests
from dotenv import load_dotenv

load_dotenv()

# ==================== 配置区域 ====================
LOG_FILE_PATH = os.getenv("LOG_FILE_PATH") or "./server.log"  # 要监控的日志文件路径
CHECKPOINT_FILE = "data/log_monitor_seek.txt"  # 记录日志读取位置的文件
METRICS_CHECKPOINT_FILE = "data/metrics_checkpoint.json"  # 记录 Metrics 检查点的文件
SENDER_PASSWD = os.getenv("EMAIL_PASSWORD")  # 从环境变量获取邮箱授权码/密码

# 固定的临时附件路径
TEMP_ATTACH_PATH = "data/temp_increment_log.txt"

# 邮件配置
SMTP_SERVER = "smtp.qq.com"  # SMTP服务器地址
SMTP_PORT = 587  # SMTP端口
SENDER_EMAIL = "79809620@qq.com"  # 发件人邮箱
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL")  # 接收人邮箱

# OTel Collector 暴露的 Prometheus endpoint 地址 (例如: http://127.0.0.1:8889/metrics)
OTEL_PROMETHEUS_URL = os.getenv("OTEL_PROMETHEUS_URL")
# ==================================================

def get_last_position():
    """获取上次读取的日志文件指针位置"""
    dir_name = os.path.dirname(CHECKPOINT_FILE)
    if dir_name and not os.path.exists(dir_name):
        os.makedirs(dir_name)

    if os.path.exists(CHECKPOINT_FILE):
        try:
            with open(CHECKPOINT_FILE, "r") as f:
                return int(f.read().strip())
        except ValueError:
            return 0
    return 0


def update_position(pos):
    """更新日志文件指针位置"""
    with open(CHECKPOINT_FILE, "w") as f:
        f.write(str(pos))


def get_last_metrics_checkpoint() -> dict:
    """读取昨天的 Metrics 检查点数据"""
    if os.path.exists(METRICS_CHECKPOINT_FILE):
        try:
            with open(METRICS_CHECKPOINT_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[Metrics] 读取历史 Checkpoint 失败，将按初次运行处理: {e}")
            return {}
    return {}


def update_metrics_checkpoint(current_data: dict):
    """保存今天的 Metrics 检查点，供明天计算差值"""
    dir_name = os.path.dirname(METRICS_CHECKPOINT_FILE)
    if dir_name and not os.path.exists(dir_name):
        os.makedirs(dir_name)
    try:
        with open(METRICS_CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            json.dump(current_data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Metrics] 保存 Checkpoint 失败: {e}")


def parse_openmetrics_text(text: str) -> dict:
    """从 OTel 暴露的 OpenMetrics 文本中解析当前的内存累计数据"""
    metrics_data = {
        "chat_requests": {},  # {"success": 100, "fail": 2}
        "nodes": {},          # {"router": 50, "customer_service": 80}
        "tools": {},          # {"query_icbc_voucher_rules": 30}
        "duration_sum": 0.0,
        "duration_count": 0.0,
    }

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        # 1. 解析请求总数 (agent_chat_requests_total)
        m_req = re.search(r'agent_chat_requests_total\{.*?status="([^"]+)".*?\}\s+([\d\.]+)', line)
        if m_req:
            status, val = m_req.group(1), float(m_req.group(2))
            metrics_data["chat_requests"][status] = metrics_data["chat_requests"].get(status, 0.0) + val

        # 2. 解析 Agent 节点执行次数 (agent_node_execution_total)
        m_node = re.search(r'agent_node_execution_total\{.*?agent_name="([^"]+)".*?\}\s+([\d\.]+)', line)
        if m_node:
            node_name, val = m_node.group(1), float(m_node.group(2))
            metrics_data["nodes"][node_name] = metrics_data["nodes"].get(node_name, 0.0) + val

        # 3. 解析 Tool 工具调用次数 (tool_node_execution_total)
        m_tool = re.search(r'tool_node_execution_total\{.*?tool_name="([^"]+)".*?\}\s+([\d\.]+)', line)
        if m_tool:
            tool_name, val = m_tool.group(1), float(m_tool.group(2))
            metrics_data["tools"][tool_name] = metrics_data["tools"].get(tool_name, 0.0) + val

        # 4. 解析响应耗时直方图 (agent_chat_request_duration_seconds)
        m_dur_sum = re.search(r'agent_chat_request_duration_seconds_sum.*?\}\s+([\d\.]+)', line)
        if m_dur_sum:
            metrics_data["duration_sum"] += float(m_dur_sum.group(1))

        m_dur_cnt = re.search(r'agent_chat_request_duration_seconds_count.*?\}\s+([\d\.]+)', line)
        if m_dur_cnt:
            metrics_data["duration_count"] += float(m_dur_cnt.group(1))

    return metrics_data


def generate_metrics_summary() -> str:
    """拉取 OTel 文本，通过 Checkpoint 差值精准计算【今日纯增量数据】"""
    if not OTEL_PROMETHEUS_URL:
        return ""

    try:
        print(f"正在从 OTel Exporter ({OTEL_PROMETHEUS_URL}) 拉取 Metrics 快照...")
        resp = requests.get(OTEL_PROMETHEUS_URL, timeout=5)
        if resp.status_code != 200:
            print(f"[Metrics] 拉取失败，HTTP 状态码: {resp.status_code}")
            return ""

        # 1. 提取当前内存里的总累计数据 (Current)
        curr = parse_openmetrics_text(resp.text)
        # 2. 读取昨天的 Checkpoint 累计数据 (Previous)
        prev = get_last_metrics_checkpoint()

        # 辅助闭包：计算字典增量（处理服务重启导致的当前值 < 昨天值的情况）
        def calc_dict_delta(curr_dict: dict, prev_dict: dict) -> dict:
            delta = {}
            for k, val in curr_dict.items():
                prev_val = prev_dict.get(k, 0.0)
                # 如果当前值 >= 昨天值，取差值；若服务重启过当前值变小了，直接用当前值
                delta[k] = val - prev_val if val >= prev_val else val
            return delta

        # 3. 计算今日各维度的净增量
        today_reqs = calc_dict_delta(curr["chat_requests"], prev.get("chat_requests", {}))
        today_nodes = calc_dict_delta(curr["nodes"], prev.get("nodes", {}))
        today_tools = calc_dict_delta(curr["tools"], prev.get("tools", {}))

        # 4. 计算今日 Histogram (耗时与次数) 的净增量
        dur_sum_delta = curr["duration_sum"] - prev.get("duration_sum", 0.0)
        if dur_sum_delta < 0:
            dur_sum_delta = curr["duration_sum"]

        dur_cnt_delta = curr["duration_count"] - prev.get("duration_count", 0.0)
        if dur_cnt_delta < 0:
            dur_cnt_delta = curr["duration_count"]

        # 5. 更新今天的 Checkpoint 文件，为明天计算做准备
        update_metrics_checkpoint(curr)

        # 6. 计算今日业务指标
        success_reqs = today_reqs.get("success", 0.0)
        fail_reqs = today_reqs.get("fail", 0.0)
        total_reqs = success_reqs + fail_reqs
        success_rate = (success_reqs / total_reqs * 100) if total_reqs > 0 else 0.0

        # 今日平均耗时 = 今日新增总耗时 / 今日新增总次数
        avg_duration = (dur_sum_delta / dur_cnt_delta) if dur_cnt_delta > 0 else 0.0

        # 7. 格式化生成报告文本
        metrics_text = (
            "==================================================\n"
            "           📊 今日 Agent Metrics 运行日报         \n"
            "==================================================\n"
            f"• 今日对话请求量 : {int(total_reqs)} 次\n"
            f"• 成功完成数     : {int(success_reqs)} 次 (成功率: {success_rate:.2f}%)\n"
            f"• 失败/异常数    : {int(fail_reqs)} 次\n"
            f"• 今日平均耗时   : {avg_duration:.2f} 秒\n"
            "--------------------------------------------------\n"
            "【Agent 节点流转次数统计 (今日新增)】:\n"
        )

        has_nodes = False
        for node_name, count in today_nodes.items():
            if count > 0:
                metrics_text += f"  - {node_name}: {int(count)} 次\n"
                has_nodes = True
        if not has_nodes:
            metrics_text += "  - 今日无节点流转数据\n"

        metrics_text += (
            "--------------------------------------------------\n"
            "【Tool 工具调用次数统计 (今日新增)】:\n"
        )

        has_tools = False
        for tool_name, count in today_tools.items():
            if count > 0:
                metrics_text += f"  - {tool_name}: {int(count)} 次\n"
                has_tools = True
        if not has_tools:
            metrics_text += "  - 今日无工具调用数据\n"

        metrics_text += "==================================================\n\n"
        return metrics_text

    except Exception as e:
        print(f"[Metrics] 计算今日 Metrics 增量异常: {e}")
        return ""


def send_email(attach_file_path, subject, body_text, display_filename):
    """通用邮件发送函数"""
    if not SENDER_PASSWD:
        print("未找到 EMAIL_PASSWORD 环境变量，无法发送邮件")
        return False

    if not RECEIVER_EMAIL:
        print("未找到 RECEIVER_EMAIL 环境变量，无法发送邮件")
        return False

    receiver_list = [email.strip() for email in RECEIVER_EMAIL.split(",") if email.strip()]

    msg = MIMEMultipart()
    msg["From"] = SENDER_EMAIL
    msg["To"] = ", ".join(receiver_list)
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    try:
        if attach_file_path and os.path.exists(attach_file_path):
            with open(attach_file_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header(
                    "Content-Disposition", f"attachment; filename={display_filename}"
                )
                msg.attach(part)
    except Exception as e:
        print(f"附件读取或添加失败: {e}")
        return False

    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWD)
            server.sendmail(SENDER_EMAIL, receiver_list, msg.as_string())

        print(f"邮件成功发送至群组 (共 {len(receiver_list)} 个邮箱): {subject}")
        return True
    except Exception as e:
        print(f"SMTP 发送异常: {e}")
        return False


def main():
    if not os.path.exists(LOG_FILE_PATH):
        print(f"错误: 找不到日志文件 {LOG_FILE_PATH}")
        sys.exit(1)

    last_pos = get_last_position()
    file_size = os.path.getsize(LOG_FILE_PATH)

    if file_size < last_pos:
        last_pos = 0

    new_logs = []

    with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(last_pos)
        new_logs = f.readlines()
        current_pos = f.tell()

    today_str = datetime.datetime.now().strftime("%Y-%m-%d")

    # 1. 计算今日净增的 Metrics 统计文本
    metrics_summary_text = generate_metrics_summary()

    # 2. 判断是否有新日志或 Metrics 统计
    if new_logs or metrics_summary_text:
        attach_path = None
        email_filename = None

        if new_logs:
            with open(TEMP_ATTACH_PATH, "w", encoding="utf-8") as temp_file:
                temp_file.writelines(new_logs)
            email_filename = f"log_{today_str}.txt"
            attach_path = TEMP_ATTACH_PATH

        # 3. 组装邮件标题与正文
        subject = f"每日运维与运行指标汇报 - {today_str}"
        body_text = f"您好！以下是今日 {today_str} 的系统运行汇总：\n\n"

        if metrics_summary_text:
            body_text += metrics_summary_text

        if new_logs:
            body_text += f"今日产生增量日志 {len(new_logs)} 行，已打包至附件，请查收。"
        else:
            body_text += "今日无新增日志。"

        # 4. 发送邮件
        success = send_email(attach_path, subject, body_text, email_filename)

        if success and attach_path and os.path.exists(attach_path):
            os.remove(attach_path)
    else:
        print("没有新日志产生且未配置 Metrics 查询，跳过邮件发送。")

    update_position(current_pos)


if __name__ == "__main__":
    main()