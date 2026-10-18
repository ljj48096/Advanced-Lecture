"""邮件通知：SMTP SSL 发送。"""
import smtplib
from email.header import Header
from email.mime.text import MIMEText


def send_email(email_cfg, subject, content):
    smtp_server = email_cfg.get("smtp_server", "smtp.163.com")
    smtp_port = email_cfg.get("smtp_port", 465)
    sender = email_cfg["sender"]
    pwd = email_cfg.get("sender_pwd", "")
    receiver = email_cfg.get("receiver", sender)

    msg = MIMEText(content, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = sender
    msg["To"] = receiver

    server = smtplib.SMTP_SSL(smtp_server, smtp_port, timeout=15)
    try:
        server.login(sender, pwd)
        server.sendmail(sender, [receiver], msg.as_string())
    finally:
        server.quit()
