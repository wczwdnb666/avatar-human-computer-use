# 外部打码平台配置指南（solver）

> **这个功能默认关闭。** 这份文档讲的是"如果你决定打开它，
> 该怎么配、会花多少钱、数据会去哪"。

最后更新：阶段五 5D / 5H（2026-10-01）

---

## 0. 先说清楚：这个功能是什么

`avatar.risk_detector.solver` 把验证题**外包给人** —— 调用第三方
打码平台（2Captcha / CapSolver 等），把图或 `site_key` 发过去，
拿回答案。

它和 5C 的 `guard.pause_for_human` 是**两条并行的路**：

```
guard.pause_for_human    叫**本机前面坐着的那个人**（免费，但要人等着）
solver.solve_*           叫**平台背后的人**（花钱，但不用你等着）
```

选哪条由调用方决定。本模块只提供能力，不替调用方做选择。

---

## 1. ⚠ 默认关闭是刻意的

必须**同时**满足两个条件才会启用：

1. 显式 `enabled=True`（或环境变量 `AVATAR_SOLVER_ENABLED=1`）
2. `api_key` 非空（或环境变量 `AVATAR_SOLVER_API_KEY`）

否则所有 `solve_*` **直接返回 `None`，连一个字节都不发出去**。

为什么定这么死：

| 理由 | 说明 |
|---|---|
| **要花钱** | 2Captcha 大约 $1~3 / 1000 次。误触发就是真金白银往外流 |
| **是隐私外发** | 截图里可能有账号、订单、聊天内容。这是把用户数据发给第三方 |
| **可能违反服务条款** | 很多平台明令禁止自动化打码。默认关闭让"开了"成为一个**明确的人类决定** |
| **不装死** | 没有默认 `api_url`。没配就是没配，不会"顺手用个免费接口" |

**这是"默认安全"而不是"默认好用"。** 如果你觉得不好用，
那是设计意图 —— 它本来就该在你想清楚之后才被打开。

---

## 2. 快速启用

### 2.1 环境变量（推荐）

key 不进代码、不进 git、不进日志：

```bat
:: Windows CMD
set AVATAR_SOLVER_ENABLED=1
set AVATAR_SOLVER_API_KEY=你的key
set AVATAR_SOLVER_API_URL=https://2captcha.com/in.php
```

```bash
# Git Bash / WSL
export AVATAR_SOLVER_ENABLED=1
export AVATAR_SOLVER_API_KEY=你的key
export AVATAR_SOLVER_API_URL=https://2captcha.com/in.php
```

配好后校验（**不发任何请求**）：

```bash
python -m avatar solver --test
```

### 2.2 代码里显式传

```python
from avatar.risk_detector.solver import solve_captcha_via_api

token = solve_captcha_via_api(
    api_url="https://2captcha.com/in.php",
    api_key="你的key",
    image_data=png_bytes,      # bytes / base64 字符串 / 本地路径 都收
    timeout=60,
    enabled=True,              # ← 必须显式打开
)
if token is None:
    # 打码失败 —— 走"叫本机的人"那条路
    ...
```

### 2.3 用配置对象

```python
from avatar.risk_detector.solver import SolverConfig, solve_captcha_detailed

cfg = SolverConfig(
    enabled=True,
    api_url="https://api.capsolver.com/createTask",
    api_key="你的key",
    timeout=90,
)
r = solve_captcha_detailed(config=cfg, image_data=png)
print(r.describe())     # ✓ 打码成功（3.2s，1 次请求）：3AGdBq...
```

---

## 3. 平台配置示例

### 3.1 2Captcha（图片验证码）

```
api_url = https://2captcha.com/in.php
api_key = <你的 key>
编码    = multipart（自动判定）
```

请求体长这样（`build_multipart_body` 生成，可直接单测）：

```
--BOUNDARY
Content-Disposition: form-data; name="key"

<你的 key>
--BOUNDARY
Content-Disposition: form-data; name="method"

base64
--BOUNDARY
Content-Disposition: form-data; name="body"

iVBORw0KGgoAAAANSUhEUg...
--BOUNDARY--
```

**响应格式**（两家都兼容）：

| 情况 | 响应 | 解析结果 |
|---|---|---|
| 成功 | `{"status": 1, "request": "3AGdBq..."}` | `ok=True`, `solution="3AGdBq..."` |
| 纯文本成功 | `OK\|3AGdBq...` | 同上 |
| 还没算完 | `{"status": 0, "request": "CAPCHA_NOT_READY"}` | `reason="not_ready"` |
| key 错 | `{"status": 0, "request": "ERROR_WRONG_USER_KEY"}` | `reason="platform_error:..."` |
| 图太糊 | `ERROR_CAPTCHA_UNSOLVABLE` | 同上 |

### 3.2 CapSolver（图片验证码）

```
api_url = https://api.capsolver.com/createTask
api_key = <你的 key>
编码    = json（自动判定 —— URL 里含 "capsolver"）
```

请求体：

```json
{"key":"<你的 key>","image":"iVBORw0KGgoAAAANSUhEUg..."}
```

**响应格式**：

```json
{"errorId": 0, "solution": {"gRecaptchaResponse": "TOKEN..."}}
{"errorId": 12, "errorCode": "ERROR_ZERO_BALANCE", "errorDescription": "..."}
```

`errorId == 0` 视为成功。`SolveResult.text` 会自动从
`solution` 字典里挑出 token（找 `gRecaptchaResponse` /
`token` / `text` / `answer` / `code` 这几个键）。

### 3.3 2Captcha（reCAPTCHA token）

这个走**两步协议**，用 `solve_recaptcha_token`：

```python
from avatar.risk_detector.solver import solve_recaptcha_token

token = solve_recaptcha_token(
    site_key="6LcXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
    page_url="https://target.example/login",
    api_key="你的key",
    enabled=True,
    timeout=120,
    poll_interval=5.0,      # 每 5 秒问一次
    max_poll=24,            # 最多问 24 次（约 120 秒）
)
```

**流程**：

```
1. POST https://2captcha.com/in.php
      method=userrecaptcha
      googlekey=<site_key>
      pageurl=<page_url>
   → {"status": 1, "request": "TASK_ID"}

2. GET  https://2captcha.com/res.php?key=...&action=get&id=TASK_ID
   → {"status": 0, "request": "CAPCHA_NOT_READY"}     ← 继续等
   → {"status": 1, "request": "TOKEN_ABC..."}         ← 成功
```

**为什么 reCAPTCHA 要 `site_key` 而不是截图**：
reCAPTCHA v2/v3 的解法需要 `site_key` + `pageurl`，平台拿这两个
去官方接口换 token。**截图在这里没用** —— 这是 reCAPTCHA 和
其他验证码最大的区别。

---

## 4. 失败语义（重要）

**所有失败都返回 `None`，不抛异常。**

打码失败是**预期内的常态**：余额不足、图太糊、平台抽风、网络抖动。
把它做成异常，会逼调用方到处写 try，最后一定有人漏写。

```python
token = solve_captcha_via_api(...)
if token is None:
    # 正常的分支，不是异常处理
    guard.pause_for_human("打码失败，叫人", ...)
```

想知道**为什么**失败，用 `solve_captcha_detailed` 或传 `errors` 列表：

```python
errs = []
token = solve_captcha_via_api(..., errors=errs)
if token is None:
    print(errs[0])    # "solver: request_failed: OSError: 网络断了"
```

常见 `reason`：

| reason | 含义 |
|---|---|
| `solver_disabled` | 没开 `enabled`（**默认状态**） |
| `solver_no_api_key` | 开了开关但没配 key |
| `solver_no_api_url` | 没配 URL |
| `empty_image` | 图是空的 |
| `image_not_found` | 路径不存在 |
| `request_failed:*` | 网络/超时/被拒 |
| `platform_error:*` | 平台报错（余额、key 错等） |
| `not_ready` | 还没算完（**不是错误**） |
| `poll_exhausted:*` | 轮询次数用完了 |

---

## 5. 安全注意

1. **api_key 不要进 git**。用环境变量，或用配置对象（`to_dict()`
   默认脱敏，`describe()` 也不含明文）。
2. **截图会离开你的机器**。开启前确认你接受这一点。
   熔断截图默认落在 `~/.avatar/interventions/`（已在 `.gitignore` 里），
   发出去的是同一份内容。
3. **不要把它设成默认路径**。默认该是 `guard.pause_for_human` ——
   免费、无隐私风险、且真的有人在看。打码是"没人在的时候的备选"。
4. **`api_key` 不会出现在日志里**。`SolverConfig.to_dict()` 默认
   脱敏成 `abcd…78`，`describe()` 同样。这是被测试守着的
   （`TestRedaction`）。

---

## 6. 能力边界

**做**：发 HTTP 请求（带 timeout、带 User-Agent）、解析两家响应格式、
reCAPTCHA 两步轮询、失败返回 `None`。

**不做**：

- **不硬解**。本模块没有一行识别滑块缺口、点选坐标、
  OCR 验证码的代码（硬约束 #9）。它只是个 HTTP 客户端，
  把问题**转交出去**。
- **不猜验证码内容**。短信/邮件 OTP 不在范围内 ——
  那是人的凭证，机器不该碰。
- **不重试**。一次调用一次请求（reCAPTCHA 轮询除外）。
  重试策略是调用方的决定。
- **不缓存**。同一个图重复调用会重复计费 —— 调用方自己去重。
- **不写磁盘**。`api_key` 绝不进任何持久化产物。

---

## 7. 相关命令

```bash
# 查看配置 + 本地校验（不发请求）
python -m avatar solver --test

# 模块自检（98 项，全程假 http，零出网）
python -m avatar.risk_detector.solver

# 单元测试（mock urllib，不发真实请求）
python -m pytest tests/test_phase5_solver.py -q
```
