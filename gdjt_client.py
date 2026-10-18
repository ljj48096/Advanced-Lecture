"""
同济高等讲堂 API 客户端
======================
逆向自 lecture.tongji.edu.cn 前端 JS (app.*.js / 0.*.js) + mitmproxy 实测。

认证方式（抓包所得）:
  - Cookie: PG.ASP.Session.ID + PG.ASP.WeiXinCode.ID
    (来源: 微信 OAuth -> weixinapi/Callback -> 同济 IAM OAuth -> loginapi/callback)
  - Header: Authorization 可从环境变量 GDJT_CLIENT_AUTH 提供；源码不保存凭证
  - UA: 要求 MicroMessenger 特征 (实测换成普通 UA 也通, 但稳妥起见保留)

重要: 服务端响应用 Brotli 压缩 (Content-Encoding: br)。
      本客户端不主动声明 br, 只声明 gzip, 服务端会回 gzip, 免去 brotli 依赖。
"""
import json
import time
import urllib.parse
import urllib.request
import gzip
import os

BASE_URL = "https://lecture.tongji.edu.cn"

# 微信内置浏览器 UA (Mac 微信 3.8.4)
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/107.0.0.0 Safari/537.36 NetType/WIFI "
    "MicroMessenger/6.8.0(0x16080000) MacWechat/3.8.4(0x13080414) XWEB/31011"
)


class SessionExpired(Exception):
    """会话失效 (Cookie 过期), 需要重新抓包获取"""


class GdjtClient:
    def __init__(self, session_id: str, weixin_code_id: str = "", ua: str = DEFAULT_UA, timeout: int = 15):
        """
        :param session_id:    Cookie 中的 PG.ASP.Session.ID
        :param weixin_code_id: Cookie 中的 PG.ASP.WeiXinCode.ID (可为空)
        """
        self.session_id = session_id.strip()
        self.weixin_code_id = (weixin_code_id or "").strip()
        self.ua = ua
        self.timeout = timeout
        self.server_offset = 0.0  # 服务器时间 - 本地时间 (秒), 用于校时
        self.last_server_time = None

    # ---------- 底层请求 ----------
    def _cookie_header(self) -> str:
        c = f"PG.ASP.Session.ID={self.session_id}"
        if self.weixin_code_id:
            c += f"; PG.ASP.WeiXinCode.ID={self.weixin_code_id}"
        return c

    def _multipart(self, fields: dict):
        """构造 multipart/form-data, 与前端 FormData 行为一致"""
        boundary = "----pybotboundary"
        body = "".join(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
            for k, v in fields.items()
        ) + f"--{boundary}--\r\n"
        return body.encode(), f"multipart/form-data; boundary={boundary}"

    def _request(self, method: str, path: str, params: dict = None, form: dict = None) -> dict:
        url = BASE_URL + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        data, content_type = (None, None)
        if form is not None:
            data, content_type = self._multipart(form)

        headers = {
            "Cookie": self._cookie_header(),
            "User-Agent": self.ua,
            "Accept": "application/json, text/plain, */*",
            "Accept-Encoding": "gzip",          # 不声明 br, 服务端回 gzip
            "Origin": BASE_URL,
            "Referer": BASE_URL + "/",
        }
        client_auth = os.environ.get("GDJT_CLIENT_AUTH", "").strip()
        if client_auth:
            headers["Authorization"] = client_auth
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        if content_type:
            req.add_header("Content-Type", content_type)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw.decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = e.read()[:500]
            if e.code in (401, 403):
                raise SessionExpired(f"HTTP {e.code}: {body!r}") from e
            raise

    @staticmethod
    def _parse_server_time(t: str):
        """'2026-09-28T19:43:47.8717089+08:00' -> timestamp"""
        from datetime import datetime
        t = t.strip()
        try:
            dt = datetime.fromisoformat(t)
        except ValueError:
            # 兼容 7 位微秒 (python 最多 6 位)
            dt = datetime.fromisoformat(t[:26] + t[27:] if len(t) > 26 else t)
        return dt.timestamp()

    def _sync_clock(self, server_time_str: str):
        try:
            st = self._parse_server_time(server_time_str)
            self.server_offset = st - time.time()
            self.last_server_time = server_time_str
        except Exception:
            pass

    def server_now(self) -> float:
        """当前服务器时间 (秒级时间戳)。抢课一律以它为基准, 不信本机时钟"""
        return time.time() + self.server_offset

    # ---------- 业务 API ----------
    def get_lecture_list(self, page=1, page_size=10, status=0, time_=0, class_level=0) -> dict:
        """讲座列表. status: 0全部 1有余位 2已报满 3未开启报名 4已结束"""
        d = self._request("GET", "/api/gj/studentwx/lecturehallapi/GetLectureHallList",
                          params={"page": page, "pageSize": page_size,
                                  "status": status, "time": time_, "classLevel": class_level})
        if isinstance(d.get("data"), dict) and d["data"].get("nowtime"):
            self._sync_clock(d["data"]["nowtime"])
        return d

    def get_lecture_detail(self, data_id: str) -> dict:
        """讲座详情 (含 TimeSigningUp 报名开放时间 / XSSigningUp 我的报名时间)"""
        return self._request("GET", "/api/gj/studentwx/lecturehallapi/GetLectureHall",
                             params={"DataId": data_id})

    def set_credentials(self, data_id: str) -> dict:
        """领取报名凭证码 code (进详情页时前端自动调用, 无害, 可提前调用)"""
        d = self._request("POST", "/api/gj/studentwx/studentapi/SetCredentials",
                          form={"dataId": data_id})
        data = d.get("data") or {}
        if data.get("time"):
            self._sync_clock(data["time"])
        return d

    def get_credentials(self, code: str) -> dict:
        """查询凭证码状态. data.iscode 表示凭证是否可用; data.time 顺带校时"""
        d = self._request("POST", "/api/gj/studentwx/studentapi/GetCredentials",
                          form={"code": code})
        data = d.get("data") or {}
        if data.get("time"):
            self._sync_clock(data["time"])
        return d

    def signup(self, code: str) -> dict:
        """★ 立即报名 ★  前端「立即报名」按钮 -> handleSigningUp -> 本接口"""
        return self._request("POST", "/api/gj/studentwx/studentapi/SginInLectureHallCredentials",
                             form={"code": code})

    def get_signin_time(self, data_id: str) -> dict:
        """查询我的报名/签到状态. data.state: 0未报名 1已报名 -1报名失败"""
        d = self._request("POST", "/api/gj/studentwx/studentapi/GetSginInTime",
                          form={"dataId": data_id})
        data = d.get("data") or {}
        if data.get("time"):
            self._sync_clock(data["time"])
        return d

    def cancel(self, data_id: str) -> dict:
        """取消报名"""
        return self._request("POST", "/api/gj/studentwx/studentapi/CancelLectureHall",
                             form={"dataId": data_id})

    def is_finish_face(self) -> bool:
        """是否已完成人脸采集 (报名前置条件)"""
        d = self._request("GET", "/api/gj/studentwx/studentapi/GetIsFinishFace")
        return bool(d.get("data"))

    def get_lecture_num(self) -> dict:
        """我的讲座统计"""
        return self._request("GET", "/api/gj/studentwx/studentapi/GetLectureHallNum")
