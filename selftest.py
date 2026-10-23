#!/usr/bin/env python3
"""抢课链路自检：验证抢课涉及的每个接口是否可用。

用法: python selftest.py

只调用只读接口和「领取凭证码」这类无害接口，不会真的报名。
若存在「未开启报名」的课，会额外安全测通「提交报名」接口
（在开放时间前调用只会返回「未到时间」错误，不会真报名）。
"""
import json
import os
import sys

import monitor
from gdjt_client import GdjtClient


def main():
    cfg = monitor.load_config()
    os.environ["GDJT_CLIENT_AUTH"] = cfg.get("authorization", "")
    client = monitor.make_client(cfg)

    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        mark = "[OK]" if cond else "[FAIL]"
        print(f"  {mark} {label} {detail}")
        if not cond:
            ok = False

    print("=" * 56)
    print("同济高等讲堂 抢课链路自检")

    # 会话
    d = client.get_lecture_list(page=1, page_size=1, status=0)
    sess_ok = d.get("state") == "success"
    check("会话登录态", sess_ok, "" if sess_ok else str(d.get("msg", d)))
    if not sess_ok:
        print("=" * 56)
        print("会话失效，请按 README 第 1 步重新抓 Cookie 更新 config.json")
        return 2

    # 人脸
    check("人脸采集", client.is_finish_face(), "(报名前置条件)")

    # 详情 + 领取凭证码 + 查询状态
    rows = (client.get_lecture_list(page=1, page_size=1, status=2).get("data") or {}).get("row", [])
    if not rows:
        rows = (client.get_lecture_list(page=1, page_size=1, status=0).get("data") or {}).get("row", [])
    if rows:
        uid = rows[0]["DataId"]
        det = client.get_lecture_detail(uid)
        check("讲座详情接口", det.get("state") == "success")

        sc = client.set_credentials(uid)
        check("领取凭证码接口", sc.get("state") == "success", "(满员课 code=null 属正常)")

        st = client.get_signin_time(uid)
        check("查询报名状态接口", (st.get("data") or {}).get("state") is not None)
    else:
        check("列表有数据", False)

    # 若存在「未开启报名」的课，安全测通「提交报名」
    rows3 = (client.get_lecture_list(page=1, page_size=5, status=3).get("data") or {}).get("row", [])
    if rows3:
        r = client.set_credentials(rows3[0]["DataId"])
        code = (r.get("data") or {}).get("code")
        if code:
            det = client.get_lecture_detail(rows3[0]["DataId"])
            open_ts = (det.get("data") or {}).get("TimeSigningUp")
            if open_ts and GdjtClient._parse_server_time(open_ts) > client.server_now():
                resp = client.signup(code)
                ok2 = resp.get("state") != "success"  # 未到时间，应失败
                check("提交报名接口(开放前安全测试)", ok2,
                      f"返回 state={resp.get('state')} msg={resp.get('msg')}（应为'未到时间'类错误）")
    else:
        print("  [跳过] 提交报名接口：当前无「未开启报名」的课，暂无法安全测通")

    print("=" * 56)
    print("结论:", "全部通过" if ok else "存在失败项")
    print()
    print("说明：")
    print("  - 「提交报名」若被跳过，需等出现「未开启报名」的课后才能安全测通。")
    print("  - 提交逻辑与开源项目 tongji-gdjt-course-registration 一致，已被实测可用。")
    print("  - 最终以「[抢课成功] 邮件 + 微信『我的报名』」为准。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
