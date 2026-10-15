---
name: avatar
description: 让 AI 以人类形式操纵 Windows 电脑。提供窗口/进程管理、UIA 元素定位、控件动作执行、坐标换算、键鼠输入（静默/物理双模式）、真人化运动轨迹与打字节奏，以及浏览器/Electron 的 CDP 控制与双端执行结果验证（三态：成功/失败/未知）。当需要打开或关闭应用、查找窗口、按控件语义点击/填表、直接发键鼠事件、操作网页或 Electron 应用、确认动作是否真的生效、或让自动化操作在行为上像真人时使用。
---

# Avatar · 计算机使用底层库

> 让 AI 以**人类形式**操纵电脑。除了必要，全是自动。

## 这个 skill 是给谁的

给需要**操作 Windows 桌面**的 agent。典型任务：

- 打开某个应用、找到它的窗口、点里面的按钮
- 在表单里填入内容、勾选选项、展开下拉框
- 关闭应用、清理残留进程
- 在多个窗口之间切换焦点
- 把一屏控件读成结构化数据交给模型决策

## 八条铁律（读代码和写代码都要守）

### 1. 元素定位优先，坐标兜底

能按控件语义定位，就绝不按像素定位。

```python
# 对
el = find.find_one(Query(name="保存", control_type="button"))
actions.click(el)

# 错 —— 坐标在窗口移动/DPI 变化后全部失效，而且失效得无声无息
actions.click_at(720, 448)
```

`click()` / `set_value()` 这类高层动作**默认不允许**坐标兜底
（`allow_coord=False`）。要用坐标必须显式传参。

### 2. 窗口相对坐标贯穿全链路

元素**只存客户区坐标**，屏幕坐标在需要的那一刻现算。

```python
cr = el.client_rect()                          # 客户区 (l, t, r, b)
sx, sy = layout.client_to_screen(el.window, *cr.center)   # 现算屏幕坐标
```

不要缓存屏幕坐标。窗口一动它就指向别的东西了，而你不会有任何提示。

### 3. 纯 ctypes 封装 COM，禁止第三方封装

不允许 `pywinauto` / `comtypes` / `win32com`。
所有 UIA 调用走 `avatar/uia/tree.py` 里的手写封装。

理由：引用计数要可见、`HRESULT` 要能自定义处置、
vtable 顺序要能对着 SDK 核。

### 4. 静默优先，可达必查

默认走**静默模式**（`PostMessage`，不抢焦点、不动光标）。
但静默之前**必须**验证目标窗口可达：

```python
from avatar.input import dispatcher as DP

DP.set_interaction_mode("silent")     # 默认就是它
DP.check_window_reachable(hwnd)       # 最小化/不可见/句柄失效 → 抛异常
```

理由：静默模式会**无声失败** —— 窗口最小化了，`PostMessage`
照样返回成功，但什么都不会发生。不可达时直接抛，
要不要降级物理由 `allow_fallback` 决定，降级会在返回值里留痕。

### 5. 真人熵必须可复现

所有随机源走 `entropy.py`，**禁止裸调 `random.*`**。

```python
from avatar.human_entropy import entropy

entropy.seed(42)          # 定种子
plan_a = submove.plan_movement(100, 100, 800, 600)
entropy.seed(42)          # 同种子
plan_b = submove.plan_movement(100, 100, 800, 600)
# plan_a 与 plan_b 逐点相同
```

理由：真人化必然引入随机，而随机会让 bug 无法复现。
如果轨迹每次都不一样，"点歪了"永远查不出原因。

### 6. 禁止裸 bool，验证必须返回三态

任何验证结论只能是 `VerifyResult`，三个状态严格互斥：

```python
from avatar.verify import verify_action, VerifyResult

r = verify_action("desktop", hwnd=win.hwnd,
                  verify_window_title="记事本", timeout=2.0)

r.ok        # 确认成功
r.failed    # 确认失败（确定性，重试没意义）
r.unknown   # 无法判定（多半是时序问题，该重试）
```

**"不知道"是一等公民，不是失败的别名。**

把它塞进 bool 的任何一个值都是错的：塞 `True` 是假装成功、
让错误往下传播；塞 `False` 是触发无谓重试，可能造成重复操作。
而 `unknown` 恰恰携带了最重要的信息 —— **该重试**。

> ⚠ `VerifyResult.__bool__` 返回 `ok`，所以 **`unknown` 是 falsy 的**。
> 写 `x if out.verify else None` 会静默丢掉 unknown 的结论。
> 正确写法是 `out.verify is not None`。

### 7. 降级必须说明理由

Web / Electron 的降级决策**必填** `reason`：

```python
from avatar.web_cdp import fallback as F

d = F.decide_level(process_name="DingTalk.exe", cmdline="...")
d.chosen           # 选中的级别
d.reason           # "no_debug_port" / "pipe_mode_unsupported" / ...
d.degraded         # 是否降级了
d.describe()       # 完整 dict，可直接进日志
```

没有 `reason` 的降级在排查时等于没有信息 —— 你会不知道是
"端口没开"还是"连错了进程"。特别注意 `cdp_available`（探测通过）
与 `cdp_assumed`（跳过探测，**未经验证**）必须能区分。

### 8. 知道何时该停

产品哲学是「**除了必要全是自动**」。前七条铁律都在扩大「自动」的
覆盖面，这一条负责把那句"必要"落到实处 —— **它是一条刹车**。

```python
from avatar.risk_detector import detect, should_intervene, guard

st = detect(hwnd)                    # 三层检测 → RiskStatus
if not st.is_clean:                  # 判据是 is_clean，不是 detected
    guard(st, hwnd=hwnd, current_step="step_3")   # 该停就停，等人按 F9
```

**判据是 `is_clean=False`，不是 `detected=True`。** 这两者差一个
`unknown`：

- `detected=True` → 查出来了 → 要处理
- `is_clean=False` 且 `detected=False` → **没查成** → **也要处理**

只认 `detected` 的话，"OCR 没装 / 读不出页面 / 检测器自己抛异常"
全都会被当成"没问题"放行 —— 那不是自动化，那是盲开车。

`should_intervene(status)` 是唯一的判定入口，返回
`(要不要介入, 原因)`：

| `is_clean` | `detected` | 处置 |
|---|---|---|
| `True` | 任意 | 放行 |
| `False` | `False` | **熔断**（未知，保守） |
| `False` | `True` | 按 `severity`：`continue` 放行 / `pause` 熔断 / `abort` 抛 `RiskBlocked` |

**不硬解**（硬约束 #9）：检出滑块/点选/图形验证码的目的**只有一个
—— 叫人**。本库没有任何识别缺口位置、点选坐标、验证码字符的代码，
将来也不会有。

### 9. 自愈必须有上限，且必须留痕

"自动恢复"是阶段六的核心，也是最容易失控的地方 ——
**无限重试**和**重试不够**都是事故。

三条上限**硬编码为常量**（不是参数）：

| 上限 | 值 | 常量 |
|---|---|---|
| 单步重试 | 3 次 | `MAX_RETRIES` |
| 单步换策略 | 2 次 | `MAX_STRATEGY_SWITCHES` |
| 全局超时 | 30 分钟 | `TASK_TIMEOUT` |

**每次自愈决策必须记入 memory**（禁止静默重试）：

```python
mem.decisions()      # 全部自愈决策，带策略与理由
```

静默重试会让"任务卡在重试循环里"看起来像"任务在正常运行" ——
没有记录就查不出来。

**遇到风险一律叫人，不允许硬闯**（与铁律 8 同源）：

```python
decide_recovery(step, risk=st)   # st.is_clean=False → pause_for_human
```

风险判断**优先于**重试计数 —— 先判重试的话，"第一次就撞验证码"
会被判成 retry，而重试只会再撞一次。

## 快速上手

```python
from avatar.app_manager import launcher as APP, windows as W, state as ST
from avatar.uia import find as F, actions as A, describe as D
from avatar.uia.find import Query

# 1. 打开应用
APP.open_app("notepad")                 # 别名 / 路径 / URI / AUMID 都行

# 2. 找到窗口
win = W.main_window("notepad.exe")

# 3. 按语义定位控件
editor = F.find_one(Query(control_type="document"), window=win.hwnd)

# 4. 执行动作（带验证）
A.set_value(editor, "要写的内容", expect=lambda: A.get_value(editor) == "要写的内容")

# 5. 验证动作真的生效了（判据不足时会回 unknown，不猜）
from avatar.verify import verify_action
r = verify_action("desktop", hwnd=win.hwnd, uia_target=editor,
                  expect_value="要写的内容", timeout=2.0)
print(r.verdict, r.reason)      # ok / fail / unknown

# 6. 读成结构化数据交给模型
print(D.to_prompt([editor], window_name="记事本"))

# 7. 收尾
APP.close_app("notepad.exe", process="notepad.exe")
```

## 模块速查

| 想做什么 | 用什么 |
|---|---|
| 打开 / 关闭应用 | `app_manager.launcher` |
| 找窗口 | `app_manager.windows` |
| 最大化 / 最小化 / 移动窗口 | `app_manager.state` |
| 抢前台焦点 | `app_manager.foreground` |
| 进程枚举 / 完整性级别 / 杀进程 | `app_manager.process` |
| 客户区 ↔ 屏幕坐标换算 | `app_manager.layout` |
| 显示器 / 缩放 / 工作区 | `app_manager.monitor` |
| 找控件 | `uia.find` |
| 读控件信息 | `uia.describe` |
| 操作控件 | `uia.actions` |
| 直接调控件模式 | `uia.patterns` |
| 遍历控件树 | `uia.tree` |
| 键鼠事件（机械） | `input.keyboard` / `input.mouse` |
| 静默/物理模式切换 | `input.dispatcher` |
| 真人化鼠标 / 键盘 | `human_entropy.mouse` / `human_entropy.keyboard` |
| 轨迹、震颤、节奏 | `human_entropy.submove` / `tremor` / `kinetics` |
| 真人度判定 | `human_entropy.metrics` |
| 随机源与种子 | `human_entropy.entropy` |
| 浏览器 / Electron 控制 | `web_cdp.session` / `web_cdp.interact` |
| 找浏览器 / 启动 / 挑端口 | `web_cdp.launcher` |
| 判断该不该用 CDP | `web_cdp.fallback` |
| Electron 识别 / 附加 | `web_cdp.electron` |
| 验证动作是否成功 | `verify.verify_action` |
| 桌面端判据 | `verify.desktop` |
| Web 端判据 | `verify.web` |
| 重试（带验证） | `verify.retry` |
| 自动验证钩子 | `verify.hook` |
| **检测风险（统一入口）** | `risk_detector.detect` |
| **该不该停（唯一判定）** | `risk_detector.should_intervene` |
| **熔断 + 等人** | `risk_detector.guard` / `pause_for_human` |
| **从断点恢复** | `risk_detector.resume_from` |
| **熔断现场快照** | `risk_detector.context` |
| **外部打码（默认关闭）** | `risk_detector.solver` |
| **把风控挂到动作层** | `risk_detector.integration` |
| **一句话 → 执行计划** | `agentic_planner.parse_goal` / `planner.plan` |
| **执行计划（DAG 调度）** | `agentic_planner.Executor` |
| **状态机（唯一转换入口）** | `agentic_planner.transition` |
| **任务记忆（可断点恢复）** | `agentic_planner.ShortTermMemory` |
| **观测 / 验证 / 恢复** | `agentic_planner.observe` / `verify` / `decide_recovery` |
| **五层降级查找** | `vision.chain.VisionChain` |
| **OCR（WinRT）** | `uia.ocr_fallback` |
| **模板/颜色匹配** | `uia.vision_fallback` |
| **LLM 规划（多模型）** | `agentic_planner.llm_backend` / `LLMPlanner` |
| **MCP Server** | `avatar.mcp` |
| **视觉大模型（第五层）** | `vision.llm_backend` / `image_prep` |
| **坐标校验（四道闸门）** | `vision.validator` |
| **调用预算与缓存** | `vision.cost` |
| 单层检测（调试用） | `risk_detector.detector_dom` / `detector_window` / `detector_pixel` |
| 指纹库与弱词分档 | `risk_detector.fingerprints` |

## 常用查询

```python
Query(name="保存")                         # 按名称
Query(automation_id="btnSave")             # 按 AutomationId —— 最稳
Query(control_type="button")               # 按类型
Query(name="确定").any_of(Query(name="OK")) # 多个候选
Query(name="用户名").near(username_field)  # 相对定位
```

控件类型短名：`button` `edit` `document` `text` `checkbox` `radio_button`
`combobox` `list` `list_item` `menu` `menu_item` `tab` `tab_item`
`tree` `tree_item` `window` `pane` `hyperlink` `image` `table`
`data_grid` `slider` `spinner` `progress_bar` `tool_bar` `status_bar`。

> ⚠ Win11 记事本的文本区是 `document`（`RichEditD2DPT`），
> **不是** `edit`。老式判断在这里会一个都找不到。

## 常用动作

```python
A.click(el)                    # 语义点击（Invoke / SelectionItem / Toggle / ...）
A.set_value(el, "文本")        # 写值
A.get_value(el)                # 读值
A.toggle(el, on=True)          # 开关（幂等）
A.check(el) / A.uncheck(el)    # 勾选 / 取消
A.select(el)                   # 选中
A.expand(el) / A.collapse(el)  # 展开 / 收起
A.focus(el)                    # 聚焦
A.scroll(el, vertical=1)       # 滚动
A.read(el)                     # 只读快照
```

所有动作返回 `dict`，含 `ok` / `path` / 相关信息。
**`path` 说明走了哪条路**：`"invoke"` / `"value"` / `"coord"`。
出现 `"coord"` 说明用了坐标 —— 那需要显式授权过。

## 输入层与真人化

### 直接发键鼠事件

```python
from avatar.input import keyboard as KB, mouse as MS, dispatcher as DP

KB.press("ctrl+s")                  # 组合键
KB.type_text("hello 世界")          # 跨键盘布局输入（KEYEVENTF_UNICODE）
MS.click(820, 620)                  # 物理屏幕绝对坐标
MS.move_absolute(100, 100)          # 绝对移动
MS.scroll(-3)                       # 滚轮
```

> ⚠ 输入层的坐标是**物理屏幕绝对坐标**。元素坐标（客户区）
> 必须先经 `layout.client_to_screen()` 换算再进来。
> 直接传客户区坐标是最常见的 bug。

### 模式切换

```python
DP.set_interaction_mode("silent")              # 默认：PostMessage，不动光标
DP.set_interaction_mode("physical")            # SendInput，抢焦点
DP.set_interaction_mode("silent", allow_fallback=True)   # 静默不行就动真格
DP.describe_modes()                            # 两种模式的机制/风险/要求
```

### 真人化动作

```python
from avatar.human_entropy import entropy, mouse as HM, keyboard as HK

entropy.seed(42)                       # 定种子 → 轨迹可复现
HM.move_human(820, 620)                # 贝塞尔路径 + 非对称速度 + 微震颤
HM.click_human(820, 620)
HK.type_text_human("一段自然节奏的文本")  # 对数正态间隔
```

`human=False` 就是机械模式（一次冲到底、零震颤），
用于对照和测试：

```python
from avatar.human_entropy import submove, metrics

entropy.seed(42)
h = submove.plan_movement(100, 100, 820, 620, human=True)
entropy.seed(42)
r = submove.plan_movement(100, 100, 820, 620, human=False)

print(metrics.analyze_move(h).describe())   # 峰值 2-4、直度 <1、非对称
print(metrics.analyze_move(r).describe())   # 峰值 1、直度 ==1、对称
```

### 动作结果：看 `mode_used` 而不是"成功没有"

```python
res = A.click(el)
res.ok             # 成功了吗
res.mode_used      # 实际用了哪个模式："silent" / "physical"
res.fallback_used  # 有没有降级（True = 本想静默，但不得不动真格）
res.method         # 具体手段："uia_invoke" / "silent_click" / "physical_move"
```

这三个字段**必填、无默认值** —— 它们回答的是"**实际发生了什么**"，
而不是"我打算做什么"。

## 浏览器与 Electron（CDP）

### 先判断，再连接

**绝大多数商业 Electron（钉钉、飞书、Discord…）默认不开调试端口。**
硬去连只会拿到一串超时，然后误判成"工具不好用"。所以先判定：

```python
from avatar.web_cdp import fallback as F, electron as E

d = F.decide_level(process_name="msedge.exe",
                   cmdline="msedge.exe --remote-debugging-port=9222")
print(d.chosen, d.reason, d.degraded)
# "cdp"  "cdp_available"  False

# Electron：三重判据（白名单 / CDP 端口反证 / 都没有 → None）
app = E.attach_electron(pid=1234, port=9222)
if app is None:
    ...降级 UIA...              # 附加失败是常态，返回 None 不抛异常
```

### 浏览器控制

```python
from avatar.web_cdp import launcher as LB, session as S

port = LB.pick_free_port()                       # ⚠ 必须先挑空闲端口
proc, info = LB.launch_browser(
    LB.find_browser("any"),                      # "any" 才会找 Edge
    port=port, headless=True, url="about:blank",
    user_data_dir=profile,                       # ⚠ 必须独立 profile
    wait_ready=True)

sess = S.open_session(info.websocket_url)         # 收 ws URL，不是端口
target = sess.find_target(type_="page")           # 注意是 type_ 不是 type
sid = sess.attach_page(target["targetId"])        # 自动 enable Page/Runtime/DOM
print(sess.evaluate("document.title", session_id=sid))   # 直接返回值本体
```

> ⚠ **必须独立 `--user-data-dir`**：撞上用户默认 profile 时，
> Chrome 会**静默地把请求转交给已运行的实例然后自己退出** ——
> 调试端口不生效，而你会以为启动成功了，实际操作的是别人的浏览器。

### CLI

```bash
python -m avatar.cli web-opts                      # 体检：浏览器路径 / 端口 / Electron 窗口
python -m avatar.cli web-eval --headless --js "1+1"
python -m avatar.cli web-eval --port 9222 --js "document.title"
python -m avatar.cli electron-windows
python -m avatar.cli electron-attach --port 9222
```

## 验证动作是否真的生效

**每个动作都该有明确的成功判据。** 统一入口只有 `verify_action`：

```python
from avatar.verify import verify_action

# 桌面：窗口标题
r = verify_action("desktop", hwnd=win.hwnd,
                  verify_window_title="记事本", timeout=2.0)

# 桌面：像素变化（截图 pHash）
r = verify_action("desktop", hwnd=win.hwnd,
                  before_hash=h1, after_shot=shot, expect_change=True)

# Web：URL 变化
r = verify_action("web", sess=sess, url_contains="example.com")

# Web：DOM 文本
r = verify_action("web", sess=sess, selector="#msg", expect_text="已保存")

# auto：自己选路
r = verify_action("auto", uia_target=el, expect_value="新值")
```

`mode` 只能是 `"auto"` / `"desktop"` / `"web"`。
桌面和 Web **共用这一个入口** —— 让调用方自己判断"这是桌面还是网页"
是最容易出错的地方（Electron 既是也不是）。

### 判据不足时回 unknown，不猜

```python
verify_action("desktop")            # 什么判据都没给
# → unknown, reason="没能确定判据（缺少必要参数）"
```

这不是缺陷，是设计。分不清就说分不清。

### 组合

```python
from avatar.verify import verify_any, verify_all

verify_all([...])   # AND：有一个 fail 就 fail
verify_any([...])   # OR：任一 ok 就 ok
# 注意：verify_any 只要有一支 unknown，整体就是 unknown —— 不是 fail
```

### 带验证的重试

```python
from avatar.verify import retry_call, RetryPolicy

out = retry_call(lambda: A.click(el), max_retries=3,
                 verify=lambda v: verify_action("desktop", uia_target=el,
                                                expect_value="期望值"))
out.ok                # 最终成功了吗
out.attempts          # 试了几次
out.gave_up_reason    # 为什么放弃（可读文本）
out.verify            # 最后一次的验证结论（注意可能为 None）
```

默认策略：**异常重试 / `fail` 不重试 / `unknown` 重试**。

`fail` 不重试是刻意的 —— 判据确定不成立，重试只是浪费时间。
`unknown` 重试也是刻意的 —— 多半是时序问题（页面没渲染完、窗口在动画）。

### 自动钩子（默认关闭）

```python
from avatar.verify.hook import set_verify_hook, set_enabled
from avatar.verify import auto_verify_hook

set_verify_hook(auto_verify_hook)
set_enabled(True)          # 打开后动作层自动带验证
```

跳过条件：动作失败 / 没给 suggest / 开关关。
`run_verify_hook` **永不抛异常**（除 KeyboardInterrupt）。

### CLI

```bash
python -m avatar.cli verify --mode desktop --target "保存" --expect-exists
python -m avatar.cli verify --mode desktop --expect-title "WorkBuddy"
python -m avatar.cli --json verify --mode desktop --expect-title "WorkBuddy"
python -m avatar.cli verify-hook
```

退出码：`0` = ok，`5` = fail，`11` = unknown。
只给 `--expect-title` 时会自动取前台窗口。

## 风控：检测 / 熔断 / 恢复 / 打码

### 单次检测

```python
from avatar.risk_detector import detect, detect_quick

st = detect(hwnd)                       # 三层全跑（DOM → 窗口 → 像素），共享 3s 预算
st = detect(hwnd, cdp_session=sess)     # 有浏览器会话时带上，DOM 层会快很多
st = detect_quick(hwnd)                 # 1s 预算 + 跳过像素层

st.is_clean      # 真的干净（查过了，没命中）
st.detected      # 查出来了
st.is_unknown    # 没查成（OCR 没装 / 超时 / 异常）
st.severity      # info / bypassable / interrupt / block
st.action        # continue / pause / abort
st.describe()    # 一行摘要
st.explain()     # 多行，带证据
```

**`is_clean` 和 `detected` 都要看。** `is_clean=False` 但
`detected=False` 是"未知" —— 这时候不该放行。

### 熔断（等人）

```python
from avatar.risk_detector import guard, pause_for_human

# 推荐入口：自动按 status 分派（continue 放行 / pause 等人 / abort 抛 RiskBlocked）
out = guard(st, hwnd=hwnd, current_step="step_3")
out["resumed"]     # 人按了 F9
out["waited"]      # 等了多久
out["degraded"]    # 是否走了无桌面退化路径

# 直接调（你已经确定要停）
out = pause_for_human("检出滑块", hwnd=hwnd, timeout=300)
```

- 提示音用 `winsound.Beep`（**Python 内置，零依赖**）
- 桌面通知优先 `Shell_NotifyIcon`，失败则打印醒目文本
- 热键监听 `GetAsyncKeyState(VK_F9)` 轮询，循环内 `sleep(0.05)`，
  按下后 `sleep(0.3)` 去抖
- **无桌面环境自动退化**：写日志 + 等固定超时 + 抛
  `HumanInterventionTimeout`，不崩
- **Ctrl+C 能干净打断**，不留后台线程（同步循环，刻意如此）

### 恢复（从断点接回去）

```python
from avatar.risk_detector import resume_from, validate_context

plan = resume_from(context, "retry", current_url=now_url)
plan.ok             # 现场还有效吗
plan.strategy       # skip / retry / abort
plan.action_hint    # skip_step / retry_step / raise_abort
plan.describe()     # ✓ 可恢复 retry @step_3 → retry_step
```

三种策略：

| 策略 | 含义 |
|---|---|
| `skip` | 跳过当前步骤，标记 skipped，继续下一跳 |
| `retry` | 从当前步骤重试（**默认**） |
| `abort` | 任务转 Blocked，**抛 `AbortSignal`** |

**现场失效时返回 `False` + 原因，不抛异常** —— 失效是预期内的常态，
不是异常状况。校验 hwnd 是否还在、URL 是否还匹配。

### 外部打码（默认关闭）

```python
from avatar.risk_detector import solve_captcha_via_api, solver_enabled

solver_enabled()    # False（默认）
token = solve_captcha_via_api(api_url=..., api_key=..., image_data=png,
                              enabled=True)     # ← 必须显式打开
```

**必须同时 `enabled=True` 且 `api_key` 非空**，否则一个字节都不发。
理由：打码要花钱，且会把截图发给第三方。详见 `docs/solver.md`。

### 把风控挂到动作层（默认关闭）

```python
from avatar.risk_detector import integration

integration.install(enabled=True)                  # observe：只观察不阻断
integration.install(enabled=True, mode_="block")   # block：检出就熔断
integration.events(limit=10)                       # 看最近发生了什么
integration.describe_integration()                 # 挂载点与统计
```

三个挂载点：物理点击前（只读标题）/ 动作失败 3 次后（完整检测）/
计划步骤前（只读标题）。**未启用时零开销。**

### CLI

```bash
python -m avatar detect                    # 单次检测（前台窗口）
python -m avatar detect --hwnd 0x1234      # 指定窗口
python -m avatar detect --cdp-port 9222    # 走 Web 层
python -m avatar detect --watch            # 持续监听（每 2s），检测到报警
python -m avatar pause-test                # 演示熔断（按 F9 继续）
python -m avatar solver --test             # 校验打码配置（不发请求）
```

## 规划与执行（一句话 → 做完）

### 生成计划

```python
from avatar.agentic_planner import parse_goal, get_planner

goal = parse_goal("打开记事本输入hello")
goal.intent          # "composite"
goal.actions         # [{"action":"open","target":"notepad"}, {...}]
goal.success         # 每步的成功判据（人类可读）
goal.budget_seconds  # 1800（30 分钟）

plan = get_planner().plan(goal)     # 默认 rule 后端
plan.validate()                     # [] = 无环 + 依赖完整
plan.topological_order()            # ['s1', 's2']
plan.ready_steps()                  # 现在能跑的（可并行）
print(plan.describe())              # 画一张 DAG
```

**解析不出来时不抛异常** —— 返回 `intent="unknown"` 的 Goal，
原话保留在 `instruction` 里，可以交给 LLM 后端再试。

### 执行

```python
from avatar.agentic_planner import Executor
from avatar.agentic_planner.executor import DefaultRunner

ex = Executor(DefaultRunner())
out = ex.run(plan)

out.ok               # 全部步骤成功
out.task_state       # success / failed / blocked
out.blocked          # 被风控拦下
out.describe()       # 逐步结果
```

每步的流程是固定的：

```
风控闸 → 观测(before) → 执行 → 观测(after) → 验证 → 恢复决策
```

### 断点恢复

```python
out = ex.resume("task_id")
```

- 已成功的步骤**不重跑**
- 失败/卡住的重新进入调度
- **自愈计数跨恢复累加**（不会因为重启就重置重试上限）

### 状态机

```python
from avatar.agentic_planner import transition, is_terminal

transition("pending", "start", scope="step")      # → "running"
transition("failed", "retry", scope="step")       # → "running"（自愈）
transition("success", "start", scope="step")      # ✗ 抛 StateError

is_terminal("failed", scope="task")   # True  —— 任务失败了就是失败了
is_terminal("failed", scope="step")   # False —— 步骤还能重试
```

**必须带 `scope`** —— `pending`/`running`/`failed` 这些值在两级都有，
光看字符串分不清，而两级转换表不同。

### 自愈决策

```python
from avatar.agentic_planner import decide_recovery

d = decide_recovery(step, error=err, risk=st)
d.value      # "retry" / "retry_with_fallback" / "skip" / "abort" / "pause_for_human"
d.reason     # 为什么这么决定（会记入 memory）

mem.decisions()   # 全部自愈决策，可复盘
```

决策顺序（**不能换**）：

```
1. 有风险        → pause_for_human    ← 最高优先
2. 不可恢复错误  → abort
3. 换策略用完    → pause_for_human
4. 重试没用完    → retry
5. 重试用完能换  → retry_with_fallback
6. 兜底          → pause_for_human
```

### CLI

```bash
python -m avatar plan --goal "打开记事本输入hello"     # 打印 DAG，不执行
python -m avatar run  --goal "打开记事本输入hello"     # 真的执行
python -m avatar run  --goal "..." --dry-run          # 只规划
python -m avatar run  --plan-file plan.json           # 从文件加载
python -m avatar tasks                                # 历史任务
python -m avatar tasks --show <task_id>               # 详情（含自愈决策）
python -m avatar tasks --resume <task_id>             # 断点续跑
```

## 视觉降级链（找到那个按钮）

```python
from avatar.vision.chain import VisionChain

chain = VisionChain()          # 默认关掉最贵的第五层
r = chain.locate("确定", window=hwnd)

r.ok              # 找到了吗
r.rect            # 客户区坐标
r.method          # 用了哪一层（"uia" / "ocr" / ...）
r.fallback_used   # 降级过吗
print(r.explain())  # ★ 完整尝试记录 —— 每层的 reason 与耗时
```

**五层按成本排序**（不能换）：

```
UIA  →  CDP  →  OCR  →  模板匹配  →  视觉大模型
~2ms   ~20ms   ~300ms   ~500ms       ~3s
```

**每层都可关**（`allow_uia=False` 等）—— 测试隔离用。

### CLI

```bash
python -m avatar vision chain --text "确定"
python -m avatar vision chain --text "确定" --no-ocr   # 关掉某层
```

## LLM 规划（多模型）

```python
from avatar.agentic_planner.llm_backend import get_backend, TokenBudget
from avatar.agentic_planner.planner import LLMPlanner

# 直接调后端
b = get_backend("deepseek", enabled=True,
                budget=TokenBudget(per_request=4000, total=50000))
r = b.complete([{"role": "user", "content": "..."}])

# 或者用它规划
planner = LLMPlanner(enabled=True, provider="deepseek", api_key="...")
plan = planner.plan(goal)
planner.last_fallback_reason    # ★ 回退原因（没回退则为空）
```

**五家厂商，两套协议**：

| 厂商 | 协议 |
|---|---|
| `openai` / `deepseek` / `kimi` / `qwen` | OpenAI 兼容 |
| `anthropic` | Claude messages API |

**三条硬约束**：

| 约束 | 默认值 |
|---|---|
| 超时 | 60s（**必须设**，不设会永久挂住） |
| 重试 | 3 次，指数退避，**只重试 429/5xx** |
| token 预算 | 单次 8000 / 累计 200000，**超了不发请求** |

**默认关闭** —— 调外部服务要花钱，必须显式打开。

**失败必须回退规则版**（硬约束 7）：

```bash
python -m avatar llm plan --goal "打开记事本输入hello"   # 没配 key → 回退
```

## MCP Server（让 Claude 调用你）

```bash
python -m avatar mcp test     # 先验证工具注册
python -m avatar mcp serve    # 启动（stdio）
python -m avatar mcp --list   # 工具清单
```

**12 个工具**：`launch_app` / `list_windows` / `mouse_click` /
`type_text` / `uia_find` / `vision_locate` / `cdp_execute_js` /
`detect` / `pause_for_human` / `execute_autonomous_task` /
`tasks_list` / `avatar_status`。

**⚠ 接 Claude Desktop 时最容易踩的三个坑**：

1. **线格式**：MCP 是**换行分隔 JSON**，不是 LSP 的 `Content-Length`
2. **stdout 不能有 print**：客户端会把每一行当协议消息
3. **`PYTHONUNBUFFERED=1` 必加**：不加握手会卡住

详见 `docs/mcp.md`。

## 视觉大模型（降级链第五层）

**前四层全失败才启用。默认关闭** —— 它最贵（约 1400 token/次）也最慢（~3s）。

```python
from avatar.vision import cost as CO, llm_backend as LB, validator as VD
from avatar.vision.chain import VisionChain

chain = VisionChain(
    allow_llm=True,                              # ★ 默认 False
    llm_backend=LB.get_vision_backend("deepseek", enabled=True,
                                      api_key="..."),
    cost_budget=CO.CostBudget(),                 # 单次 2000 / 累计 20000
    cache=CO.ResultCache(),                      # 键 = 截图 hash + 目标
    validator=VD.CoordinateValidator(recheck=False),
)
r = chain.locate("确定按钮", window=hwnd)
r.cost          # 花了多少 token
r.validation    # 第五层的闸门记录
```

### ⚠ 四道闸门：大模型会编造坐标

```
① 范围校验   0 ≤ x < 宽 且 0 ≤ y < 高
② 坐标换算   图像 → 原图 → 屏幕（漏了会整体偏移）
③ 像素校验   周围 50x50 必须有颜色变化（**最有价值的一道**）
④ 置信度     confidence ≥ 0.5
```

**失败返回 `None`，不降级到盲点，不抛异常。**

想知道**哪道闸门没过**：

```python
gates = []
r = validator.validate(res, img, bitmap=bmp, trace=gates)
if r is None:
    failed = [g for g in gates if not g.passed and not g.skipped]
    print(failed[0].gate, failed[0].reason)   # → pixel 该位置是一片空白
```

### 六家厂商

`openai` / `deepseek` / `kimi` / `qwen` / `glm`（OpenAI 兼容）
+ `anthropic`（单独协议）。

**三家硬约束**：超时 60s / 重试 3 次（只重试 429/5xx）/
token 预算（超了**抛 `CostExceeded`**，不静默调用）。

### CLI

```bash
python -m avatar vision llm --live --goal "确定按钮"        # 只预览请求构造
python -m avatar vision llm --live --goal "确定按钮" --enabled  # 真调用
python -m avatar vision llm --cost                        # 看预算
python -m avatar vision llm --clear-cache                 # 清缓存
```

## 交互外壳（悬浮球）

看 Avatar 在干什么，控它做什么。

**独立包**（`avatar_shell/`），**不 import 主包**，只通过 stdio / MCP 通信。

```bash
python -m avatar_shell                    # 启动悬浮球
python -m avatar_shell --selftest         # 自检（4 个模块）
python -m avatar_shell --position br      # 初始位置 tl/tr/bl/br
```

### 四种状态

| 状态 | 颜色 | 动效 | 含义 |
|---|---|---|---|
| `idle` | 灰 | 静止 | 没任务在跑 |
| `running` | 绿 | 呼吸 1.2s | 正常执行 |
| `needs_human` | 橙 | **闪 + 提示音 0.5s** | **它在等你** |
| `error` | 红 | 静止 | 已经停了，不闪 |

**`error` 只能靠 `reset` 退出；`needs_human` 只能靠 `resumed` 回 `running`。**

这两条是刻意设计：出错不能自己"恢复"（否则会被静默吞掉，
变成"看起来在跑"）；人工处理完要**明确告诉它**，不能靠超时自动放行。

橙色闪得最快也是刻意的 —— `needs_human` 是唯一需要你立刻看一眼的状态。

### 三条硬约束

1. **不引入第三方 UI 框架** —— 透明窗口走 `WS_EX_LAYERED` +
   `UpdateLayeredWindow`，菜单走原生 `TrackPopupMenu`，提示音走
   `winsound`。一个 40x40 的球不该背 200MB 运行时。
2. **不 import avatar** —— 只通过 stdio / MCP 通信。外壳可独立替换。
3. **故障隔离** —— 独立进程。它挂了，主包照常跑任务。

详见 `avatar_shell/README.md`。

## 失败处理

```python
from avatar.errors import ElementNotFound, UnsupportedError, ProcessBlocked

# 定位失败 —— 不降级，直接抛
try:
    el = F.find_one(Query(name="不存在"))
except ElementNotFound:
    ...   # 该报错就报错，不要"大概点这里"

# 软失败 —— 允许返回 None
el = F.find_one(Query(name="可能不存在"), required=False)
if el is None:
    ...

# 等元素出现
el = F.wait(Query(name="加载完成"), timeout=10)

# 等元素消失
F.wait_gone(Query(name="正在加载"), timeout=30)
```

## 安全边界

### 进程白名单

```python
process.kill(pid, force=True)                    # 非自己启动的 → ProcessBlocked
process.kill(pid, force=True, allow_foreign=True)  # 显式授权
```

默认只允许杀 **avatar 自己启动的**进程。连自己都不许杀。

### 降级链如实报告

```python
from avatar import uia
uia.fallback_chain()
# [{'name': 'uia', 'available': True, ...},   # 阶段二只有这个可用
#  {'name': 'ocr', 'available': False, 'reason': '...'},
#  {'name': 'vision', 'available': False, 'reason': '...'},
#  {'name': 'coord', 'available': False, 'reason': '...'}]

uia.has_fallback()   # False
```

`has_fallback()` 问的是"**UIA 之外**还有没有兜底"。
返回 `False` 意味着：**UIA 一旦失手，任务只能放弃**。
规划阶段就该据此决定要不要接这个任务。

## 自检

```bash
python scripts/selfcheck.py                # 模块自检 + 111 条硬约束（快，全只读）
python scripts/selfcheck.py --full         # 加上集成测试（慢，会动窗口）
python scripts/selfcheck.py --only phase5  # 只看阶段五（含环境可用性探测）
python scripts/selfcheck.py --list         # 只列出会跑什么（含硬约束清单）

python -m avatar.verify                    # 验证层契约检查（10 项）
python -m avatar.web_cdp                   # CDP 层契约检查（12 项）
python -m avatar.risk_detector.guard       # 风控熔断层（86 项）
python -m avatar.risk_detector.solver      # 打码层（98 项，零出网）
python -m avatar.agentic_planner.goal      # 目标解析（61 项）
python -m avatar.agentic_planner.executor  # 执行器（59 项，不碰真实桌面）
python -m avatar._win32.winrt              # WinRT 地基（37 项）
python -m avatar.uia.ocr_fallback          # OCR + 诊断（31 项）
python -m avatar.vision.chain              # 降级链（45 项）
python -m avatar.mcp                       # MCP（103 项）
python -m avatar.vision.llm_backend        # 视觉后端（65 项）
python -m avatar.vision.image_prep         # 图像预处理（57 项）
python -m avatar.vision.validator          # 四道闸门（56 项）
python -m avatar.vision.cost               # 预算与缓存（61 项）
python -m pytest tests/ -q                 # 全部集成测试
```

模块自检的返回契约统一为：

```python
{"checks": [...], "failures": [...], "ok": not failures}
```

### 111 条硬约束

自检会强制校验下面 111 条，违反即失败。

> **108 条覆盖主包（阶段一 ~ 八），3 条覆盖独立外壳（阶段九）。**
> 全项目生效的 **#9「不硬解验证题」** 对外壳同样适用 ——
> 它只会显示"需要人工"，然后等人。

**阶段一 ~ 三（8 条）**

| # | 约束 | 检查方式 |
|---|---|---|
| 1 | 纯 ctypes，无第三方输入库 | 源码扫描 |
| 2 | `MOUSEINPUT` 32 字节（x64） | 运行时 `sizeof` |
| 3 | 绝对坐标归一化分母 65536 | 扫描 480 点，误差须 0px |
| 4 | 熵源纪律，无裸 `random.*` | 7 个熵模块源码正则 |
| 5 | 可复现性 | 同 seed 30 点逐点一致 |
| 6 | 静默可达性 | 可见 OK / 死句柄抛异常 |
| 7 | `ActionResult` 三个字段必填 | 检查无默认值 |
| 8 | 模块级 docstring | 13 个模块齐查 |

**阶段四（13 条）**

| # | 约束 | 检查方式 |
|---|---|---|
| 9 | 手搓 WebSocket，禁第三方网络库 | 扫 `web_cdp/` 的 import |
| 10 | verify 只返回结构化结论（禁裸 bool） | 实际调用 4 个入口断言类型 |
| 11 | 三态语义完整（ok/fail/unknown 严格互斥） | 构造三态比对属性 |
| 12 | CDP 只走明文 ws，明确拒绝 `wss://` | 断言抛错 |
| 13 | 启动浏览器前必挑空闲端口 | 占用端口须判为不空闲 |
| 14 | 进程黑名单（避免盲目扫端口） | 黑/白名单无交集 |
| 15 | 降级决策必带 `reason` | 4 个场景都有理由 |
| 16 | Electron 识别失败返 `None` 而非抛异常 | 调 `attach_electron` |
| 17 | 浏览器探测（环境信息，缺了不算失败） | 找 Chrome / Edge |
| 18 | Electron 端口扫描可用 | 自起监听须被扫到 |
| 19 | 模块级 docstring | 17 个模块齐查 |
| 20 | 双端验证共用一个入口 | 桌面/Web/auto 返回类型一致 |
| 21 | UIA 收紧旋钮全链路透传 | 源码级盯 4 层转发 |

**阶段五（27 条）** —— 14 条环境可用性 + 13 条硬约束

| # | 约束 | 检查方式 |
|---|---|---|
| 22 | 验证题检测：三层开关可读 | 实际读 `LAYERS` |
| 23 | 验证题检测：检测器可调用 | `detect` 存在 |
| 24 | 验证题检测：OCR 探测（阶段七，可选） | 软失败，缺了只提示 |
| 25 | 提示音可用（`winsound.Beep`） | 真出一声，无声卡则软失败 |
| 26 | 提示音：`winsound` 是内置模块 | 零依赖 |
| 27 | 热键监听：`GetAsyncKeyState` 可读 | `user32` 已加载 |
| 28 | 热键监听：轮询循环能正常退出 | 0.1s 内返回 |
| 29 | 热键监听：F9 = `0x78` | 常量比对 |
| 30 | 打码平台：配置可读 | 读环境变量 |
| 31 | 打码平台：默认关闭策略生效 | `enabled` + `key` 缺一不可 |
| 32 | 打码平台：请求构造可测（纯函数） | `build_*_body` |
| 33 | 打码平台：响应解析兼容两家 | 2Captcha / CapSolver |
| 34 | 熔断环境：桌面探测 | 有桌面 / 退化路径 |
| 35 | 熔断环境：会话类型 | console / remote |
| 36 | ① 打码默认关闭 | 缺任一条件都不启用 |
| 37 | ② 所有请求带 `timeout` | 检查 `urlopen` 调用 |
| 38 | ② 失败返回 `None`（不抛异常） | 坏 http 函数 |
| 39 | ③ 误报率测试用本地样本 | 20 个 HTML，AST 查无 CDP 依赖 |
| 40 | ④ footer 档清零后误报率上升 | 1 → 3 |
| 41 | ⑤ 模块 docstring 含「能力边界」 | AST 取 docstring，12 个模块 |
| 42 | ⑥ 不硬解 | AST 查符号（**不查文本**） |
| 43 | ⑦ `should_intervene` 判定表正确 | 5 种状态全对 |
| 44 | ⑦ `translate_action` 不分叉 | 与上一入口一致 |
| 45 | ⑧ 熔断等待不留后台线程 | 线程数不变 |
| 46 | ⑧ 熔断不用 `threading` | 源码扫描 |
| 47 | ⑨ 风控集成默认关闭 | 未启用时不干预 |
| 48 | ⑩ `quick_check` 不截图（热路径） | AST 查：无截图调用 |

**阶段六（25 条）** —— 4 条可用性 + 4 条可序列化 + 17 条硬约束

| # | 约束 | 检查方式 |
|---|---|---|
| 49 | 目标解析可用 | `parse_goal` 真跑一次 |
| 50 | 规划器可用（规则后端） | `plan()` 产出步骤 |
| 51 | 生成的计划通过校验 | 无环 + 依赖完整 |
| 52 | DAG 支持并行 | 连续 `open` 无依赖 |
| 53 | LLM 后端可插拔（阶段六时是接口占位） | 阶段七已实现 `LLMPlanner.plan()`；失败自动回退规则规划 |
| 54 | 执行器可跑通 | 假 runner 跑 2 步 |
| 55 | 每步前后各观测一次 | 2 步 → 4 条观测 |
| 56 | memory 可序列化为 JSON | `json.dumps` 成功 |
| 57 | memory 能从磁盘恢复 | save → load 往返 |
| 58 | 原子写不留 `.tmp` 残渣 | 检查文件 |
| 59 | ⑤ memory 禁用 pickle | AST 查 import |
| 60 | ⑥ 三条上限（3 / 2 / 30min） | 常量比对 |
| 61 | ⑥ recovery 与 executor 上限一致 | 两处不许分叉 |
| 62 | ⑥ 模拟 20 次失败必须收敛 | 跑一遍看最后策略 |
| 63 | ④ 合法状态转换全对 | 6 条关键路径 |
| 64 | ④ 非法状态转换必须抛 | 3 条非法路径 |
| 65 | ④ `failed` 两级结论相反 | task=终态 step=非终态 |
| 66 | ⑦ 恢复决策表全对 | 6 条全对 |
| 67 | ⑦ 每个决策都带 `reason` | 禁止静默重试 |
| 68 | ⑨ 执行器接风控闸 | `before_step` 存在 |
| 69 | ⑨ 执行器引用了 `integration` | AST 查 import 别名 |
| 70 | ⑩ 模块 docstring 含「能力边界」 | 9 个模块齐查 |
| 71 | ⑪ 不硬解 | AST 查符号 |
| 72 | ⑫ 自检不污染真实任务目录 | 前后目录对比 |
| 73 | 全局收敛 | 上面全部通过 |

**阶段八（35 条）** —— 20 条配置可用性 + 15 条硬约束

| # | 约束 | 检查方式 |
|---|---|---|
| 74 | 六家厂商预设 | `VISION_PRESETS` 长度 |
| 75 | ② 视觉后端默认关闭 | `active is False` |
| 76 | LLM 配置缺什么说得清 | `unavailable_reason()` |
| 77 | ③ 超时与重试有默认值 | 60s / 3 次 |
| 78 | ③ 4xx 不重试 | `RETRYABLE_STATUS` |
| 79 | 默认最长边 1280 | 常量 |
| 80 | 默认 JPEG 质量 85 | 常量 |
| 81 | JPEG 编码器（GDI+） | `jpeg_available()`（软失败） |
| 82 | PNG 兜底可用 | 真编一张 |
| 83 | 预处理产出可用图 | `prepare_for_llm` |
| 84 | 四条约束都写进 prompt | 关键字检查 |
| 85 | 输出 Schema 五个字段 | 字段数 |
| 86 | 置信度下限 0.5 | 常量 |
| 87 | 四道闸门 + 可选二次确认 | `GATE_ORDER` |
| 88 | 像素校验默认必需 | 保守 |
| 89 | 二次确认默认关闭 | 它多花一次调用 |
| 90 | 阈值与 prompt 一致 | 两处比对 |
| 91 | 单次上限 2000 | 常量 |
| 92 | 累计上限 20000 | 常量 |
| 93 | ⑦ 缓存默认开启 | `disabled is False` |
| 94 | 缓存容量有上限 | 常量 |
| 95 | 两处 token 公式一致 | 逐点比对 |
| 96 | ⑨ 第五层是最后一层 | `LAYER_ORDER[-1]` |
| 97 | ⑨ 五层按成本递增 | 单调性 |
| 98 | ⑨ 第五层默认关闭 | 默认关 |
| 99 | ④ 闸门①：越界被拒 | 真跑 |
| 100 | ④ 闸门③：空白区被拒 | 真跑 |
| 101 | ④ 闸门④：低置信被拒 | 真跑 |
| 102 | ④ 没有原图 = 失败 | 保守 |
| 103 | ④ 失败时说出哪道闸门 | `trace` |
| 104 | ⑥ 超预算抛 CostExceeded | 真抛 |
| 105 | ⑦ 缓存键两个都要 | 换目标/换截图 |
| 106 | ⑪ 零第三方 SDK | AST 查 import |
| 107 | ⑫ 不硬解 | AST 查符号 |
| 108 | ⑬ 模块 docstring 含「能力边界」 | 5 个模块齐查 |

**阶段九（3 条）** —— 独立外壳（`avatar_shell/`）

| # | 约束 | 检查方式 |
|---|---|---|
| 109 | ① 不引入第三方 UI 框架 | AST 扫 `avatar_shell/*.py` |
| 110 | ② 不 import avatar | AST 扫 import（含 `from avatar...` 形式） |
| 111 | ③ 故障隔离（独立进程） | `bridge` 用 `subprocess` 拉起主包，不共享内存 |

外壳的另外几条设计约束（**状态机出边、stdout 卫生、不持久化**）
由 `tests/test_phase9_shell.py` 覆盖，不单列进这张表。

## 环境要求

- Windows 10 / 11
- Python 3.10+（CI 在 3.10 / 3.11 / 3.12 / 3.13 上跑）
- **零运行期依赖**（UIA 走手写 ctypes 封装）
- 进程必须是 **DPI aware** —— `perception` 模块会自己设并验证
- 跑测试需要 `pytest`（唯一的开发期依赖）

## 更多

- `docs/architecture.md` —— 分层、降级链、八条铁律
- `docs/uia.md` —— UIA 子系统设计与踩坑记录
- `docs/human_entropy.md` —— 输入层与真人熵的设计与踩坑记录
- `docs/web_cdp.md` —— CDP 层设计与踩坑记录
- `docs/verify.md` —— 验证机制设计与踩坑记录
- `docs/risk_model.md` —— **风险判定的原则**（含"反证按普遍程度排权重"）
- `docs/planner.md` —— **规划器架构与 DAG 设计**
- `docs/state_machine.md` —— **状态机、失败分类、自愈上限**
- `docs/ocr.md` —— **WinRT OCR 接入原理与已知问题**
- `docs/vision_chain.md` —— **五层降级链设计**
- `docs/llm_backend.md` —— **多模型适配 + 预算 + 回退**
- `docs/mcp.md` —— **MCP 使用指南 + Claude Desktop 接入**
- `docs/vision_llm.md` —— **视觉大模型层（第五层）完整设计**
- `avatar_shell/README.md` —— **交互外壳（悬浮球）**
- `docs/articles/` —— 4 篇设计取舍文章
- `examples/` —— 7 个可执行演示案例
- `docs/solver.md` —— 外部打码平台配置指南
- `docs/philosophy.md` —— 产品哲学
- `RELEASE_CHECKLIST.md` —— v0.1.0 发布清单
