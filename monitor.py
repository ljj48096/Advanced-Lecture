#!/usr/bin/env python3
"""同济高等讲堂 监控 + 自动抢课系统

按 config.json 的 target_weekdays 筛选课程：
  - 符合星期且有余量的课程 -> 发邮件通知一次
  - 到达报名开放时间(TimeSigningUp, 以服务器时钟为准) -> 自动抢课
  - 抢课成功 -> 停止监控该课程，继续监控其余课程
状态持久化到 SQLite (course_status.db)，日志写入 monitor.log。

用法: python monitor.py
"""
import json
import logging
import os
import random
import sys
import threading
import time
from datetime import datetime

from gdjt_client import GdjtClient, SessionExpired
import db
import mailer

CONFIG_PATH = "config.json"
TERMINAL_KEYWORDS = ("满", "已报名", "存在", "不能", "无权", "人脸", "超过", "频繁", "无效时间")


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def make_client(cfg):
    return GdjtClient(
        session_id=cfg["session"]["pg_asp_session_id"],
        weixin_code_id=cfg["session"].get("pg_asp_weixin_code_id", ""),
    )


def setup_logging(log_path):
    logger = logging.getLogger("monitor")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger


def safe_weekday(s):
    """'2026-10-19T09:30:00' -> 1..7 (1=周一), 解析失败返回 0"""
    try:
        return datetime.fromisoformat(s.strip()).weekday() + 1
    except Exception:
        return 0


def weekday_cn(wd):
    return {1: "周一", 2: "周二", 3: "周三", 4: "周四", 5: "周五", 6: "周六", 7: "周日"}.get(wd, f"星期{wd}")


def try_send_email(cfg, log, subject, content):
    """发送邮件，失败只记日志不抛异常。返回是否成功。"""
    try:
        mailer.send_email(cfg["email"], subject, content)
        return True
    except Exception as e:
        log.error("邮件发送失败: %s", e)
        return False


def spin_sleep(seconds):
    """睡眠 seconds 秒，期间在终端显示转圈动画，表示程序仍在运行。"""
    frames = "|/-\\"
    end = time.time() + seconds
    i = 0
    while time.time() < end:
        sys.stdout.write(f"\r  监控运行中 {frames[i % 4]} ")
        sys.stdout.flush()
        time.sleep(0.2)
        i += 1
    sys.stdout.write("\r" + " " * 24 + "\r")
    sys.stdout.flush()


def is_weekday_match(wd, rule):
    if rule == "all":
        return True
    return wd in rule


def parse_server_time(s):
    return GdjtClient._parse_server_time(s)


def fetch_matching(cfg, client):
    """拉取【星期匹配 + 校区匹配 + 级别匹配 + 未报满】的课程，并返回各状态课程总数。

    返回 (匹配课程列表, {状态码: 总数})。
    状态码: 1=有余位 2=已报满 3=未开启报名 4=已结束。
    """
    rule = cfg.get("target_weekdays", "all")
    campus = cfg.get("target_campus", "")
    level_limit = cfg.get("class_level_limit") or {}
    result = {}
    counts = {}

    # 有余位(1) + 未开启报名(3)：翻页找匹配课程，并记总数
    for status in (1, 3):
        page = 1
        while page <= 20:
            d = client.get_lecture_list(page=page, page_size=50, status=status)
            data = d.get("data") or {}
            rows = data.get("row", [])
            if page == 1:
                counts[status] = data.get("totalcount", 0)
            for r in rows:
                wd = safe_weekday(r.get("LectureTimeStart", ""))
                if not wd or not is_weekday_match(wd, rule):
                    continue
                if campus and campus not in (r.get("Venue") or ""):
                    continue
                if level_limit and (r.get("ClassLevelName") or "") not in level_limit:
                    continue
                result[r["DataId"]] = r
            if not rows or len(rows) < 50:
                break
            page += 1

    # 已报满(2) + 已结束(4)：只取总数，不翻页
    for status in (2, 4):
        d = client.get_lecture_list(page=1, page_size=1, status=status)
        counts[status] = (d.get("data") or {}).get("totalcount", 0)

    return list(result.values()), counts


def notify_and_grab(cfg, client, lec, log, active):
    uid = lec["DataId"]
    name = lec.get("Cathedra", "")
    wd = safe_weekday(lec.get("LectureTimeStart", ""))
    if not wd:
        return
    lv = lec.get("ClassLevelName") or ""
    remain = int(lec.get("Capacity") or 0) - int(lec.get("SignupNum") or 0)
    level_limit = cfg.get("class_level_limit") or {}

    if db.is_grabbed(uid) or db.is_failed(uid):
        return
    if lec.get("LectureHallStatus") == 2:  # 已报满
        return

    # 邮件通知（有余量且未通知过，只发一次；含「未开启报名」即还没到抢课时间的课）
    if remain > 0 and not db.is_notified(uid):
        status_hint = {1: "有余位（现在就能抢）", 3: "未开启报名（还没到抢课时间）"}.get(
            lec.get("LectureHallStatus"), lec.get("LectureHallStatusName", ""))
        subject = f"[抢课通知] 有余位：{name}"
        content = (f"【发现可抢课程】\n"
                   f"课程：{name}\n"
                   f"星期：{weekday_cn(wd)}\n"
                   f"状态：{status_hint}\n"
                   f"剩余名额：{remain}\n"
                   f"报名开放时间：{lec.get('TimeSigningUp')}\n"
                   f"地点：{lec.get('Venue')}")
        if try_send_email(cfg, log, subject, content):
            db.mark_notified(uid, name, wd, lv)
            log.info("已发送邮件通知: 《%s》 剩余 %d", name, remain)

    # 自动抢课：到达开放时间前后 lead 秒内启动抢课线程
    if remain <= 0:
        return
    # 级别限抢数量：该级别已抢满则跳过
    if level_limit and lv in level_limit and db.count_grabbed_by_level(lv) >= level_limit[lv]:
        return
    try:
        open_ts = parse_server_time(lec.get("TimeSigningUp", ""))
    except Exception:
        return
    lead = cfg.get("grab_prepare_lead_seconds", 180)
    if client.server_now() >= open_ts - lead and uid not in active:
        active[uid] = True
        t = threading.Thread(target=grab_worker, args=(cfg, uid, log, active), daemon=True)
        t.start()


def grab_worker(cfg, uid, log, active):
    """单门课程完整抢课流程（独立 client + 服务器时钟精确等待）。"""
    client = make_client(cfg)
    try:
        det = client.get_lecture_detail(uid)
        if det.get("state") != "success":
            log.error("[%s] 获取详情失败: %s", uid, det.get("msg", det))
            return
        data = det["data"]
        title = data.get("Cathedra", uid)
        wd = safe_weekday(data.get("LectureTimeStart", ""))
        lv = data.get("ClassLevelName") or ""
        if data.get("XSSigningUp"):
            log.info("《%s》 已报名过", title)
            db.mark_grabbed(uid, title, wd)
            return
        if data.get("LectureHallStatus") == 2:
            log.info("《%s》 已报满", title)
            return

        sc = client.set_credentials(uid)
        if sc.get("state") != "success":
            log.error("《%s》 SetCredentials 失败: %s", title, sc.get("msg", sc))
            return
        code = (sc.get("data") or {}).get("code")
        if not code:
            log.error("《%s》 未取得凭证码", title)
            return

        # 精确等到报名开放时间（服务器时钟）
        open_ts = parse_server_time(data.get("TimeSigningUp") or "")
        while True:
            remain = open_ts - client.server_now()
            if remain <= 0:
                break
            if remain > 3:
                time.sleep(min(1.0, remain - 2.5))
            else:
                time.sleep(0.05)
        log.info("《%s》 到点，提交报名", title)

        submitted = False
        for i in range(200):
            try:
                r = client.signup(code)
            except SessionExpired:
                raise
            except Exception:
                time.sleep(0.35)
                continue
            if r.get("state") == "success":
                submitted = True
                break
            msg = str(r.get("msg", r))
            if any(k in msg for k in ("未登录", "登录失效", "登录过期")):
                raise SessionExpired(msg)
            if any(k in msg for k in TERMINAL_KEYWORDS):
                log.info("《%s》 报名失败(终态): %s", title, msg)
                db.mark_failed(uid, title, wd, lv)
                try_send_email(cfg, log, f"[抢课失败] {title}",
                               f"【抢课失败】\n课程：{title}\n星期：{weekday_cn(wd)}\n原因：{msg}")
                return
            time.sleep(0.35)
        if not submitted:
            log.error("《%s》 重试后仍失败", title)
            return

        for _ in range(20):
            st = client.get_signin_time(uid)
            state = (st.get("data") or {}).get("state")
            if state == 1:
                db.mark_grabbed(uid, title, wd, lv)
                sign_up_time = (st.get("data") or {}).get("signUpTime", "")
                log.info("🎉 《%s》 抢课成功！继续监控其他符合条件课程", title)
                try_send_email(cfg, log, f"[抢课成功] {title}",
                               f"【抢课成功】\n课程：{title}\n星期：{weekday_cn(wd)}\n"
                               f"报名时间：{sign_up_time}\n地点：{data.get('Venue')}")
                return
            if state == -1:
                log.info("《%s》 服务端确认失败", title)
                db.mark_failed(uid, title, wd, lv)
                try_send_email(cfg, log, f"[抢课失败] {title}",
                               f"【抢课失败】\n课程：{title}\n星期：{weekday_cn(wd)}\n"
                               f"原因：服务端确认报名失败")
                return
            time.sleep(1.0)
        log.warning("《%s》 确认超时，请到「我的报名」核对", title)
    except SessionExpired as e:
        log.error("会话失效，需重新抓 Cookie 更新 config.json: %s", e)
    except Exception as e:
        log.error("[%s] 抢课异常: %s", uid, e)
    finally:
        active.pop(uid, None)


def main():
    cfg = load_config()
    log = setup_logging(cfg.get("log_path", "./monitor.log"))
    db.init_db()
    os.environ["GDJT_CLIENT_AUTH"] = cfg.get("authorization", "")

    client = make_client(cfg)
    try:
        d = client.get_lecture_list(page=1, page_size=1, status=0)
        if d.get("state") != "success":
            log.error("登录态无效: %s", d.get("msg", d))
            log.error("请重新抓 Cookie 更新 config.json 后重启")
            sys.exit(2)
        if not client.is_finish_face():
            log.error("未完成人脸采集，无法报名。请先在微信里完成人脸采集后再运行。")
            sys.exit(3)
        log.info("✓ 会话有效，服务器时钟偏差 %.3fs", client.server_offset)
    except SessionExpired as e:
        log.error("会话失效: %s", e)
        sys.exit(2)

    log.info("开始监控: target_weekdays=%s, target_campus=%s, class_level_limit=%s, 轮询间隔=%ss(+随机1~3s)",
             cfg.get("target_weekdays"), cfg.get("target_campus", ""),
             cfg.get("class_level_limit"), cfg.get("poll_interval", 5))
    active = {}
    poll = cfg.get("poll_interval", 5)
    while True:
        try:
            lectures, counts = fetch_matching(cfg, client)
            log.info("本轮扫描: 有余位=%d 未开启=%d 已报满=%d 已结束=%d | 匹配到可抢=%d",
                     counts.get(1, 0), counts.get(3, 0), counts.get(2, 0), counts.get(4, 0),
                     len(lectures))
            for lec in lectures:
                notify_and_grab(cfg, client, lec, log, active)
        except SessionExpired as e:
            log.error("会话失效: %s", e)
            log.error("请重新抓 Cookie 更新 config.json 后重启程序")
            sys.exit(2)
        except Exception as e:
            log.error("本轮异常(继续下一轮): %s", e)
        # 轮询间隔 + 随机 1~3 秒抖动，避免固定频率被识别；期间显示转圈
        spin_sleep(poll + random.randint(1, 3))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出")
