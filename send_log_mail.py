import datetime
import os
import smtplib
import sys
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from dotenv import load_dotenv

load_dotenv()

# ==================== 配置区域 ====================
LOG_FILE_PATH = os.getenv("LOG_FILE_PATH") or "./server.log"  # 要监控的日志文件路径
CHECKPOINT_FILE = "data/log_monitor_seek.txt"  # 记录读取位置的检查点文件
SENDER_PASSWD = os.getenv("EMAIL_PASSWORD")  # 从环境变量获取邮箱授权码/密码

# 固定的临时附件路径（每次写入都会自动覆盖，防止文件堆积）
TEMP_ATTACH_PATH = "data/temp_increment_log.txt"

# 邮件配置
SMTP_SERVER = "smtp.qq.com"  # SMTP服务器地址
SMTP_PORT = 587  # SMTP端口（STARTTLS 模式使用 587）
SENDER_EMAIL = "79809620@qq.com"  # 发件人邮箱
RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL")  # 接收人邮箱
# ==================================================


def get_last_position():
    """获取上次读取的文件指针位置"""
    # 确保 data 目录存在
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
    """更新文件指针位置"""
    with open(CHECKPOINT_FILE, "w") as f:
        f.write(str(pos))


def send_email(attach_file_path, subject, body_text, display_filename):
    """
    通用邮件发送函数
    display_filename: 允许指定附件在邮件里显示的名字（这样即使本地是统一文件名，邮件里也可以显示带日期的名字）
    """
    if not SENDER_PASSWD:
        print("未找到 EMAIL_PASSWORD 环境变量，无法发送邮件")
        return False

    msg = MIMEMultipart()
    msg["From"] = SENDER_EMAIL
    msg["To"] = RECEIVER_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(body_text, "plain", "utf-8"))

    try:
        if attach_file_path and os.path.exists(attach_file_path):
            with open(attach_file_path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                # 在邮件中显示更友好的、带日期的文件名
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
            server.send_message(msg)
        print(f"邮件发送成功: {subject}")
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

    # 如果日志文件被轮转(Rotation)或清空了，重置指针
    if file_size < last_pos:
        last_pos = 0

    new_logs = []

    with open(LOG_FILE_PATH, "r", encoding="utf-8", errors="ignore") as f:
        f.seek(last_pos)
        # 读取新内容
        new_logs = f.readlines()
        # 获取当前最新指针
        current_pos = f.tell()

    # 如果有新日志，保存为统一命名的临时文件，并以附件形式发送
    if new_logs:
        today_str = datetime.datetime.now().strftime("%Y-%m-%d")

        # 使用 'w' 模式写入：如果文件存在，会直接覆盖旧内容；如果不存在则新建
        with open(TEMP_ATTACH_PATH, "w", encoding="utf-8") as temp_file:
            temp_file.writelines(new_logs)

        # 准备邮件标题、正文
        subject = f"每日新增日志汇报 - {today_str}"
        body_text = f"您好，附件为今日 {today_str} 的新增日志，请查收。"

        # 虽然本地文件名是统一的，但传给收件人时，我们让附件显示为带日期的名字（例如：log_2026-06-05.txt）
        email_filename = f"log_{today_str}.txt"

        # 发送邮件
        success = send_email(TEMP_ATTACH_PATH, subject, body_text, email_filename)

        # 发送成功后将其删除，双重保险保持轻量
        if success and os.path.exists(TEMP_ATTACH_PATH):
            os.remove(TEMP_ATTACH_PATH)
    else:
        print("没有新日志产生，无需发送。")

    # 无论是否有新日志，都更新位置
    update_position(current_pos)


if __name__ == "__main__":
    main()