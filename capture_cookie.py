"""mitmproxy 插件：抓取 lecture.tongji.edu.cn 请求里的登录 Cookie，
并自动写入 config.json 的 session 字段（无需手动复制粘贴）。

用法见 README「第 1 步：抓取登录 Cookie」。

用法:
    pip install mitmproxy
    mitmdump -s capture_cookie.py -p 8080 --set block_global=false

手机微信打开讲座页面后，终端会弹出醒目提示，config.json 自动更新。
"""
import json
import os

from mitmproxy import http

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
EXAMPLE = os.path.join(HERE, "config.example.json")

# 上一次写入的值：值没变化就不再重写文件、不再重复打印提示
_last = {}


def _load_config():
    """读 config.json；不存在则用 config.example.json 做底稿。返回 (配置, 是否新建)。"""
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                return json.load(f), False
        except Exception:
            pass
    if os.path.exists(EXAMPLE):
        try:
            with open(EXAMPLE, encoding="utf-8") as f:
                return json.load(f), True
        except Exception:
            pass
    return {}, True


def _write_config(cfg):
    """先写临时文件再原子替换，避免中途崩溃留下半截 config.json。"""
    tmp = CONFIG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONFIG)


def _prompt(sid, wid, created):
    line = "=" * 60
    print(line, flush=True)
    print("[CAPTURE] 已抓到登录 Cookie，并自动写入 config.json", flush=True)
    print(f"          文件: {CONFIG}", flush=True)
    print(f"          PG.ASP.Session.ID    = {sid}", flush=True)
    if wid:
        print(f"          PG.ASP.WeiXinCode.ID = {wid}", flush=True)
    if created:
        print("          注意: config.json 是刚新建的（来自 config.example.json），", flush=True)
        print("                请补全 email 等配置后再运行 python main.py。", flush=True)
        print("                详见 README「第 2 步：填写配置」。", flush=True)
    else:
        print("          无需手动复制，直接运行 python main.py 即可。", flush=True)
    print(line, flush=True)


def _save(sid, wid):
    if not sid:
        return
    if _last.get("sid") == sid and (not wid or _last.get("wid") == wid):
        return
    cfg, created = _load_config()
    session = cfg.setdefault("session", {})
    session["pg_asp_session_id"] = sid
    if wid:
        session["pg_asp_weixin_code_id"] = wid
    _write_config(cfg)
    _last["sid"], _last["wid"] = sid, wid
    _prompt(sid, wid, created)


def _parse(header):
    if not header:
        return None, None
    sid = wid = None
    for part in header.split(";"):
        p = part.strip()
        if p.startswith("PG.ASP.Session.ID="):
            sid = p[len("PG.ASP.Session.ID="):]
        elif p.startswith("PG.ASP.WeiXinCode.ID="):
            wid = p[len("PG.ASP.WeiXinCode.ID="):]
    return sid, wid


def request(flow: http.HTTPFlow):
    if flow.request.host and "lecture.tongji.edu.cn" in flow.request.host:
        sid, wid = _parse(flow.request.headers.get("Cookie", ""))
        if sid:
            _save(sid, wid)


def response(flow: http.HTTPFlow):
    if flow.request.host and "lecture.tongji.edu.cn" in flow.request.host:
        for value in flow.response.headers.get_all("Set-Cookie", []):
            nv = value.split(";")[0]
            k, _, v = nv.partition("=")
            if k.strip() == "PG.ASP.Session.ID":
                _, wid = _parse(flow.request.headers.get("Cookie", ""))
                _save(v.strip(), wid)
