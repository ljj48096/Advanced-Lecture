# 同济高等讲堂 自动监控抢课系统

本地常驻运行的讲座监控 + 自动抢课脚本。按 **星期 + 校区 + 级别 + 报名资格** 筛选讲座，到达报名开放时间后自动抢课；**只有抢课成功才发邮件**。抢到一门就停止监控该门、继续监控其余课程。

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
| `SetCredentials` | 领取报名凭证码 `code`（抢课前必须先领）；也用于探测报名资格（失败=无资格） |
| `SginInLectureHallCredentials` | 提交报名（抢课） |
| `GetSginInTime` | 查询报名结果（`state`: 1=成功 -1=失败） |
| `GetIsFinishFace` | 是否已完成人脸采集（报名前置条件） |

**抢课以服务器时钟为准**：接口响应里带 `nowtime`，客户端据此自动校时，不信任本机时间。

**网络容错**：单次请求超时 10 秒；超时/TLS 握手失败/5xx 会退避重试（0.5s、1.0s），4xx 与 401/403 不重试（后者直接判定会话失效）。请求默认**直连**，不受系统代理影响。

---

## 文件结构

```
.
├── main.py             # 入口：python main.py 启动监控抢课
├── monitor.py          # 监控主循环：轮询 → 筛选/资格探测 → 自动抢课 → 成功邮件
├── gdjt_client.py      # API 客户端（纯标准库，含服务器校时）
├── db.py               # SQLite 持久化（已抢/终态失败状态，重启不丢）
├── mailer.py           # SMTP 邮件（抢课成功通知）
├── selftest.py         # 链路自检（python selftest.py）
├── capture_cookie.py   # mitmproxy 插件（抓 Cookie 并自动写入 config.json）
├── config.json         # 配置文件（Cookie / 星期 / 校区 / 级别 / 邮箱，gitignored）
├── config.example.json # 配置模板（不含真实凭据）
├── course_status.db    # 运行后自动生成，状态库
├── monitor.log         # 运行后自动生成，日志
└── 方案.md             # 原始设计文档
```

> 运行主程序**无需安装任何第三方依赖**，只用 Python 标准库。只有「抓 Cookie」那一步需要临时装 mitmproxy。

---

## 环境准备

- Windows / macOS / Linux，Python **3.9+**（用 `python --version` 确认）。
- 一个能收发邮件的邮箱（用于接收抢课成功通知，推荐 163/QQ）。
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

### 1.6 取 Cookie（自动写入，无需手动复制）

回到电脑终端，mitmdump 会弹出醒目提示：

```
============================================================
[CAPTURE] 已抓到登录 Cookie，并自动写入 config.json
          文件: D:\...\Advanced-Lecture\config.json
          PG.ASP.Session.ID    = xxxxxxxx-xxxx-...
          PG.ASP.WeiXinCode.ID = yyyyyyyy...
          无需手动复制，直接运行 python main.py 即可。
============================================================
```

`capture_cookie.py` 会把两个 Cookie **自动写进 `config.json` 的 `session` 字段**，不用手动复制粘贴：

- 如果 `config.json` 还不存在，会以 `config.example.json` 为模板**自动创建**，此时提示里会多一行，提醒你补全 `email` 等配置（见第 2 步）；
- 如果 `config.json` 已存在，只更新 `session` 两个字段，其他配置原样保留；
- 值没变化不会重复写文件、重复刷提示。

### 1.7 清理（可选但建议）

抓完 Cookie 后：
1. 手机：关掉 WiFi 代理、删除 mitmproxy 描述文件、关掉证书信任、断开热点重连校园网。
2. 电脑：关闭移动热点。
3. 卸载 mitmproxy：`pip uninstall -y mitmproxy`。

---

## 第 2 步：填写配置

先把仓库里的配置模板复制（或改名）为正式配置：

```bash
cp config.example.json config.json      # Windows PowerShell: Copy-Item config.example.json config.json
```

> `config.json` 已写进 `.gitignore`，**不会被提交到 Git**，可以放心填自己的 Cookie 和邮箱授权码。
>
> 如果第 1 步跑过 `capture_cookie.py`，`config.json` 可能已被自动创建、Cookie 也已自动填好，那就跳过上面的复制，直接补全 `email` 即可。

然后编辑 `config.json`：

```jsonc
{
  "session": {
    "pg_asp_session_id": "第1步抓到的 PG.ASP.Session.ID",
    "pg_asp_weixin_code_id": "第1步抓到的 PG.ASP.WeiXinCode.ID"
  },
  "authorization": "Basic ...（固定值，不用改）",
  "poll_interval": 5,              // 轮询间隔（秒）
  "poll_jitter": {                 // 每次轮询额外加的随机抖动（秒）；默认开启 1~3 秒
    "enabled": true,               // false = 关闭抖动，严格按 poll_interval 轮询
    "min": 1,                      // 抖动下限（含）
    "max": 3                       // 抖动上限（含）；可写成 5/10 等任意范围
  },
  "grab_retry_interval": 0.5,      // 抢课时每次提交报名请求的间隔（秒）：每 0.5s 发一次，直到没余位
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

**轮询节奏与抖动**：实际每轮间隔 = `poll_interval` + `poll_jitter` 范围内的随机秒数（默认 1~3 秒）。加抖动是为了让请求间隔不呈机械的固定节律（更像真人刷新，也不容易踩到服务端的固定采样点）。想让节奏完全固定就设 `"enabled": false`；想抖得更散就调大 `min`/`max`。`min` 和 `max` 写反了程序会自动对调，`max` ≤ 0 等同关闭。

**抢课提交间隔**：扫描频率（`poll_interval`）和抢课频率是两回事。一旦某门课进入抢课流程，抢课线程会**每 `grab_retry_interval` 秒（默认 0.5 秒）发一次报名请求**，连续不断地发，直到抢到、返回「已满」或判定终态失败为止；单个线程最多连续抢 180 秒，没抢到就交给下一轮扫描重新起线程（不做冷却）。所以扫描间隔设多大都不影响抢课的手速。

---

## 第 3 步：运行

**先自检（可选但推荐）**：
```bash
python selftest.py
```
会检查会话/人脸/各接口是否可用，确认无误再开抢。

**启动主程序**：
```bash
python main.py
```

启动后若 Cookie 有效，打印：
```
✓ 会话有效，服务器时钟偏差 0.031s
开始监控: target_weekdays=all, target_campus=四平路校区, class_level_limit={'校级': 2, '院级': 5}, 轮询间隔=5s(+随机1~3s)
```

> 末尾的 `(+随机1~3s)` 来自配置 `poll_jitter`；关掉抖动后这里会显示 `(无抖动)`。

然后进入常驻监控，每轮（间隔 + `poll_jitter` 抖动，默认 1~3 秒）会：
- 打印状态统计：`本轮扫描: 有余位=0 未开启=2 已报满=5 可抢=3 抢课中=0 已过滤=8(条件6/无资格2)`
- 等待期间终端显示转圈 `监控运行中 | / - \`，表示程序仍在运行
- 出现【星期匹配 + 校区匹配 + 级别匹配 + 有报名资格 + 有余量】的课 → 到达报名开放时间前 3 分钟自动进入抢课流程；**有余位时每轮扫描都会重新尝试抢**（不做冷却）
- 抢课线程会每 0.5 秒发一次报名请求（`grab_retry_interval`），连续抢到没余位为止；扫描本身仍按 `poll_interval` 正常轮询
- 抢到一门 → 发一封「抢课成功」邮件，停止监控该门，继续监控其余（直到各级别限抢数量到顶）
- 提交时发现已满 → 当作「没有余位」的课继续监控，等有人退课释放名额后再抢

**为什么「显示有余位」却没去抢？** 程序不会静默跳过，跳过时终端会打印原因（同一原因只提示一次，不刷屏）：

```
[校级] 某讲座 跳过：校级已抢到 2/2 门，达到 class_level_limit 上限（改 config 可放开）
[校级] 某讲座 跳过：之前已判定终态失败，不再重试（…）
[院级] 某讲座 跳过：列表显示没有剩余名额（Capacity=100, SignupNum=100）
```

真正开始抢时也会明确提示，状态行里的 `抢课中=N` 是当前正在抢的课程数：

```
🎯 开始抢课：[院级] 某讲座（剩余 3 个名额，每 0.5s 提交一次）
```

抢到后终端会响一声并打出横幅：

```
========================================================
🎉 抢课成功：[院级] 某讲座
   星期：周二   报名时间：2026-10-29 12:00:01
   地点：四平路校区逸夫楼1楼报告厅
   继续监控其他符合条件课程
========================================================
级别限抢进度: 校级 1/2, 院级 3/5
```

> 状态行里的所有数量都是**过滤后**口径：不符合筛选条件、或 `SetCredentials` 判定为无报名资格的课程不会计入，只在末尾的 `已过滤` 里体现。

> 终端日志会用颜色区分重点：`抢课成功`/`会话有效` 为绿色，`失败`/`异常`/`失效` 为红色，`已报满`/`已满` 为黄色；`WARNING`/`ERROR` 级别标签也分别标黄/标红。日志文件 `monitor.log` 不含颜色控制符。设置环境变量 `NO_COLOR=1` 可关闭颜色。

**Cookie 失效/缺失时**：程序启动会打印醒目的 `!!!` 警告横幅，提示重新抓 Cookie，并直接退出。

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

> 列表/详情接口没有「报名对象学院」字段（`CollegeCodeName` / `Unit` 是主讲人单位），所以报名资格只能通过 `SetCredentials` 的返回结果判定：失败表示无资格，成功但 `code` 为空多半表示已满。

### 课程选择与过滤统计

每轮遍历 status=1/3/2 的全部课程（可抢的 1/3 优先探测），先做本地筛选，再用 `SetCredentials` 探测资格：

1. 星期 / 校区 / 级别不匹配 → 计入「条件过滤」，不进入统计；
2. `SetCredentials` 明确失败（例如不是目标学院、无报名资格）→ 计入「无资格过滤」，不进入统计，也不会发起抢课；
3. `SetCredentials` 成功但没有 `code` → 多半是已满，保留在统计里，但真正抢课时拿不到凭证会被跳过；
4. 临时性错误（频繁、超时等）不计入过滤，下一轮重试。

资格探测结果会缓存：有资格永久缓存；无资格 **30 分钟**、无 `code` **5 分钟**后重新探测。每轮最多新增 **50 次**探测，避免首轮一次性发太多 POST；未探测到的课程先按有资格统计，后续轮次继续补探。

### 抢课流程（每门课）

1. `GetLectureHall` 查详情：已报名过则跳过；已报满则本轮放弃，但**下一轮仍会再试**——不做任何冷却。列表接口有缓存延迟（名额被抢光后仍显示「有余位」），所以同一门课可能连续多轮都走到这里，日志里同一原因只提示一次，不会刷屏；
2. `SetCredentials` 领取报名凭证码 `code`；
3. 用**服务器时钟**精确等到 `TimeSigningUp`；
4. `SginInLectureHallCredentials(code)` 提交报名（带重试；资格/人脸等终态错误及时止损。若返回「已满」，仅表示名额被抢光，**不标记失败**，当作没有余位的课继续监控）；
5. `GetSginInTime` 轮询确认 `state==1` 成功。

### 去重与持久化

- SQLite（`course_status.db`）记录每门课的 `grabbed`（1=已抢到 / -1 终态失败 / 0=未处理），程序重启不丢，避免重复抢。`notified` 字段为旧版遗留，现已不再使用。
- `class_level_limit` 是按 `grabbed=1` 的行数**累计（跨次运行）**计算的：抢到 2 门校级后再启动，程序仍认为校级已满、不再抢校级课，并会打印 `级别限抢进度` 和跳过原因。想重新计数就删掉这些行（见下）。

---

## 维护与常见问题

| 现象 | 处理 |
| --- | --- |
| 启动弹 `!!!` 警告「Cookie 无效/过期/缺失」 | 重新走一遍「第 1 步」抓 Cookie，或先 `python selftest.py` 自检 |
| 运行中报「会话失效」退出 | 同上，更新 `config.json` 后重启 |
| 日志里偶尔出现 `本轮异常(继续下一轮): <urlopen error timed out>` | 网络抖动，请求在 10 秒内没连上（`_ssl.c:1112: The handshake operation timed out` 是 TLS 握手超时）。程序已自动**退避重试 2 次**（0.5s、1.0s），仍失败才跳过本轮，下一轮自动恢复，无需干预。若连续失败 5 轮会弹警告横幅——检查网络，或关掉 Clash/梯子的「全局/TUN」模式（程序默认**直连**，不读 `HTTP(S)_PROXY` 环境变量；确需走代理时启动前设 `GDJT_USE_ENV_PROXY=1`） |
| 邮件发不出去 | 检查 `sender_pwd` 是否为**授权码**（非登录密码）；确认已开启 SMTP |
| 未完成人脸采集 | 程序会拒绝启动并提示，先去微信里完成采集 |
| 抢课没抢到 | 名额可能瞬间被抢空；程序不冷却，每轮扫描都会重试。以「我的报名」为准 |
| 电脑休眠导致监控停 | 关闭电脑自动睡眠；保持终端窗口运行 |
| 明明有余位却不抢，终端说「已达到 class_level_limit 上限」 | 级别限抢是**累计**的（含以前抢到的课）。要么调大 `class_level_limit`（如校级 `4`），要么清掉旧记录：见下方「重置已抢/失败记录」 |
| 终端说「之前已判定终态失败，不再重试」 | 该课曾返回真正的终态错误（已报名/无资格/人脸未采集等），已写入 `course_status.db`（`grabbed=-1`）。确认要重试就删掉那一行 |

### 重置已抢/失败记录

`class_level_limit` 计数和「终态失败」都来自 `course_status.db`，直接删行即可（先 `Ctrl+C` 停掉程序）：

```bash
# 看当前记录（1=已抢到 -1=终态失败 0=未处理）
python -c "import sqlite3;c=sqlite3.connect('course_status.db');[print(r) for r in c.execute('select course_name,class_level,grabbed,grab_time from course_record')]"

# 只清「已抢到」的计数（保留失败记录）
python -c "import sqlite3;c=sqlite3.connect('course_status.db');c.execute('delete from course_record where grabbed=1');c.commit()"

# 清掉某门课的终态失败，让程序重新尝试
python -c "import sqlite3;c=sqlite3.connect('course_status.db');c.execute(\"delete from course_record where course_name like '%课程名关键字%'\");c.commit()"
```

想从零开始也可以直接删掉整个 `course_status.db`（下次启动自动重建，但所有已抢/失败记录都会丢失）。

### 收窄筛选范围

- 只想抢某几天的课：`"target_weekdays": [2,4,5]`（周二四、五）。
- 只想抢某校区：`"target_campus": "四平路校区"`；不限校区：`"target_campus": ""`。
- 想按级别限抢：`"class_level_limit": {"校级": 2, "院级": 5}`（校级最多抢 2 门、院级最多抢 5 门）；`{}` = 不限级别不限数量。

### 调整轮询节奏

- 抢课靠的是「先到先得」，轮询越密越容易抢到，但请求过密可能被服务端限流。秒空的课建议 `"poll_interval": 3~5`。
- 抖动（`poll_jitter`）默认开启 1~3 秒，让每次请求的间隔不固定；不需要就设 `"enabled": false`。
- 喜欢更分散的节奏：`"poll_jitter": {"enabled": true, "min": 2, "max": 8}`，实际间隔 = `poll_interval` + 2~8 秒。
- 抢课提交间隔由 `"grab_retry_interval"` 控制（默认 0.5 秒）。想更激进可设 `0.2`，但请求量会成倍上升；嫌太猛就调到 `1~2`。
- 这个间隔只影响「已经在抢的那门课」；扫描其他课程的节奏仍是 `poll_interval`，两者互不影响。

### 关于「校区」匹配的说明

`target_campus` 是对 `Venue` 字段做**子串包含**匹配。标准写法有 `四平路校区` / `嘉定校区` / `沪西校区` 等。少数地点不带校区前缀（如「新能源汽车工程中心二楼报告厅」，实为嘉定），这类不会被 `四平路校区` 匹配到——这是预期行为（宁缺勿错）。
