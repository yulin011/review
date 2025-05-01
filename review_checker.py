import requests
import time
import smtplib
import logging
from email.mime.text import MIMEText
from email.header import Header
import sys
import json
import os
import threading
from datetime import datetime, timedelta
from email.utils import formataddr
import re
from bs4 import BeautifulSoup

# ======================== 邮件配置 =========================
EMAIL_SENDER = "此处为用来发邮件的邮箱"
SMTP_SERVER = "smtp.163.com"
SMTP_PORT = 465
EMAIL_PASSWORD = "此处为EMAIL_SENDER对应的授权码"
EMAIL_COPY_RECEIVER = "此处为每封邮件的抄送邮箱"

CHECK_INTERVAL = 1800  # 每30分钟检查一次
COOKIE_UPDATE_INTERVAL = 86400  # 每24小时更新一次 session

# ======================== 日志设置 =========================
logging.basicConfig(
    filename='review_check.log',
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)

# ======================== 邮件发送函数 =========================
def send_email(receiver, subject, body, name="用户", only_copy=False):
    msg = MIMEText(body, 'plain', 'utf-8')
    msg['From'] = formataddr((str(Header('论文评审监控', 'utf-8')), EMAIL_SENDER))
    msg['To'] = Header(name, 'utf-8')
    msg['Subject'] = Header(subject, 'utf-8')

    try:
        server = smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT)
        server.login(EMAIL_SENDER, EMAIL_PASSWORD)
        to_list = [EMAIL_COPY_RECEIVER] if only_copy else [receiver, EMAIL_COPY_RECEIVER]
        server.sendmail(EMAIL_SENDER, to_list, msg.as_string())
        server.quit()
        logging.info(f"{name} - 邮件发送成功: {subject} -> {receiver}, 副本: {EMAIL_COPY_RECEIVER}")
    except Exception as e:
        logging.error(f"{name} - 邮件发送失败: {e}")

# ======================== 登录获取 Cookie =========================
def get_fresh_cookies(username, password, name="未知"):
    login_url = "https://11414.lwglxt.com/checklogin/"
    session = requests.Session()
    login_data = {
        "login_name_common": username,
        "login_pwd_common": password,
        "menu": "checklogin"
    }

    try:
        response = session.post(login_url, data=login_data, timeout=10)
        if response.ok and "PHPSESSID" in session.cookies:
            return {"PHPSESSID": session.cookies["PHPSESSID"]}
        else:
            logging.error(f"{name} 登录失败")
            return None
    except Exception as e:
        logging.error(f"{name} 登录异常: {e}")
        return None

# ======================== 状态检查函数 =========================
def parse_expert_status(html, student_type):
    soup = BeautifulSoup(html, 'html.parser')
    expert_links = soup.find_all("a", id=re.compile(r"expert_hr_\d+"))
    expert_count = len(expert_links)
    results = []

    for i in range(0, expert_count ):
        div = soup.find("div", id=f"div_expert_{i}")
        if div:
            expert_html = str(div)
            reviewed = "未评阅完毕" not in expert_html
            score = "-"
            opinion = "-"

            # 获取评分
            soupScore = BeautifulSoup(expert_html, 'html.parser')
            score_id = 'value_6' if student_type == 1 else 'value_5'
            select_tag = soupScore.find('select', id=score_id)
            selected_option = select_tag.find('option', selected=True) if select_tag else None
            score = selected_option['value'] if selected_option else None

            # 获取答辩意见
            # 匹配“(√)”后面紧跟的非括号内容，直到下一个“(”或字符串结尾
            match_opinion = re.search(r'[（(]√[)）]\s*([^()]+)', expert_html)
            if match_opinion:
                opinion = match_opinion.group(1).strip()
            else:
                opinion = None

            results.append({
                "reviewed": reviewed,
                "score": score,
                "opinion": opinion
            })
    return expert_count, results

# ======================== 主监控函数 =========================
def monitor_account(account_config):
    url = account_config['url']
    username = account_config['username']
    password = account_config['password']
    receiver = account_config['receiver']
    name = account_config.get('comment', '未知')
    student_type = account_config.get('type', 1)

    cookies = get_fresh_cookies(username, password, name)
    if cookies is None:
        send_email(receiver, f"[{name}] 登录失败", "无法获取 PHPSESSID，检查账号密码。", name)
        return

    response = requests.get(url, cookies=cookies, timeout=10)
    if not response.ok:
        send_email(receiver, f"[{name}] 页面加载失败", "无法访问评审页面，请检查网络或 Cookie。", name)
        return

    html = response.text
    prev_count, prev_status = parse_expert_status(html, student_type)

    def format_status(status):
        lines = []
        for idx, r in enumerate(status, 1):
            lines.append(f"专家{idx}：{'已评阅' if r['reviewed'] else '未评阅'} | 分数：{r['score']} | 答辩意见：{r['opinion']}")
        return "\n".join(lines)

    send_email(receiver, f"[{name}] 启动成功 - 当前评审状态", format_status(prev_status), name, only_copy=True)

    threshold = 3 if student_type == 3 else 2
    if len(prev_status) >= threshold and all(r['reviewed'] for r in prev_status):
        send_email(receiver, f"[{name}] 所有专家已评阅 - 程序终止", format_status(prev_status), name)
        return

    last_cookie_update = datetime.now()

    while True:
        time.sleep(CHECK_INTERVAL)

        if (datetime.now() - last_cookie_update).total_seconds() > COOKIE_UPDATE_INTERVAL:
            new_cookies = get_fresh_cookies(username, password, name)
            if new_cookies:
                cookies = new_cookies
                last_cookie_update = datetime.now()

        try:
            html = requests.get(url, cookies=cookies, timeout=10).text
            current_count, current_status = parse_expert_status(html, student_type)

            # 动态打印所有专家状态日志
            status_list = []
            for idx, expert in enumerate(current_status, start=1):
                state = '已评阅' if expert['reviewed'] else '未评阅'
                status_list.append(f"专家{idx}：{state}")
            logging.info(f"[{name}] 状态 - " + "，".join(status_list))

            # 检查专家人数变化
            if current_count > prev_count:
                send_email(receiver, f" [{name}] 检测到新增专家", f"原有专家数：{prev_count}，当前：{current_count}", name)

            # 检查是否有新评阅完成
            for i in range(min(prev_count, current_count)):
                if not prev_status[i]['reviewed'] and current_status[i]['reviewed']:
                    send_email(receiver, f" [{name}] 专家{i+1} 已完成评审", format_status(current_status), name)

            if len(current_status) >= threshold and all(r['reviewed'] for r in current_status):  # <<< 修改点
                send_email(receiver, f"[{name}] 所有评审完成 - 程序终止", format_status(current_status), name)
                break

            prev_count = current_count
            prev_status = current_status

        except Exception as e:
            logging.error(f"[{name}] 检查异常: {e}")
            continue

# ======================== 多账号入口 =========================
if __name__ == "__main__":
    config_file = 'accounts.json'
    if not os.path.exists(config_file):
        logging.error("未找到配置文件 accounts.json")
        sys.exit(1)

    with open(config_file, 'r', encoding='utf-8') as f:
        accounts = json.load(f)

    threads = []
    for account in accounts:
        t = threading.Thread(target=monitor_account, args=(account,))
        t.start()
        threads.append(t)

    for t in threads:
        t.join()
