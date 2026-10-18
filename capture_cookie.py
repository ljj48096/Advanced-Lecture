"""mitmproxy 插件：抓取 lecture.tongji.edu.cn 请求里的登录 Cookie。

配合 mitmproxy 使用，把手机微信的流量导进来后自动提取登录 Cookie，
写入 captured_cookie.json。用法见 README「第 1 步：抓取登录 Cookie」。

用法:
    pip install mitmproxy
    mitmdump -s capture_cookie.py -p 8080 --set block_global=false
"""
import json
from mitmproxy import http

OUT = "captured_cookie.json"


def _save(sid, wid):
    if not sid:
        return
    data = {"PG.ASP.Session.ID": sid}
    if wid:
        data["PG.ASP.WeiXinCode.ID"] = wid
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[CAPTURE] {json.dumps(data, ensure_ascii=False)}", flush=True)


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
