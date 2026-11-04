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


def _mask(v, keep=4):
    """只露头尾几位，避免 token 明文留在终端/截图里。"""
    return v if len(v) <= keep * 2 else f"{v[:keep]}…{v[-keep:]}"


def _prompt(sid, wid, created, token=""):
    line = "=" * 60
    print(line, flush=True)
    print("[CAPTURE] 已抓到登录 Cookie，并自动写入 config.json", flush=True)
    print(f"          文件: {CONFIG}", flush=True)
    print(f"          PG.ASP.Session.ID    = {sid}", flush=True)
    if wid:
        print(f"          PG.ASP.WeiXinCode.ID = {wid}", flush=True)
    if token:
        print(f"          token                = {_mask(token)}（已保存，新版接口需要）", flush=True)
    if created:
        print("          注意: config.json 是刚新建的（来自 config.example.json），", flush=True)
        print("                请补全 email 等配置后再运行 python main.py。", flush=True)
        print("                详见 README「第 2 步：填写配置」。", flush=True)
    else:
        print("          无需手动复制，直接运行 python main.py 即可。", flush=True)
    print(line, flush=True)


def _save(sid, wid, token="", auth=""):
    if not sid:
        return
    changed = (_last.get("sid") != sid
               or (wid and _last.get("wid") != wid)
               or (token and _last.get("token") != token)
               or (auth and _last.get("auth") != auth))
    if not changed:
        return
    cfg, created = _load_config()
    session = cfg.setdefault("session", {})
    session["pg_asp_session_id"] = sid
    if wid:
        session["pg_asp_weixin_code_id"] = wid
    if token:
        session["token"] = token
    if auth:
        cfg["authorization"] = auth   # 站点会轮换这个公开的 Basic 值，顺手更新
    _write_config(cfg)
    _last.update(sid=sid, wid=wid, token=token, auth=auth)
    _prompt(sid, wid, created, token)


def _parse(header):
    if not header:
        return None, None, ""
    sid = wid = token = None
    for part in header.split(";"):
        p = part.strip()
        if p.startswith("PG.ASP.Session.ID="):
            sid = p[len("PG.ASP.Session.ID="):]
        elif p.startswith("PG.ASP.WeiXinCode.ID="):
            wid = p[len("PG.ASP.WeiXinCode.ID="):]
        elif p.startswith("token="):
            token = p[len("token="):]
    return sid, wid, token or ""


def request(flow: http.HTTPFlow):
    if flow.request.host and "lecture.tongji.edu.cn" in flow.request.host:
        sid, wid, token = _parse(flow.request.headers.get("Cookie", ""))
        # 新版前端把登录态放在请求头 token 里（cookie 里也可能有一份）
        token = flow.request.headers.get("token", "") or token
        auth = flow.request.headers.get("Authorization", "")
        if sid:
            _save(sid, wid, token, auth)


def response(flow: http.HTTPFlow):
    if flow.request.host and "lecture.tongji.edu.cn" in flow.request.host:
        sid, wid, token = _parse(flow.request.headers.get("Cookie", ""))
        token = flow.request.headers.get("token", "") or token
        auth = flow.request.headers.get("Authorization", "")
        for value in flow.response.headers.get_all("Set-Cookie", []):
            k, _, v = value.split(";")[0].partition("=")
            k, v = k.strip(), v.strip()
            if not v:
                continue
            if k == "PG.ASP.Session.ID":
                sid = v
            elif k == "PG.ASP.WeiXinCode.ID":
                wid = v
            elif k == "token":
                token = v
        _save(sid, wid, token, auth)
