#!/usr/bin/env python3
"""同济高等讲堂 监控 + 自动抢课系统

按 config.json 的 target_weekdays / target_campus / class_level_limit 筛选课程，
再用 SetCredentials 探测报名资格：
  - 不匹配筛选条件、或无报名资格的课程不计入统计，只汇总到「已过滤」
  - 到达报名开放时间(TimeSigningUp, 以服务器时钟为准) -> 自动抢课
  - 不做冷却：只要课程还可抢，每轮扫描都会重新发一次抢课请求
  - 抢课线程内每 grab_retry_interval(默认 0.5s) 提交一次，直到抢到/没余位/终态失败
  - 抢课成功 -> 发邮件通知，并停止监控该课程，继续监控其余课程
  - 提交时发现已报满 -> 当作「没有余位」继续监控，不标记失败
状态持久化到 SQLite (course_status.db)，日志写入 monitor.log。

用法: python monitor.py
"""
import json
import logging
import os
import random
import re
import sys
import threading
import time
import unicodedata
from datetime import datetime

from gdjt_client import GdjtClient, SessionExpired
import db
import mailer

CONFIG_PATH = "config.json"
# 提交接口返回「已满」类提示：说明名额已被抢光，按「没有余位」继续监控，
# 不标记失败、也不冷却——等有人退课释放名额后还能再抢。
FULL_KEYWORDS = ("满",)
# 其他终态错误（资格/人脸/重复报名等），标记失败后不再重试。
TERMINAL_KEYWORDS = ("已报名", "存在", "不能", "无权", "人脸", "超过", "无效时间")
# 限流类提示：只放缓节奏，不算失败，更不能标记终态（否则一次限流就永久放弃这门课）
RATE_LIMIT_KEYWORDS = ("频繁", "过快", "稍后", "冷却")
# 凭证码失效类提示：服务端让你「重新进入详情页」= SetCredentials 重新领码。
# 复用同一个 code 会一直被拒，必须换新码再提交。
CODE_INVALID_KEYWORDS = ("重新进入详情页", "重新进入", "刷新页面", "重新获取", "凭证")
MAX_CODE_REFRESHES = 4     # 连续重新领码这么多次仍被拒，说明是服务端真实拒绝，本轮放弃

# 「成功但没拿到凭证码」多半是已满，资格探测的判定结果缓存这么久再重探。
# 注意：这只是资格探测的缓存，不影响抢课——抢课不做任何冷却，每轮都会尝试。
NO_CODE_TTL = 300

# 单个抢课线程最长连续提交多久；超时后交给下一轮扫描重新起线程。
GRAB_MAX_SECONDS = 180

# 日志去重：key -> 上次已提示的消息。同一条消息只打一次，内容变了才再打。
_noted = {}
_start_logged = {}   # uid -> 上次打印「开始抢课」的时间


def note_once(key, log, msg):
    """同一条消息只提示一次（状态变化后内容不同，会再次提示）。"""
    if _noted.get(key) == msg:
        return
    _noted[key] = msg
    log.info("%s", msg)


def note_grab_start(uid, log, msg, gap=60):
    """同一门课 60 秒内只打印一次「开始抢课」，避免每轮扫描刷屏。"""
    now = time.time()
    if now - _start_logged.get(uid, 0) < gap:
        return
    _start_logged[uid] = now
    log.info("%s", msg)


def note_full(uid, title, reason, log):
    """同一种「已满」原因只提示一次，原因变了再提示。"""
    note_once((uid, "full"), log, f"{title} {reason}，下一轮继续尝试")


def bell():
    """终端响一声，抢课成功时提醒。"""
    try:
        sys.stdout.write("\a")
        sys.stdout.flush()
    except Exception:
        pass


def success_banner(log, title, wd_cn, sign_up_time, venue):
    """抢课成功时打一个醒目的横幅，并响一声提示。"""
    console_clear()
    bell()
    line = "=" * 56
    log.info(line)
    log.info("🎉 抢课成功：%s", title)
    log.info("   星期：%s   报名时间：%s", wd_cn, sign_up_time)
    log.info("   地点：%s", venue)
    log.info("   继续监控其他符合条件课程")
    log.info(line)


def log_limit_progress(cfg, log):
    """打印各级别「已抢/上限」，方便判断为什么某些课被跳过。"""
    lim = cfg.get("class_level_limit") or {}
    if not lim:
        return
    parts = [f"{lv} {db.count_grabbed_by_level(lv)}/{n}" for lv, n in lim.items()]
    log.info("级别限抢进度: %s", ", ".join(parts))


def relax_console_encoding():
    """GBK 控制台遇到 🎉/★ 等字符会抛 UnicodeEncodeError，改成替换为 ? 不报错。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


def make_client(cfg):
    return GdjtClient(
        session_id=cfg["session"]["pg_asp_session_id"],
        weixin_code_id=cfg["session"].get("pg_asp_weixin_code_id", ""),
    )


class ConsoleFilter(logging.Filter):
    """控制台过滤：排除「本轮扫描」这类原地更新的状态行，避免刷屏。"""

    def filter(self, record):
        return "本轮扫描" not in record.getMessage()


_last_status_w = 0


def _display_width(text):
    """终端显示宽度：中文/全角按 2 列算，避免状态行残留。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def console_status(text):
    """在控制台原地更新一行状态（覆盖上一次，不换行，只显示最新一条）。"""
    global _last_status_w
    width = _display_width(text)
    pad = " " * max(0, _last_status_w - width)
    sys.stdout.write("\r" + text + pad)
    sys.stdout.flush()
    _last_status_w = width


def console_clear(width=70):
    """清掉原地状态行，避免日志和状态行挤在同一行。"""
    global _last_status_w
    n = max(width, _last_status_w)
    sys.stdout.write("\r" + " " * n + "\r")
    sys.stdout.flush()
    _last_status_w = 0


# ---------- 控制台彩色日志（只影响终端，文件日志保持纯文本）----------

def _enable_ansi():
    """让 Windows 控制台支持 ANSI 转义；失败则退回无色输出。"""
    if os.name != "nt":
        return True
    ok = False
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        for std in (-11, -12):                       # STDOUT / STDERR
            handle = kernel32.GetStdHandle(std)
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                ok = bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004)) or ok
    except Exception:
        return False
    return ok


# 日志输出到终端才加色（StreamHandler 默认写 stderr）；尊重 NO_COLOR 约定
COLOR_ENABLED = (sys.stderr.isatty() and "NO_COLOR" not in os.environ and _enable_ansi())

_RESET = "\033[0m"
_STYLE = {
    "bold_green": "\033[1;32m",
    "green": "\033[32m",
    "red": "\033[1;31m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
}

# 关键字 -> 颜色；按长度倒序参与正则，保证长词优先匹配
_KEYWORDS = {
    "抢课成功": "bold_green",
    "会话有效": "green",
    "已报满": "yellow",
    "已满": "yellow",
    "未取得凭证": "yellow",
    "到点": "cyan",
    "失败": "red",
    "异常": "red",
    "失效": "red",
}
_KEYWORD_RE = re.compile("|".join(
    re.escape(k) for k in sorted(_KEYWORDS, key=len, reverse=True)))

_LEVEL_STYLE = {
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[1;31m",
    logging.CRITICAL: "\033[1;31m",
}


def _highlight(text):
    """给消息里的关键字上色（未启用颜色时原样返回）。"""
    if not COLOR_ENABLED:
        return text
    return _KEYWORD_RE.sub(
        lambda m: _STYLE[_KEYWORDS[m.group(0)]] + m.group(0) + _RESET, text)


class ConsoleFormatter(logging.Formatter):
    """控制台专用格式：级别与关键字上色；复制记录，不影响文件日志。"""

    def format(self, record):
        rec = logging.makeLogRecord(record.__dict__)
        rec.msg, rec.args = _highlight(record.getMessage()), ()
        text = super().format(rec)
        style = _LEVEL_STYLE.get(record.levelno)
        if COLOR_ENABLED and style:
            text = text.replace(f"[{record.levelname}]",
                                f"{style}[{record.levelname}]{_RESET}", 1)
        return text


def setup_logging(log_path):
    logger = logging.getLogger("monitor")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
        fh = logging.FileHandler(log_path, encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler()
        sh.setFormatter(ConsoleFormatter("%(asctime)s [%(levelname)s] %(message)s",
                                         "%Y-%m-%d %H:%M:%S"))
        sh.addFilter(ConsoleFilter())
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


def with_level(name, level):
    """给课程名加级别前缀，如 '[校级] 课程A'；无级别则原样返回。"""
    return f"[{level}] {name}" if level else name


def try_send_email(cfg, log, subject, content):
    """发送邮件，失败只记日志不抛异常。返回是否成功。"""
    try:
        mailer.send_email(cfg["email"], subject, content)
        return True
    except Exception as e:
        log.error("邮件发送失败: %s", e)
        return False


DEFAULT_JITTER = (1, 3)   # config 未配置 poll_jitter 时的默认抖动范围（秒）


def jitter_range(cfg):
    """解析轮询抖动配置，返回 (lo, hi)；已关闭返回 None。

    config: "poll_jitter": {"enabled": true, "min": 1, "max": 3}
    未配置时默认开启 1~3 秒（兼容旧 config）。
    """
    j = cfg.get("poll_jitter")
    if not isinstance(j, dict):
        return DEFAULT_JITTER
    if not j.get("enabled", True):
        return None
    try:
        lo, hi = int(j.get("min", DEFAULT_JITTER[0])), int(j.get("max", DEFAULT_JITTER[1]))
    except (TypeError, ValueError):
        return DEFAULT_JITTER
    if hi <= 0:
        return None
    if lo > hi:
        lo, hi = hi, lo
    return max(0, lo), max(0, hi)


def random_jitter(cfg):
    """本次轮询额外睡的随机秒数（关闭抖动时为 0）。"""
    r = jitter_range(cfg)
    return 0 if r is None else random.randint(r[0], r[1])


def jitter_text(cfg):
    """用于启动日志的描述文字。"""
    r = jitter_range(cfg)
    return f"+随机{r[0]}~{r[1]}s" if r else "无抖动"


def spin_sleep(seconds, status=""):
    """睡眠 seconds 秒，期间在状态行末尾显示转圈动画，表示程序仍在运行。"""
    frames = "|/-\\"
    end = time.time() + seconds
    i = 0
    while time.time() < end:
        console_status(f"{status} {frames[i % 4]}")
        time.sleep(0.2)
        i += 1


def warn_banner(log, title, lines):
    """打印醒目的多行警告横幅。"""
    console_clear()
    log.warning("!" * 60)
    log.warning("!! %s", title)
    for ln in lines:
        log.warning("!! %s", ln)
    log.warning("!" * 60)


def is_weekday_match(wd, rule):
    if rule == "all":
        return True
    return wd in rule


def parse_server_time(s):
    return GdjtClient._parse_server_time(s)


PAGE_SIZE = 50
MAX_PAGES = 20
MAX_PROBES_PER_ROUND = 50   # 每轮最多新增多少次资格探测，避免首轮 POST 风暴

# SetCredentials 明确失败时，这些提示属于临时性错误，不作为「无资格」缓存
PROBE_TRANSIENT = ("频繁", "超时", "稍后", "系统", "未到", "未开始", "未开放",
                   "不在报名时间", "报名时间未", "还没", "人脸")
# 失败提示里出现这些词，多半是已满而不是无资格
PROBE_FULL = ("满", "上限", "名额", "人数")
# 已经报过名，仍算有资格（后续抢课线程会把它标记为已抢到）
PROBE_ALREADY = ("已报名", "已存在")


def iter_lecture_rows(client, status):
    """逐页拉取某状态的讲座行。"""
    page = 1
    while page <= MAX_PAGES:
        d = client.get_lecture_list(page=page, page_size=PAGE_SIZE, status=status)
        rows = (d.get("data") or {}).get("row", [])
        for r in rows:
            yield r
        if len(rows) < PAGE_SIZE:
            break
        page += 1


def check_eligibility(client, uid, cache, budget):
    """用 SetCredentials 探测报名资格，返回 (verdict, fresh)。

    verdict:
      ok       有凭证码（或已报名），有资格
      no       接口明确拒绝，无报名资格
      no_code  成功但没有凭证码，多半是已满，按不可报处理
      unknown  探测失败/临时错误/本轮额度用完，暂不判定
    """
    now = time.time()
    hit = cache.get(uid)
    if hit:
        verdict, ts = hit
        if verdict == "ok":
            return verdict, False
        ttl = 1800 if verdict == "no" else NO_CODE_TTL
        if now - ts < ttl:
            return verdict, False

    if budget[0] <= 0:
        return "unknown", False
    budget[0] -= 1

    try:
        sc = client.set_credentials(uid)
    except SessionExpired:
        raise
    except Exception:
        return "unknown", False

    msg = str(sc.get("msg") or "")
    if any(k in msg for k in ("未登录", "登录失效", "登录过期")):
        raise SessionExpired(msg)
    if any(k in msg for k in PROBE_ALREADY):
        cache[uid] = ("ok", now)
        return "ok", True
    if sc.get("state") != "success":
        if any(k in msg for k in PROBE_FULL):
            cache[uid] = ("no_code", now)
            return "no_code", True
        if any(k in msg for k in PROBE_TRANSIENT):
            return "unknown", False
        cache[uid] = ("no", now)
        return "no", True
    if (sc.get("data") or {}).get("code"):
        cache[uid] = ("ok", now)
        return "ok", True
    cache[uid] = ("no_code", now)
    return "no_code", True


def fetch_matching(cfg, client, elig_cache, active):
    """拉取【星期 + 校区 + 级别 + 报名资格】都通过的课程，并统计过滤数量。

    返回 (候选课程列表, {状态码: 课程数}, 条件过滤数, 无资格过滤数)。
    状态码: 1=有余位 2=已报满 3=未开启报名。所有计数均为过滤后的口径。
    """
    rule = cfg.get("target_weekdays", "all")
    campus = cfg.get("target_campus", "")
    level_limit = cfg.get("class_level_limit") or {}
    selected = {}
    seen = set()
    counts = {1: 0, 2: 0, 3: 0}
    filtered_cond = 0
    filtered_elig = 0
    budget = [MAX_PROBES_PER_ROUND]

    # 先处理可抢的 1/3，再处理已报满的 2，保证探测额度优先用在候选课上
    for status in (1, 3, 2):
        for r in iter_lecture_rows(client, status):
            uid = r.get("DataId")
            if not uid or uid in seen:
                continue
            seen.add(uid)

            wd = safe_weekday(r.get("LectureTimeStart", ""))
            if not wd or not is_weekday_match(wd, rule):
                filtered_cond += 1
                continue
            if campus and campus not in (r.get("Venue") or ""):
                filtered_cond += 1
                continue
            if level_limit and (r.get("ClassLevelName") or "") not in level_limit:
                filtered_cond += 1
                continue

            # 正在抢课的课程不再探测，避免重复领码覆盖抢课线程手里的凭证码
            if uid in active:
                verdict = "ok"
            else:
                verdict, _ = check_eligibility(client, uid, elig_cache, budget)
            if verdict == "no":
                filtered_elig += 1
                continue

            counts[status] += 1
            if status in (1, 3):
                selected[uid] = r

    return list(selected.values()), counts, filtered_cond, filtered_elig


def notify_and_grab(cfg, client, lec, log, active):
    uid = lec["DataId"]
    name = lec.get("Cathedra", "")
    wd = safe_weekday(lec.get("LectureTimeStart", ""))
    lv = lec.get("ClassLevelName") or ""
    disp = with_level(name, lv)
    if not wd:
        note_once((uid, "wd"), log, f"{disp} 跳过：上课时间无法解析（{lec.get('LectureTimeStart')!r}）")
        return
    remain = int(lec.get("Capacity") or 0) - int(lec.get("SignupNum") or 0)
    level_limit = cfg.get("class_level_limit") or {}

    if db.is_grabbed(uid):
        return                                   # 已经抢到，无需提示
    if db.is_failed(uid):
        note_once((uid, "failed"), log, f"{disp} 跳过：之前已判定终态失败，不再重试"
                                        f"（原因见当时的日志；确需重试删掉 course_status.db 里这一行）")
        return
    if lec.get("LectureHallStatus") == 2:        # 已报满
        return
    if remain <= 0:
        note_once((uid, "norem"), log,
                  f"{disp} 跳过：列表显示没有剩余名额（Capacity={lec.get('Capacity')}, "
                  f"SignupNum={lec.get('SignupNum')}）")
        return
    # 级别限抢数量：该级别已抢满则跳过（并明确告诉你为什么不动）
    if level_limit and lv in level_limit:
        got = db.count_grabbed_by_level(lv)
        if got >= level_limit[lv]:
            note_once((uid, "limit"), log,
                      f"{disp} 跳过：{lv}已抢到 {got}/{level_limit[lv]} 门，达到 "
                      f"class_level_limit 上限（改 config 可放开）")
            return
    try:
        open_ts = parse_server_time(lec.get("TimeSigningUp", ""))
    except Exception:
        note_once((uid, "ts"), log, f"{disp} 跳过：报名开放时间无法解析（{lec.get('TimeSigningUp')!r}）")
        return
    lead = cfg.get("grab_prepare_lead_seconds", 180)
    # 不做冷却：只要这门课还可抢（本轮列表里状态 1/3 且有余量），每轮扫描都会重新起线程尝试。
    # 正在抢课（线程还活着）时不重复起线程——那个线程本身就在按很短间隔反复提交。
    if client.server_now() >= open_ts - lead and uid not in active:
        active[uid] = True
        note_grab_start(uid, log,
                        f"🎯 开始抢课：{disp}（剩余 {remain} 个名额，每 "
                        f"{cfg.get('grab_retry_interval', 0.5)}s 提交一次）")
        t = threading.Thread(target=grab_worker,
                             args=(cfg, uid, log, active), daemon=True)
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
        raw_title = data.get("Cathedra", uid)
        wd = safe_weekday(data.get("LectureTimeStart", ""))
        lv = data.get("ClassLevelName") or ""
        title = with_level(raw_title, lv)   # 带级别前缀，用于日志/邮件显示
        if data.get("XSSigningUp"):
            log.info("%s 已报名过", title)
            db.mark_grabbed(uid, raw_title, wd, lv)
            return
        if data.get("LectureHallStatus") == 2:
            note_full(uid, title, "已报满（列表数据有延迟）", log)
            return

        sc = client.set_credentials(uid)
        if sc.get("state") != "success":
            log.error("%s SetCredentials 失败: %s", title, sc.get("msg", sc))
            return
        code = (sc.get("data") or {}).get("code")
        if not code:
            note_full(uid, title, "未取得凭证码（已满或无资格）", log)
            return
        _noted.pop((uid, "full"), None)   # 拿到凭证码，说明有余位，清掉「已满」提示记录

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
        log.info("%s 到点，提交报名", title)

        def finish_success(sign_up_time=""):
            """报名成功后的收尾：落库、横幅、发邮件。"""
            db.mark_grabbed(uid, raw_title, wd, lv)
            success_banner(log, title, weekday_cn(wd), sign_up_time, data.get("Venue"))
            log_limit_progress(cfg, log)
            try_send_email(cfg, log, f"[抢课成功] {title}",
                           f"【抢课成功】\n课程：{title}\n星期：{weekday_cn(wd)}\n"
                           f"报名时间：{sign_up_time}\n地点：{data.get('Venue')}")

        # 抢课循环：每隔 grab_retry_interval 秒发一次报名请求，直到抢到 / 没余位 / 终态失败
        interval = float(cfg.get("grab_retry_interval", 0.5))
        deadline = time.time() + GRAB_MAX_SECONDS
        attempts = 0
        submitted = False
        refreshes = 0
        next_note = time.time() + 15
        while time.time() < deadline:
            attempts += 1
            last_msg = ""
            try:
                r = client.signup(code)
            except SessionExpired:
                raise
            except Exception:
                time.sleep(interval)
                continue
            if r.get("state") == "success":
                submitted = True
                break
            last_msg = str(r.get("msg", r))
            if any(k in last_msg for k in ("未登录", "登录失效", "登录过期")):
                raise SessionExpired(last_msg)
            if any(k in last_msg for k in RATE_LIMIT_KEYWORDS):
                # 被限流：只放缓，不算失败、更不能标记终态（否则一次限流就永久放弃这门课）
                log.info("%s 被限流（%s），放慢后继续", title, last_msg)
                time.sleep(max(interval, 2.0))
                continue
            if any(k in last_msg for k in FULL_KEYWORDS):
                # 提交时才发现已满：不标记失败、不冷却，当作「没有余位」的课程继续监控。
                # 等有人退课释放名额，后续轮次还能再抢。
                log.info("%s 提交时已满（第 %d 次），等余位后继续", title, attempts)
                return
            if any(k in last_msg for k in TERMINAL_KEYWORDS):
                log.info("%s 报名失败(终态): %s", title, last_msg)
                db.mark_failed(uid, raw_title, wd, lv)
                return
            if any(k in last_msg for k in CODE_INVALID_KEYWORDS):
                # 「报名失败！请重新进入详情页」= 凭证码失效（只能用一次/已过期）。
                # 先查一下服务端到底有没有记上这次报名，再重新领一张码继续提交。
                sdata = (client.get_signin_time(uid).get("data") or {})
                if sdata.get("state") == 1:
                    finish_success(sdata.get("signUpTime", ""))
                    return
                if sdata.get("state") == -1:
                    log.info("%s 服务端确认报名失败", title)
                    db.mark_failed(uid, raw_title, wd, lv)
                    return
                if refreshes >= MAX_CODE_REFRESHES:
                    log.warning("%s 连续重新领码 %d 次仍被拒（%s）：不是凭证问题，是服务端真实拒绝"
                                "（多半是名额缓存未刷新/批次未放号/资格限制），本轮放弃，下一轮再试",
                                title, refreshes, last_msg)
                    return
                refreshed = client.set_credentials(uid)
                new_code = (refreshed.get("data") or {}).get("code")
                if refreshed.get("state") != "success" or not new_code:
                    note_full(uid, title,
                              f"重新领码失败（{refreshed.get('msg', '无凭证码')}）", log)
                    return
                code = new_code
                refreshes += 1
                log.info("%s 凭证码已失效，已重新领取（第 %d 次），继续提交", title, refreshes)
                time.sleep(max(interval, 0.3))
                continue
            if time.time() >= next_note:
                log.info("%s 仍在抢（已提交 %d 次，最近返回: %s）", title, attempts, last_msg)
                next_note = time.time() + 15
            time.sleep(interval)
        if not submitted:
            log.warning("%s 连续提交 %d 次仍未成功，交给下一轮扫描继续", title, attempts)
            return

        for _ in range(20):
            st = client.get_signin_time(uid)
            state = (st.get("data") or {}).get("state")
            if state == 1:
                finish_success((st.get("data") or {}).get("signUpTime", ""))
                return
            if state == -1:
                log.info("%s 服务端确认失败", title)
                db.mark_failed(uid, raw_title, wd, lv)
                return
            time.sleep(1.0)
        log.warning("%s 确认超时，请到「我的报名」核对", title)
    except SessionExpired as e:
        log.error("会话失效，需重新抓 Cookie 更新 config.json: %s", e)
    except Exception as e:
        log.error("[%s] 抢课异常: %s", uid, e)
    finally:
        active.pop(uid, None)


def main():
    relax_console_encoding()
    cfg = load_config()
    log = setup_logging(cfg.get("log_path", "./monitor.log"))
    db.init_db()
    os.environ["GDJT_CLIENT_AUTH"] = cfg.get("authorization", "")

    # Cookie 是否存在
    sid = (cfg.get("session") or {}).get("pg_asp_session_id", "")
    if not sid.strip():
        warn_banner(log, "缺少登录 Cookie！", [
            "config.json 里的 session.pg_asp_session_id 为空。",
            "请按 README「第 1 步」抓取 Cookie 并填写，或先运行: python selftest.py",
        ])
        sys.exit(2)

    client = make_client(cfg)
    try:
        d = client.get_lecture_list(page=1, page_size=1, status=0)
        if d.get("state") != "success":
            warn_banner(log, "登录 Cookie 无效或已过期！", [
                f"接口返回: {d.get('msg', d)}",
                "请按 README「第 1 步」重新抓取 Cookie 更新 config.json 后重启。",
                "可先运行: python selftest.py 自检链路。",
            ])
            sys.exit(2)
        if not client.is_finish_face():
            warn_banner(log, "未完成人脸采集！", [
                "报名前需在微信里完成人脸采集，否则无法报名。",
            ])
            sys.exit(3)
        log.info("✓ 会话有效，服务器时钟偏差 %.3fs", client.server_offset)
    except SessionExpired as e:
        warn_banner(log, "登录 Cookie 无效或已过期！", [
            f"异常: {e}",
            "请按 README「第 1 步」重新抓取 Cookie 更新 config.json 后重启。",
            "可先运行: python selftest.py 自检链路。",
        ])
        sys.exit(2)

    log.info("开始监控: target_weekdays=%s, target_campus=%s, class_level_limit=%s, 扫描间隔=%ss(%s), 抢课提交间隔=%ss",
             cfg.get("target_weekdays"), cfg.get("target_campus", ""),
             cfg.get("class_level_limit"), cfg.get("poll_interval", 5), jitter_text(cfg),
             float(cfg.get("grab_retry_interval", 0.5)))
    log_limit_progress(cfg, log)
    active = {}
    elig_cache = {}
    poll = cfg.get("poll_interval", 5)
    status = ""
    fails = 0
    while True:
        try:
            lectures, counts, filtered_cond, filtered_elig = fetch_matching(
                cfg, client, elig_cache, active)
            fails = 0
            status = (f"本轮扫描: 有余位={counts.get(1, 0)} 未开启={counts.get(3, 0)} "
                      f"已报满={counts.get(2, 0)} 可抢={len(lectures)} "
                      f"抢课中={len(active)} "
                      f"已过滤={filtered_cond + filtered_elig}"
                      f"(条件{filtered_cond}/无资格{filtered_elig})")
            log.info("%s", status)   # 写日志文件（控制台被过滤掉）
            console_status(status)   # 控制台原地刷新，只显示最新一条
            # 只要本轮列表里还有可抢的课，就都为它们发一次抢课请求（无冷却）。
            # 抢课线程内部按 grab_retry_interval（默认 0.5s）连续提交，直到没余位。
            for lec in lectures:
                notify_and_grab(cfg, client, lec, log, active)
        except SessionExpired as e:
            console_clear()
            log.error("会话失效: %s", e)
            log.error("请重新抓 Cookie 更新 config.json 后重启程序")
            sys.exit(2)
        except Exception as e:
            # 网络抖动（超时/SSL 握手超时）已由客户端自动重试 2 次，走到这里说明仍失败
            fails += 1
            console_clear()
            log.error("本轮异常(继续下一轮): %s （连续失败 %d 轮）", e, fails)
            if fails in (5, 20):
                warn_banner(log, "连续多轮请求失败，请检查网络", [
                    f"已连续失败 {fails} 轮，最近一次: {e}",
                    "1) 检查本机网络 / 校园网是否正常；",
                    "2) 若开着 Clash/梯子，请关闭「全局/TUN」或退出代理软件",
                    "   （本程序默认直连，不读 HTTP(S)_PROXY 环境变量）；",
                    "3) 若必须走代理，启动前设置 GDJT_USE_ENV_PROXY=1。",
                ])
        # 扫描间隔保持不变（poll_interval + 可选抖动）；
        # 「每 grab_retry_interval 秒发一次抢课请求」由抢课线程负责，与扫描频率无关。
        spin_sleep(poll + random_jitter(cfg), status)
