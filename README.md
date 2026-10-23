# 同济高等讲堂 自动监控抢课系统

本地常驻运行的讲座监控 + 自动抢课脚本。按 **星期 + 校区** 筛选讲座，检测到有余位时发邮件通知（每门只发一次），到达报名开放时间后自动抢课，抢到一门就停止监控该门、继续监控其余课程。

> ⚠️ 风险声明：本项目仅用于 Python 技术学习研究。若教务/选课平台禁止自动化访问、爬虫、模拟请求，使用本方案存在账号受限风险，请遵守平台管理规定，勿对接口施加异常流量。

---

## 目录

- [原理](#原理)
- [文件结构](#文件结构)
- [环境准备](#环境准备)
- [第 1 步：抓取登录 Cookie](#第-1-步抓取登录-cookie)
- [第 2 步：填写配置](#第-2-步填写配置)
- [第 3 步：运行](#第-3-步运行)
- [工作原理](#工作原理)
- [维护与常见问题](#维护与常见问题)

---

## 原理

同济高等讲堂是微信 H5 应用，后端是一组 HTTP API（`https://lecture.tongji.edu.cn/api/...`）。本项目**不模拟浏览器**，直接调用这些 API——比 Playwright/Selenium 更轻、更快、可无界面挂机。

**登录态**由两个 Cookie 构成，抓取方式见下文：
- `PG.ASP.Session.ID`（主会话，必需）
- `PG.ASP.WeiXinCode.ID`（微信码，可选）

此外还有一个**应用级请求头**（对所有请求都带）：
```
Authorization: Basic cTN4YXBwbTV0eHc0MmtoejVjYzg1YWY0OmI1dTRteWVrZDVkYno0cGUxcTFoY3dnb3h4eXR0bTM3
```
它来自前端 `https://lecture.tongji.edu.cn/static/js/config.js`，是写死的公共值，非个人凭证。

**核心接口**（`gdjt_client.py` 已封装）：

| 接口 | 作用 |
| --- | --- |
| `GetLectureHallList` | 讲座列表（`status`: 1=有余位 2=已报满 3=未开启 4=已结束） |
| `GetLectureHall` | 讲座详情（含报名开放时间 `TimeSigningUp`） |
| `SetCredentials` | 领取报名凭证码 `code`（抢课前必须先领） |
| `SginInLectureHallCredentials` | 提交报名（抢课） |
| `GetSginInTime` | 查询报名结果（`state`: 1=成功 -1=失败） |
| `GetIsFinishFace` | 是否已完成人脸采集（报名前置条件） |

**抢课以服务器时钟为准**：接口响应里带 `nowtime`，客户端据此自动校时，不信任本机时间。

---

## 文件结构

```
.
├── monitor.py          # 主程序：轮询 → 筛选 → 邮件通知 → 自动抢课
├── gdjt_client.py      # API 客户端（纯标准库，含服务器校时）
├── db.py               # SQLite 持久化（已通知/已抢状态，重启不丢）
├── mailer.py           # SMTP 邮件通知
├── capture_cookie.py   # mitmproxy 插件（仅抓 Cookie 时用）
├── config.json         # 配置文件（Cookie / 星期 / 校区 / 邮箱 / 轮询间隔）
├── course_status.db    # 运行后自动生成，状态库
├── monitor.log         # 运行后自动生成，日志
└── 方案.md             # 原始设计文档
```

> 运行主程序**无需安装任何第三方依赖**，只用 Python 标准库。只有「抓 Cookie」那一步需要临时装 mitmproxy。

---

## 环境准备

- Windows / macOS / Linux，Python **3.9+**（用 `python --version` 确认）。
- 一个能收发邮件的邮箱（用于接收通知，推荐 163/QQ）。
- 一台手机（装微信，用于登录抓 Cookie）。
- 报名前需已在微信里完成**人脸采集**。

---

## 第 1 步：抓取登录 Cookie

这是唯一有难度的步骤。因为该系统的微信 OAuth 只认**真实微信客户端**——用电脑浏览器打开只会看到「请在微信客户端打开链接」，无法在浏览器里登录。所以需要用 mitmproxy 抓取手机微信的流量，拿到登录 Cookie。

全程约 10 分钟。分两条路，**优先用「电脑开热点」**（校园网通常做了设备隔离，手机直接连校园网是连不到电脑的）。

### 1.1 电脑开热点（绕开校园网设备隔离）

1. Windows：`设置 → 网络和 Internet → 移动热点`，打开开关，共享来源选 **WLAN**。
2. 记下热点名称和密码，手机连上这个热点。

> 电脑开热点后，手机网关默认是 `192.168.137.1`（Windows 热点固定值）。

### 1.2 电脑装并启动 mitmproxy

```bash
pip install mitmproxy
mitmdump -s capture_cookie.py -p 8080 --set block_global=false
```

保持这个终端窗口开着，它会一直监听 8080 端口。

### 1.3 手机设 HTTP 代理

iPhone：`设置 → 无线局域网 → 点电脑热点右边的 ⓘ → 配置代理 → 手动`：

- 服务器：`192.168.137.1`
- 端口：`8080`

### 1.4 手机装并信任 mitmproxy 证书

1. 手机 Safari 打开 `http://mitm.it`（此时能看到 mitmproxy 页面即说明代理已通）。
2. 点 **Apple** 那栏下载证书 → 按提示安装描述文件：
   - `设置 → 通用 → VPN与设备管理` → 安装 mitmproxy 描述文件；
   - `设置 → 通用 → 关于本机 → 证书信任设置` → 打开 mitmproxy 的**完整信任**开关（这步最容易漏）。

### 1.5 微信登录（关键一步）

1. **彻底退出微信再重新打开**（上滑强制杀掉，不是按 Home 返回）。
   - ⚠️ 这是关键：iOS 上微信的网页（WKWebView）会绕过系统 HTTP 代理，必须**重开微信后**，其网页流量才会走代理。
2. 微信里把 `https://lecture.tongji.edu.cn/wxpage/index` 发到「文件传输助手」→ 点开。
3. 弹「授权」点允许，进入讲座列表页。

### 1.6 取 Cookie

回到电脑终端，mitmdump 会打印出：

```
[CAPTURE] {"PG.ASP.Session.ID": "xxxx", "PG.ASP.WeiXinCode.ID": "yyyy"}
```

同时项目目录会生成 `captured_cookie.json`。把这两个值填进 `config.json` 的 `session` 字段即可。

### 1.7 清理（可选但建议）

抓完 Cookie 后：
1. 手机：关掉 WiFi 代理、删除 mitmproxy 描述文件、关掉证书信任、断开热点重连校园网。
2. 电脑：关闭移动热点。
3. 卸载 mitmproxy：`pip uninstall -y mitmproxy`。

---

## 第 2 步：填写配置

编辑 `config.json`：

```jsonc
{
  "session": {
    "pg_asp_session_id": "第1步抓到的 PG.ASP.Session.ID",
    "pg_asp_weixin_code_id": "第1步抓到的 PG.ASP.WeiXinCode.ID"
  },
  "authorization": "Basic ...（固定值，不用改）",
  "poll_interval": 5,              // 轮询间隔（秒），实际每次会再加 1~3 秒随机抖动
  "target_weekdays": "all",        // 目标星期："all" 或 [1,3,5]（1=周一…7=周日）
  "target_campus": "四平路校区",    // 目标校区：Venue 含此关键词即匹配；留空 "" = 不限校区
  "class_level_limit": {           // 级别限抢：键=级别(校级/院级)，值=该级别最多抢几门；{} = 不限
    "校级": 2,
    "院级": 5
  },
  "grab_prepare_lead_seconds": 180, // 提前多少秒进入抢课准备（默认 180 = 3 分钟）
  "email": {
    "smtp_server": "smtp.163.com",  // 163 邮箱；QQ 用 smtp.qq.com
    "smtp_port": 465,               // SSL 端口
    "sender": "你的发件邮箱",
    "sender_pwd": "邮箱授权码（不是登录密码！）",
    "receiver": "收件邮箱"
  },
  "log_path": "./monitor.log"
}
```

**邮箱授权码**怎么拿（以 163 为例）：
登录 163 邮箱网页版 → `设置 → POP3/SMTP/IMAP` → 开启「SMTP 服务」→ 按提示生成授权码，填到 `sender_pwd`。

---

## 第 3 步：运行

```bash
python monitor.py
```

启动后会打印：

```
✓ 会话有效，服务器时钟偏差 0.001s
开始监控: target_weekdays=all, target_campus=四平路校区, 轮询间隔=5s
```

然后进入常驻监控，每 5 秒轮询一次：
- 出现【星期匹配 + 校区匹配 + 有余量】的课 → 发邮件通知（每门只发一次）；
- 到达报名开放时间前 3 分钟 → 自动进入抢课流程；
- 抢到一门 → 停止监控该门，继续监控其余。

停止：终端按 `Ctrl+C`。

---

## 工作原理

### 字段映射（逆向所得）

讲座列表接口返回的每个 `row` 关键字段：

| 字段 | 含义 |
| --- | --- |
| `DataId` | 课程唯一 ID（主键去重用） |
| `Cathedra` | 讲座标题 |
| `Venue` | 地点（校区藏在里面，如「四平路校区逸夫楼1楼报告厅」） |
| `LectureTimeStart` / `LectureTimeEnd` | 上课起止时间，**星期由此算**（`weekday()+1`） |
| `TimeSigningUp` | 报名开放时间（抢课起点） |
| `Capacity` | 容量 |
| `SignupNum` | 已报名人数，**剩余名额 = Capacity - SignupNum** |
| `LectureHallStatus` | 状态：1=有余位 2=已报满 3=未开启 4=已结束 |
| `ClassLevel` / `ClassLevelName` | 级别：`校级` / `院级`（用于级别限抢） |

### 抢课流程（每门课）

1. `GetLectureHall` 查详情：已报名过则跳过，已报满则跳过；
2. `SetCredentials` 领取报名凭证码 `code`；
3. 用**服务器时钟**精确等到 `TimeSigningUp`；
4. `SginInLectureHallCredentials(code)` 提交报名（带重试，识别满员/资格等终态错误及时止损）；
5. `GetSginInTime` 轮询确认 `state==1` 成功。

### 去重与持久化

- SQLite（`course_status.db`）记录每门课的 `notified`（已通知）和 `grabbed`（已抢到）状态，程序重启不丢，避免重复发邮件、重复抢。

---

## 维护与常见问题

| 现象 | 处理 |
| --- | --- |
| 启动报「登录态无效 / 未登录」 | Cookie 失效了，重新走一遍「第 1 步」抓 Cookie |
| 运行中报「会话失效」退出 | 同上，更新 `config.json` 后重启 |
| 邮件发不出去 | 检查 `sender_pwd` 是否为**授权码**（非登录密码）；确认已开启 SMTP |
| 未完成人脸采集 | 程序会拒绝启动并提示，先去微信里完成采集 |
| 抢课没抢到 | 名额可能瞬间被抢空/触发服务端冷却，程序会记录日志并继续下一门；以「我的报名」为准 |
| 电脑休眠导致监控停 | 关闭电脑自动睡眠；保持终端窗口运行 |

### 收窄筛选范围

- 只想抢某几天的课：`"target_weekdays": [2,4,5]`（周二四、五）。
- 只想抢某校区：`"target_campus": "四平路校区"`；不限校区：`"target_campus": ""`。
- 想按级别限抢：`"class_level_limit": {"校级": 2, "院级": 5}`（校级最多抢 2 门、院级最多抢 5 门）；`{}` = 不限级别不限数量。

### 关于「校区」匹配的说明

`target_campus` 是对 `Venue` 字段做**子串包含**匹配。标准写法有 `四平路校区` / `嘉定校区` / `沪西校区` 等。少数地点不带校区前缀（如「新能源汽车工程中心二楼报告厅」，实为嘉定），这类不会被 `四平路校区` 匹配到——这是预期行为（宁缺勿错）。
