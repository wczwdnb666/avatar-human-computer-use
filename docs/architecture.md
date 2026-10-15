# 架构

## 分层

```
┌─────────────────────────────────────────────────┐
│  agentic_planner   目标 → 步骤 DAG → 执行        │  阶段六 ✅
├─────────────────────────────────────────────────┤
│  risk_detector     风险分级 · 熔断 · 恢复  ✅     │  阶段五
├─────────────────────────────────────────────────┤
│  mcp               JSON-RPC over stdio  ✅        │  阶段七
├─────────────────────────────────────────────────┤
│  vision.chain      五层降级查找  ✅               │  阶段七
├─────────────────────────────────────────────────┤
│  vision.llm_*      视觉大模型层（第五层）✅        │  阶段八
├─────────────────────────────────────────────────┤
│  verify            三态验证 · 重试 · 钩子  ✅     │  阶段四
├─────────────────────────────────────────────────┤
│  human_entropy     真人化运动学与节奏  ✅         │  阶段三
├──────────────┬──────────────┬───────────────────┤
│  web_cdp ✅   │  uia  ✅      │  vision           │  阶段四/二/七
│  (协议)       │  (语义)       │  (像素)            │
├──────────────┴──────────────┴───────────────────┤
│  input         鼠标 · 键盘 · 静默/物理分发  ✅    │  阶段三
├─────────────────────────────────────────────────┤
│  app_manager   窗口 · 进程 · 焦点 · 几何  ✅      │  阶段二（只读部分已在阶段一）
├─────────────────────────────────────────────────┤
│  perception    DPI · 显示器 · 坐标 · 快照  ✅     │  阶段一
├─────────────────────────────────────────────────┤
│  _win32        纯 ctypes：结构体 · 签名 · COM    │  阶段一 + 阶段二/三增补
└─────────────────────────────────────────────────┘
```

**`avatar_shell` 不在这张图里** —— 它是**独立包**，与主包只通过
stdio / MCP 通信（**不 import avatar**）。这是刻意的：外壳可以独立
替换，主包保持零依赖，外壳崩了不影响主包。
详见「阶段九的定位」。

**`risk_detector` 是唯一"回头看"的层**：其它层都在往外做动作，
只有它在问"现在这个屏幕还安全吗"。它消费 `web_cdp`（读 DOM）、
`app_manager`（读窗口）、`vision`（截图），产出
`RiskStatus`，再由 `guard` 决定停不停。详见 `docs/risk_model.md`。

**`agentic_planner` 是唯一的"向上"层**：它不直接操作系统，而是
编排别的层 —— 把 `uia` / `web_cdp` / `input` 的能力按 DAG 组织起来，
每步前后调 `risk_detector` 检查、调 `verify` 判定。
它也是 `risk_detector` 的第三个挂载点。详见 `docs/planner.md`。

**`verify` 横跨全层**：它不产生动作，只回答"动作成功了吗"。
之所以放在 `human_entropy` 之上，是因为它要同时消费
`web_cdp` 的 DOM 回读和 `uia` 的属性回读 ——
以及 `input` 的落点轨迹（真人化容差判定）。
详见 `docs/verify.md`。

**`input` 与 `human_entropy` 分开**：输入层管"能不能做到"，
真人熵管"像不像人"。输入层可以独立工作（`human=False`），
真人熵只在输入层之上做轨迹与节奏编排。
详见 `docs/human_entropy.md`。

## 降级链

执行一个动作时的尝试顺序：

```
1. UIA       目标暴露 UI Automation → 按控件语义定位（阶段二主通道）
2. CDP       如果目标是浏览器/Electron → 走 DevTools Protocol（阶段四 ✅）
3. OCR       截图 + 文字识别 → 按文字定位（阶段七）
4. Vision    截图 + 模板匹配 → 按像素定位（阶段七）
5. Coordinate  已知坐标 → 直接点（显式 opt-in，永不默认）
```

**CDP 与 UIA 的优先顺序不是固定的。** 对浏览器，CDP 能直接
读 DOM —— 比走 UIA 拿到的那棵"辅助功能树"精确得多。
但对 Electron 应用，**必须先判定它到底开没开调试端口**：
绝大多数商业 Electron 默认不开，硬连只会拿到一串超时。

`web_cdp/fallback.py` 的 `decide_level()` 负责这个判断，
每次都返回一个带 `reason` 的 `FallbackDecision`。
理由字段是必须的 —— 没有它，你分不清"端口没开"和"连错了进程"。

**每一层都在"定位失败"时**才向下走，而不是"猜一个坐标"。
`LocateError` 这个异常类型的存在就是为了强制这条规则。

降级链的顺序和可用性通过 `avatar.uia.fallback_chain()` 对外暴露，
每一环都带有 `available` 和 `reason`。**不可用时必须给原因** ——
这比"静默失败"重要得多。`has_fallback()` 回答的是"UIA 之外还有没有兜底"，
阶段二返回 `False`（OCR/vision/coord 都还没实现），
上层规划阶段就该据此决定要不要接这个任务。

## 贯穿全链路的铁律

这些铁律独立于具体模块，是所有代码都要遵守的约束。违反任何一条都算 bug。

- 铁律 1–3（阶段一/二）：元素定位优先、窗口相对坐标、纯 ctypes 封装 COM
- 铁律 4–5（阶段三）：静默优先可达必查、真人熵必须可复现
- 铁律 6–7（阶段四）：禁止裸 bool 必须三态、降级必须说明理由

后两条在前面对应的「阶段N的定位」一节展开。

### 1. 元素定位优先，坐标兜底

能按控件语义定位就绝不按像素定位。像素定位在窗口移动、
主题切换、DPI 变化下全部失效，而且失效得**无声无息**。

`avatar click` / `avatar set` 这类命令**禁止**自动降级到坐标。
要坐标必须显式传 `allow_coord=True`。

### 2. 窗口相对坐标贯穿全链路

元素只存**客户区坐标**（相对于所属窗口），屏幕坐标在需要的那一刻现算。

理由：窗口一移动，所有屏幕坐标作废，而客户区坐标不变。
缓存屏幕坐标 = 缓存在未来某一刻会指向错误位置的值。

`layout.client_to_screen(hwnd, x, y)` 和
`layout.screen_to_client(hwnd, x, y)` 是仅有的两个换算原语。
`Element.client_rect()` 用 `self.window`（元素**所在**的顶层窗口），
不是 `self.hwnd`（元素**自己**的窗口，多数是 0）。

### 3. 纯 ctypes 封装 COM

禁止 `pywinauto` / `comtypes` / `win32com`。理由见 `docs/uia.md` 开头。

配套硬约束：

- 所有 Win32 / COM 调用必须显式声明 `argtypes` 与 `restype`
- 所有 `HRESULT` 必须经过 `check_hresult`，失败抛带十六进制码的 `ComError`
- `ComPtr` 必须实现 `__enter__` / `__exit__`，自动 `Release`，不许泄漏
- `MOUSEINPUT` 的 32 字节填充规则是**模块级常量**，所有结构体引用它

## 阶段一的定位

阶段一不产生任何动作，只建立**可信的感知**。

为什么必须先做它：Avatar 的所有动作最终都落到"屏幕上某个物理像素"。
DPI 错一步，上面所有层的坐标全部偏移，而且不报错。

### 唯一真值原则

**物理像素是唯一真值。**

| 坐标系 | 含义 | 出现位置 |
|---|---|---|
| 物理像素 | 屏幕上的真实像素 | 全部对外接口 |
| 逻辑像素 | 物理 / scale | 仅 `to_logical()` / `to_physical()` 内部 |
| 归一化 0..65535 | SendInput 绝对坐标 | 仅 `normalize_absolute()` |

代码评审时如果发现逻辑坐标流到了别的地方，那是 bug。

### 验证而非假设

`SetProcessDpiAwareness(2)` 的返回值**不可信**：

- 进程已经建过窗口 → 返回 `E_ACCESSDENIED`，设置不生效
- 某些兼容层会返回成功但实际没生效

所以 `verify()` 用**两条独立路径交叉比对**：

```
GetSystemMetrics(SM_CXSCREEN)     ← 进程视角
        vs
EnumDisplaySettingsW(CURRENT)      ← 显示器真实模式
```

两者不等 → 我们还在逻辑坐标系下。这条检查不依赖任何 API 的返回值，
所以能戳穿静默失败。

## 阶段二的定位

阶段二把"窗口"和"控件"变成可操作的一等对象。它不产生输入事件
（那是阶段三的输入层），只负责**找准目标**。

| 子层 | 职责 | 关键点 |
|---|---|---|
| 几何 `layout` | 客户区 ↔ 屏幕坐标换算 | 往返必须无损，200% 缩放下取整误差在这里暴露 |
| 显示器 `monitor` | 分辨率、缩放、工作区 | 每屏独立 DPI |
| 窗口 `windows` | 枚举、查找、打分、UWP | 过滤 IME/GDI+ 辅助窗口 |
| 状态 `state` | 最大化/最小化/置顶/吸附 | 状态变更**异步**，必须轮询确认 |
| 前台 `foreground` | 抢焦点、`AttachThreadInput` | 只读探测，测试里不抢用户焦点 |
| 进程 `process` | 枚举、完整性级别、启停 | **白名单**：默认只杀自己启动的 |
| 启动 `launcher` | 别名、URI、UWP AUMID | AUMID 里不能带版本号 |
| UIA | 元素定位、描述、动作 | 见 `docs/uia.md` |

### 进程白名单是安全阀

```python
kill(pid, force=True)                    # 非自己启动的 → ProcessBlocked
kill(pid, force=True, allow_foreign=True)  # 显式授权
```

默认拒绝的理由：Avatar 会经常调用 `kill`（关应用、清残留）。
如果默认允许，一次定位错误就可能杀掉用户的数据密集型进程。
要求显式传参的成本很低，收益是"误杀不可能发生"。

连自己都不许杀 —— `pid == os.getpid()` 在检查白名单**之前**就拦下。

### 窗口状态变更是异步的

```python
state.maximize(hwnd)   # 调完 API 不代表已经最大化
```

`SetWindowPos(SWP_SHOWWINDOW)` 这类调用只是**投递请求**。
立刻读回状态会拿到旧值，于是"最大化"看起来没生效。
实测在 `restore()` 后立刻 `maximize()` 会撞 `0x80131509`。

解法：`set_visual_state()` 带重试与轮询（最多 3 次，指数退避），
`maximize/minimize/restore` 都接受 `timeout` 参数。

## 阶段三的定位

阶段二负责**找准目标**，阶段三负责**动手**。

| 子层 | 职责 | 关键点 |
|---|---|---|
| 键盘 `input.keyboard` | 虚拟键映射、`SendInput` 键事件 | `KEYEVENTF_UNICODE` 跨布局输入 |
| 鼠标 `input.mouse` | 绝对坐标归一化、鼠标事件 | 分母 **65536** 不是 65535 |
| 分发 `input.dispatcher` | 模式管理、静默/物理 | 可达性必须验证，不许静默失败 |
| 熵源 `human_entropy.entropy` | 统一随机源、可复现 | 禁止裸 `random.*` |
| 运动学 `human_entropy.kinetics` | 速度曲线、时长 | Woodworth 双相，峰值 40–55% |
| 震颤 `human_entropy.tremor` | 8–12Hz 相关噪声 | 幅度随速度**递减** |
| 子运动 `human_entropy.submove` | 主运动 + 修正 + 微调 | 贝塞尔路径 |
| 度量 `human_entropy.metrics` | 峰值检测、真人度判定 | 峰判据相对**峰自身** |

### 铁律 4：静默优先，可达必查

阶段三新增一条铁律。默认走静默模式（不抢焦点、不动光标），
但**静默之前必须验证窗口可达**：

```python
dispatcher.check_window_reachable(hwnd)
# null_hwnd / invalid_hwnd / minimized / invisible → TargetUnreachable
```

理由：静默模式的致命问题是**无声失败** —— 窗口最小化了，
`PostMessage` 照样返回成功，但什么都不会发生。

配套：`ActionResult` 的 `mode_used` 告诉你**实际发生了什么**，
`fallback_used` 告诉你**有没有降级**。这两个字段没有默认值。

### 铁律 5：真人熵必须可复现

所有随机源走 `entropy.py`，同一 seed + 同一路径 = 逐点相同的轨迹。

理由：真人化必然引入随机，而随机会让 bug 无法复现。
如果轨迹每次都不一样，"点歪了"永远查不出原因。

验证不看统计量，直接**逐点比对**（30 个采样点全部相等）。

### 阶段的验证核心

| 检查 | 位置 | 判据 |
|---|---|---|
| 坐标归一化往返 0 误差 | 硬约束 #3 | 扫描 480 点 |
| 结构体尺寸 | 硬约束 #2 | x64 `MOUSEINPUT=32` |
| 熵源纪律 | 硬约束 #4 | 源码正则扫 7 模块 |
| 可复现性 | 硬约束 #5 | 同 seed 30 点逐点一致 |
| 真人 vs 机械分界 | `test_phase3_entropy.py` | 真人 400 全过 / 机械 400 全拒 |

## 阶段四的定位

阶段三负责**动手**，阶段四负责**补齐 Web 端**和**回答"成功了吗"**。

| 子层 | 职责 | 关键点 |
|---|---|---|
| `web_cdp.websocket` | RFC 6455 手搓 | 客户端必掩码；控制帧 ≤125B 不可分片 |
| `web_cdp.session` | CDP JSON-RPC | id 匹配；`sessionId` 路由；超时与协议错分离 |
| `web_cdp.launcher` | 找浏览器 / 挑端口 / 启动 | **必须**独立 `user-data-dir`，否则静默转交已有实例 |
| `web_cdp.dom/interact/tab` | DOM 操作 | |
| `web_cdp.electron` | Electron 识别 | 三重判据；失败返 `None` 不抛异常 |
| `web_cdp.fallback` | 降级决策 | 每次降级必带 `reason` |
| `verify.result` | 三态结论 | 禁止裸 bool |
| `verify.desktop/web` | 双端判据 | 桌面走 pHash + UIA 回读，Web 走 DOM + URL |
| `verify.action` | 统一入口 | 内部路由，外部只看到一个 `verify_action` |
| `verify.retry` | 重试 | `fail` 不重试，`unknown` 重试 |
| `verify.hook` | 全局钩子 | 默认关闭；永不抛异常 |

### 铁律 6：禁止裸 bool，必须三态

阶段四新增。任何验证结果只能是 `VerifyResult`，三个状态严格互斥：

```
ok      → 确认成功
fail    → 确认失败（确定性，重试没意义）
unknown → 无法判定（多半是时序问题，该重试）
```

**"不知道"是一等公民，不是失败的别名。**

理由：把 unknown 塞进 bool 的任何一个值都是错的。
塞 True 是假装成功、让错误往下传播；塞 False 是触发无谓重试、
可能造成重复操作。而 unknown 恰恰携带了最重要的信息 —— 该重试。

落地检查：`scripts/selfcheck.py` 的阶段四硬约束 #2 会实际调用
4 个验证入口，断言返回类型。

### 铁律 7：降级必须说明理由

阶段四第二个新增铁律，是铁律 4（静默优先，可达必查）在 Web 端的类比。

`FallbackDecision` 的 `reason` 字段是**必填**的：

| reason | 含义 |
|---|---|
| `process_never_cdp` | 进程在黑名单里 |
| `no_debug_port` | 浏览器但没带调试端口 |
| `pipe_mode_unsupported` | 用了管道模式（本库只支持 TCP） |
| `port_not_cdp` | 端口开着但不是 CDP（可能连错进程） |
| `cdp_available` | 探测通过 |
| `cdp_assumed` | 跳过探测，**可用性未经验证** |

最后两条必须能区分 —— 不能把"猜的"和"验过的"写成同一个结果。

### 阶段的验证核心

| 检查 | 位置 | 判据 |
|---|---|---|
| 禁第三方网络库 | 阶段四硬约束 #1 | 源码扫 `web_cdp/` |
| 禁裸 bool | 阶段四硬约束 #2 | 4 个入口返回 `VerifyResult` |
| 三态严格互斥 | 阶段四硬约束 #3 | unknown ≠ fail |
| 只走明文 ws | 阶段四硬约束 #4 | `wss://` 必须被拒 |
| 启动前挑端口 | 阶段四硬约束 #5 | 占用端口须判为不空闲 |
| CDP 端到端 | `test_phase4_cdp.py` | 手搓 mock 验 id 匹配与会话路由 |
| 大帧不截断 | `test_phase4_websocket.py` | 80000 字节往返完整 |

## 阶段五的定位

阶段五回答一个问题：**"知道何时该停"**。

产品哲学是「除了必要全是自动」。前四个阶段都在扩大"自动"的
覆盖面，阶段五负责把那句"必要"落到实处 —— 它是一条**刹车**。

### risk_detector 内部调用关系

```
                     ┌──────────────────────────┐
  三层检测            │  detector.detect()       │  统一入口 · 共享总预算
  （共享 3s 预算）    │  按层降级 · 命中即返回     │
                     └───┬────────┬────────┬────┘
                         │        │        │
              ┌──────────▼─┐ ┌────▼─────┐ ┌▼──────────────┐
              │detector_dom│ │detector_ │ │detector_pixel │
              │ DOM/CDP    │ │window    │ │ 截图 → OCR    │
              │ ~50ms      │ │ ~20ms    │ │ 0.5~3s        │
              └──────┬─────┘ └────┬─────┘ └──────┬────────┘
                     │            │              │
                     └────────────┼──────────────┘
                                  ▼
                     ┌──────────────────────────┐
                     │ fingerprints（85 条指纹） │  唯一的判定素材
                     │ 强指纹 + 弱词分档惩罚      │  三层共用
                     └────────────┬─────────────┘
                                  ▼
                     ┌──────────────────────────┐
                     │ status.RiskStatus        │  三态：clean/检出/未知
                     │ + ACTION_INTERFACE       │  动作契约表
                     └────────────┬─────────────┘
                                  ▼
        ┌─────────────────────────┼─────────────────────────┐
        │                         │                         │
   ┌────▼─────────┐      ┌────────▼────────┐      ┌─────────▼────────┐
   │context.py    │      │guard.py         │      │solver.py         │
   │ 造现场        │─────▶│ should_intervene│      │ 外部打码（可选）  │
   │ 截图 + 存盘   │      │ pause_for_human │      │ **默认关闭**      │
   └──────────────┘      └────────┬────────┘      └──────────────────┘
                                  ▼
                         ┌──────────────────┐
                         │resume.py         │
                         │ skip/retry/abort │  从断点接回去
                         └──────────────────┘
```

### 三个挂载点（integration.py）

检测能力做出来没人调，等于没有。所以挂到动作层的必经之路上：

| 挂载点 | 时机 | 开销 | 理由 |
|---|---|---|---|
| `input/dispatcher` | **物理点击前** | 只读窗口标题 | 物理点击是唯一真动鼠标的动作，点错代价最大 |
| `uia/actions` | 失败**连续 3 次后** | 完整三层（~3s） | 单次失败太常见；3 次是强信号 |
| `agentic_planner` | 每个步骤前 | 只读窗口标题 | 纵深防御（步骤可能不经 dispatcher） |

**风控集成默认关闭。** 未启用时每次动作只多一次
`is_installed()` 布尔判断 —— 纳秒级，对性能无可感影响。
打开方式：

```python
from avatar.risk_detector import integration
integration.install(enabled=True)                  # 只观察，不阻断
integration.install(enabled=True, mode_="block")   # 检出就熔断
```

### 阶段的验证核心

| 检查 | 位置 | 判据 |
|---|---|---|
| 判据唯一入口 | 阶段五硬约束 ⑦ | `should_intervene` 五态全对 |
| 打码默认关闭 | 阶段五硬约束 ① | `enabled` + `api_key` 缺一不可 |
| 失败不抛异常 | 阶段五硬约束 ② | 坏 http 函数 → 返回 `None` |
| 误报率 < 5% | `test_phase5_false_positive.py` | 20 个真实页面最多 1 个误报 |
| 消融有效 | 阶段五硬约束 ④ | footer 清零后误报 1 → 3 |
| 不硬解 | 阶段五硬约束 ⑥ | AST 查符号（不查文本） |
| 无后台线程 | 阶段五硬约束 ⑧ | `guard.py` 里没有 `threading.Thread` |
| quick_check 轻量 | 阶段五硬约束 ⑩ | AST 查：不出现任何截图调用 |

## 阶段六的定位

阶段六回答一个问题：**"给我一句话，怎么把它做完？"**

它是「除了必要全是自动」里"自动"的核心实现层：

```
一句话目标 → 自动拆解 → 自动执行 → 自动验证 → 自动恢复
```

### agentic_planner 内部调用关系

```
     "打开记事本输入hello"
              │
              ▼
     ┌─────────────────┐
     │ goal.parse_goal │  规则解析（阶段六）／LLM（阶段七）
     └────────┬────────┘
              ▼  Goal{intent, actions, success, budget}
     ┌─────────────────┐
     │ planner.plan    │  PlannerBackend 可插拔
     │  RuleBased ✅    │  依赖推断：连续 open 可并行，其余链式
     │  LLMPlanner ⏳   │
     └────────┬────────┘
              ▼  Plan{steps, edges}
     ┌─────────────────┐
     │ plan.validate   │  无环 + 依赖完整 + id 唯一
     └────────┬────────┘
              ▼
     ┌──────────────────────────────────────────┐
     │ executor.Executor.run                     │
     │  ① 取 ready_steps（DAG 拓扑，可并行）      │
     │  ② 每步：                                  │
     │     风控闸 → 观测 → 执行 → 验证 → 恢复     │
     │  ③ 状态转换全过 state.transition            │
     │  ④ 全部记入 memory                          │
     └──┬────────┬─────────┬──────────┬──────────┘
        │        │         │          │
   ┌────▼───┐ ┌──▼─────┐ ┌─▼───────┐ ┌▼──────────┐
   │state   │ │observer│ │verifier │ │recovery   │
   │状态机   │ │观测    │ │三态验证  │ │五种策略    │
   └────────┘ └────────┘ └─────────┘ └───────────┘
                              │
                    ┌─────────▼─────────┐
                    │ memory            │
                    │ JSON 存档，可恢复   │
                    │ ~/.avatar/tasks/  │
                    └───────────────────┘
```

### 三条铁律（阶段六新增）

**铁律 8：知道何时该停**（阶段五立的，阶段六执行）

`executor` 每步执行前调 `risk_detector` 的风控闸。
检出风险一律 `pause_for_human`，**不允许硬闯**。

**铁律 9：自愈必须有上限，且必须留痕**

单步重试 3 次 / 换策略 2 次 / 全局 30 分钟 —— 硬编码为常量。
每次自愈决策**必须**记入 memory（禁止静默重试）。

**铁律 10：每步可观测、可验证、可回滚**

- `observer` 执行前后**各观测一次**（硬约束 8，禁止跳过）
- `verifier` 给出**三态**结论（`unknown` ≠ `fail`）
- 状态机保证任意时刻能回答"现在在哪一步"

### 阶段的验证核心

| 检查 | 位置 | 判据 |
|---|---|---|
| DAG 无环 | `plan.validate()` | Kahn 算法 |
| 依赖完整 | `plan.validate()` | 引用的 id 必须存在 |
| 状态转换合法 | 阶段六硬约束 ④ | 非法转换抛 `StateError` |
| `failed` 两级结论相反 | 阶段六硬约束 ④ | task=终态 step=非终态 |
| 三条上限 | 阶段六硬约束 ⑥ | 模拟 20 次失败必须收敛 |
| 决策表正确 | 阶段六硬约束 ⑦ | 6 条全对 |
| memory 可序列化 | 阶段六硬约束 ⑤ | JSON 往返 + 禁 pickle |
| 每步前后都观测 | 阶段六硬约束 ⑧ | 2 步 → 4 条观测 |
| 不污染真实目录 | 阶段六硬约束 ⑫ | 自检前后目录不变 |

## 阶段七的定位

阶段七是**最后一公里**：让 Avatar 真正能"看懂屏幕、自己思考、
被任何 MCP 客户端调用"。

### 四件事

| 能力 | 模块 | 状态 |
|---|---|---|
| **看懂屏幕** | `_win32/winrt` + `uia/ocr_fallback` | ✅ WinRT 手搓；⚠ 位图通路有已知问题 |
| **有序查找** | `vision/chain` | ✅ 五层降级 |
| **自己思考** | `agentic_planner/llm_backend` + `LLMPlanner` | ✅ 五家厂商 |
| **被调用** | `mcp/` | ✅ 12 个工具，接得上 Claude Desktop |

### 调用关系

```
    外部客户端（Claude Desktop / Cursor）
              │  JSON-RPC over stdio
              ▼
     ┌────────────────────┐
     │ mcp.server         │  stdout 只出 JSON-RPC，日志走 stderr
     │ mcp.tools（12 个） │
     └────────┬───────────┘
              ▼
     ┌──────────────────────────────────────────┐
     │ agentic_planner（阶段六）                  │
     │  RuleBasedPlanner  ←→  LLMPlanner         │
     └────────┬─────────────────────────────────┘
              ▼
     ┌──────────────────────────────────────────┐
     │ vision.chain（五层降级）                   │
     │  UIA → CDP → OCR → 模板 → 大模型          │
     └──┬────────┬─────────┬──────────┬─────────┘
        │        │         │          │
   ┌────▼───┐ ┌─▼──────┐ ┌▼────────┐ ┌▼──────────┐
   │uia.find│ │web_cdp │ │ocr_     │ │vision_    │
   │        │ │        │ │fallback │ │fallback   │
   └────────┘ └────────┘ └────┬────┘ └───────────┘
                              ▼
                    ┌──────────────────┐
                    │ _win32/winrt     │  纯 ctypes 手搓
                    │ HSTRING/vtable/  │  零 SDK
                    │ IAsyncOperation  │
                    └──────────────────┘
```

### 阶段的验证核心

| 检查 | 位置 | 判据 |
|---|---|---|
| vtable 槽位正确 | 阶段七硬约束 + `test_phase7_winrt` | `TryCreateFromUserProfileLanguages=[10]` |
| HSTRING 生命周期 | `test_phase7_winrt` | `hstring_ref` 往返正确 |
| 降级顺序 | 阶段七硬约束 ④ | `uia → cdp → ocr` 就停 |
| 每层留痕 | 阶段七硬约束 ④ | 每层都有 reason |
| token 预算 | 阶段七硬约束 ⑤ | 超预算不发请求 |
| LLM 回退 | 阶段七硬约束 ⑦ | 失败回退规则版 |
| stdout 卫生 | 阶段七硬约束 ⑧ | MCP server 里没有 print |
| 零 SDK | 阶段七硬约束 ⑪ | 七个模块只用标准库 |
| MCP 线格式 | 阶段七硬约束 ① | 换行分隔 JSON（不是 LSP） |

## 阶段八的定位

阶段八是**五层降级链的最后一层**：前四层全失败才启用。

启用意味着**高成本 + 低不确定性**，所以设计得极其保守。

### 六个模块

```
                     ┌──────────────────────────┐
   VisionChain  ────▶ │ 第五层 _layer_llm         │
   （最后一层）        │                           │
                     └──┬────────────────────┬───┘
                        │                    │
              ┌─────────▼────────┐  ┌────────▼─────────┐
              │ llm_backend      │  │ image_prep       │
              │ 六家厂商 / 两协议 │  │ 裁剪→DPR→缩放→编码│
              └─────────┬────────┘  └────────┬─────────┘
                        │                    │
              ┌─────────▼────────┐  ┌────────▼─────────┐
              │ prompts          │  │ validator        │
              │ 四条硬约束        │  │ ★ 四道闸门        │
              └──────────────────┘  └────────┬─────────┘
                                             │
                                   ┌─────────▼─────────┐
                                   │ cost              │
                                   │ 预算 + 缓存 + 记账 │
                                   └───────────────────┘
```

### ⚠ 四道闸门（本阶段最重要的设计）

**大模型会编造坐标** —— 这不是"偶尔出错"，是结构性的。

| 闸门 | 挡什么 |
|---|---|
| ① 范围 | 模型给了 (5000, 3000)，图只有 1280x720 |
| ② 换算 | 图缩到 0.44 倍，漏了换算会整体偏移 2.3 倍 |
| ③ **像素** | 模型在**空白处**报坐标（**最有价值的一道**） |
| ④ 置信度 | 模型说"可能是吧"，你却拿它去点 |

**失败返回 `None`，不降级到盲点。**

### 阶段的验证核心

| 检查 | 位置 | 判据 |
|---|---|---|
| 默认关闭 | 阶段八硬约束 ② | 六家全部 `active=False` |
| 超时+UA+重试 | 阶段八硬约束 ③ | 60s / 3 次 / 4xx 不重试 |
| 四道闸门 | 阶段八硬约束 ④ | 越界/空白/低置信全被拒 |
| 超预算抛异常 | 阶段八硬约束 ⑥ | `CostExceeded` 带详情 |
| 缓存键 | 阶段八硬约束 ⑦ | 换目标/换截图都不命中 |
| 第五层在最后 | 阶段八硬约束 ⑨ | `LAYER_ORDER[-1] == llm` |
| 零 SDK | 阶段八硬约束 ⑪ | 五个模块只用标准库 |

## 阶段九的定位

阶段九是**交互外壳** —— 全项目唯一一个**不在 `avatar/` 包里的模块**。

存在理由很单一：Avatar 跑任务可以跑几十分钟，你得知道它
**在干什么**，以及**什么时候需要你**。

### 独立包，不是子模块

```
avatar/                     avatar_shell/
├── vision/                 ├── state.py    状态机（零 IO）
├── agentic_planner/        ├── menu.py     原生菜单
├── mcp/                    ├── ball.py     分层透明窗口
└── ...                     └── bridge.py   stdio → MCP
        ▲                            │
        └──────── stdio / MCP ───────┘
             （单向依赖，不 import）
```

**为什么必须是独立包**（硬约束 2）：

- 外壳可以**独立替换** —— 想换个 UI 就换，主包一行不动
- 主包保持**零依赖** —— 它不需要知道外壳存在
- 外壳崩了**不影响主包** —— 独立进程，故障隔离（硬约束 3）

### 四种状态

| 状态 | 颜色 | 动效 | 出边 |
|---|---|---|---|
| `idle` | 灰 | 静止 | → running |
| `running` | 绿 | 呼吸 1.2s | → needs_human / error / idle |
| `needs_human` | 橙 | **闪 + 提示音 0.5s** | → running（**只能靠 `resumed`**） |
| `error` | 红 | 静止 | → idle（**只能靠 `reset`**） |

**两条刻意设计**：

- **`error` 没有别的出边** —— 出错不能自己"恢复"，得有人明确清掉。
  否则"出错"会被静默吞掉，变成"看起来在跑"。
- **`needs_human` 只能通过 `resumed` 回 `running`** —— 人工处理完了
  要**明确告诉它**，不能靠超时自动放行。

橙色闪得最快（0.5s）也是刻意的：`needs_human` 是唯一需要你
立刻看一眼的状态；红色反而不闪 —— 闪是"要你管"，错是"已经停了"。

### 阶段的验证核心

| 检查 | 位置 | 判据 |
|---|---|---|
| 不 import avatar | 阶段九硬约束 ② | AST 扫 `avatar_shell/*.py` |
| 零第三方 UI 框架 | 阶段九硬约束 ① | 只用 `ctypes` / `winsound` |
| 透明窗口 | `ball.py` | `WS_EX_LAYERED` + `UpdateLayeredWindow` |
| 不占任务栏 | `ball.py` | `WS_EX_TOOLWINDOW` |
| 点击穿透可切换 | `ball.py` | `WS_EX_TRANSPARENT` |
| 拖拽 | `ball.py` | `WM_NCHITTEST` → `HTCAPTION` |
| 原生菜单 | `menu.py` | `TrackPopupMenu` + `TPM_RETURNCMD` |
| 状态机无出边泄漏 | `state.py` | `error` 除 `reset` 外无出边 |
| 桥接 stdout 卫生 | `bridge.py` | 日志只走 stderr，stdout 只走 JSON-RPC |

## 交付清单

| 文件 | 阶段 | 职责 |
|---|---|---|
| `_win32/structs.py` | 一 | 结构体与常量，逐字节对齐 |
| `_win32/api.py` | 一 | Win32 函数签名显式声明 |
| `_win32/dpi.py` | 一 | DPI 初始化、验证、显示器枚举、坐标换算 |
| `_win32/guids.py` | 二 | GUID / IID 常量与命名表 |
| `_win32/com.py` | 二 | `ComPtr`、`check_hresult`、`ComError` |
| `_win32/variant.py` | 二 | `VARIANT` 布局与 BSTR 生命周期 |
| `vision/capture.py` | 一 | GDI 抓屏、像素读取、BMP 写出 |
| `app_manager/layout.py` | 二 | `Rect` 与客户区/屏幕换算原语 |
| `app_manager/monitor.py` | 二 | 显示器、缩放、工作区、相邻屏 |
| `app_manager/windows.py` | 一/二 | 窗口枚举、查找、打分、UWP |
| `app_manager/state.py` | 二 | 窗口状态与几何调整 |
| `app_manager/foreground.py` | 二 | 前台窗口、线程挂接、抢焦点 |
| `app_manager/process.py` | 二 | 进程枚举、完整性级别、启停（带白名单） |
| `app_manager/launcher.py` | 二 | 应用启动/关闭、别名、UWP |
| `uia/tree.py` | 二 | UIA COM 封装、遍历、属性读取 |
| `uia/patterns.py` | 二 | 控件模式的类型化封装 |
| `uia/find.py` | 二 | 三级通道定位 |
| `uia/describe.py` | 二 | 人读 / 模型读两套输出 |
| `uia/actions.py` | 二/三 | 三级执行阶梯 + 接入输入层 |
| `uia/ocr_fallback.py` | 二/七 | **WinRT OCR**（手搓 ABI；位图通路受阻，见 README「能力边界」） |
| `uia/vision_fallback.py` | 二/七 | **模板匹配 / 颜色聚类 / 差分**（纯 Python 算法） |
| `input/keyboard.py` | 三 | 虚拟键映射、`SendInput` 键事件 |
| `input/mouse.py` | 三 | 绝对坐标归一化、鼠标事件 |
| `input/dispatcher.py` | 三 | 模式管理、静默/物理分发、`ActionResult` |
| `human_entropy/entropy.py` | 三 | 统一随机源、可复现种子 |
| `human_entropy/kinetics.py` | 三 | 非对称速度曲线、时长模型 |
| `human_entropy/tremor.py` | 三 | 8–12Hz 相关噪声震颤 |
| `human_entropy/submove.py` | 三 | 子运动分解、贝塞尔路径 |
| `human_entropy/mouse.py` | 三 | 真人化鼠标动作 |
| `human_entropy/keyboard.py` | 三 | 真人化打字 |
| `human_entropy/metrics.py` | 三 | 轨迹指标、峰值检测、真人度判定 |
| `perception.py` | 一 | 聚合视图、窗口几何、坐标换算入口、自检 |
| `cli.py` | 一/二/三 | 命令行 |
| `scripts/selfcheck.py` | 二/三 | 统一自检入口 + 8 条硬约束 |
| `tests/test_phase1.py` | 一 | 47 项测试 |
| `tests/test_phase2_com.py` | 二 | 30 项测试（HRESULT/ComPtr/VARIANT/IID/vtable） |
| `tests/test_phase2_windows.py` | 二 | 76 项测试（几何/显示器/窗口/状态/前台/进程/启动） |
| `tests/test_phase2_uia.py` | 二 | 30+ 项测试（树/模式/定位/描述/动作/降级链） |
| `tests/test_phase3_input.py` | 三 | 27 项测试（键盘/鼠标/归一化/坐标） |
| `tests/test_phase3_entropy.py` | 三 | 46 项测试（熵源/运动学/震颤/子运动/峰值/真人度） |
| `tests/test_phase3_dispatcher.py` | 三 | 21 项测试（模式/可达性/静默/物理/降级） |
| `tests/test_phase3_actions.py` | 三 | 14 项测试（接入/坐标换算/兜底纪律） |
| `tools/dump_uia_vtbl.py` | 二 | 从 SDK 头文件解析权威 vtable 顺序 |
| `docs/uia.md` | 二 | UIA 子系统的设计与踩坑记录 |
| `docs/human_entropy.md` | 三 | 输入层与真人熵的设计与踩坑记录 |
| `web_cdp/websocket.py` | 四 | 手搓 RFC 6455（零第三方依赖） |
| `web_cdp/session.py` | 四 | CDP JSON-RPC 会话与事件分发 |
| `web_cdp/electron.py` | 四 | Electron 识别与 CDP 端口发现 |
| `web_cdp/fallback.py` | 四 | 降级决策（带 reason） |
| `verify/result.py` | 四 | 三态结论 + pHash |
| `verify/desktop.py` | 四 | 桌面端判据（pHash / 标题 / UIA 回读） |
| `verify/web.py` | 四 | Web 端判据（DOM / URL / 网络） |
| `verify/action.py` | 四 | 统一入口与内部路由 |
| `verify/retry.py` | 四 | 重试策略（fail 不重试 / unknown 重试） |
| `verify/hook.py` | 四 | 全局验证钩子（默认关闭） |
| `human_entropy/verify_hook.py` | 四 | 真人化落点容差判定 |
| `tests/test_phase4_websocket.py` | 四 | 57 项（含手搓服务端） |
| `tests/test_phase4_cdp.py` | 四 | 21 项（含手搓 CDP mock） |
| `tests/test_phase4_electron.py` | 四 | 51 项（进程识别 / 端口扫描 / 降级） |
| `tests/test_phase4_verify.py` | 四 | 63 项（三态语义 / 重试 / 灰区） |
| `docs/web_cdp.md` | 四 | CDP 层的设计与踩坑记录 |
| `docs/verify.md` | 四 | 验证机制的设计与踩坑记录 |
| `risk_detector/status.py` | 五 | 三态结论 + `ACTION_INTERFACE` 契约表 |
| `risk_detector/fingerprints.py` | 五 | 85 条指纹 + 弱词三档惩罚 |
| `risk_detector/detector_dom.py` | 五 | 第一层：DOM / CDP |
| `risk_detector/detector_window.py` | 五 | 第二层：窗口标题 / 类名 / 遮挡 |
| `risk_detector/detector_pixel.py` | 五 | 第三层：截图 → OCR → 文本匹配 |
| `risk_detector/detector.py` | 五 | 三层降级编排（共享总预算） |
| `risk_detector/context.py` | 五 | 熔断现场快照 + 自动截图存盘 |
| `risk_detector/guard.py` | 五 | 熔断：`should_intervene` / `pause_for_human` |
| `risk_detector/resume.py` | 五 | 恢复：`skip` / `retry` / `abort` |
| `risk_detector/solver.py` | 五 | 外部打码（**默认关闭**） |
| `risk_detector/integration.py` | 五 | 与主流程的三个挂载点 |
| `agentic_planner/executor.py` | 六 | 执行器（DAG 调度 / 自愈 / 断点恢复） |
| `tests/test_phase5_guard.py` | 五 | 106 项（熔断四路径 / 判定表 / Ctrl+C） |
| `tests/test_phase5_resume.py` | 五 | 83 项（三策略 / context 失效） |
| `tests/test_phase5_solver.py` | 五 | 133 项（mock urllib 验请求构造） |
| `tests/test_phase5_false_positive.py` | 五 | 28 项（20 个真实页面 / 消融） |
| `tests/fixtures/normal_pages/` | 五 | 20 个本地 HTML 正常页面样本 |
| `docs/risk_model.md` | 五 | 判定原则（含"反证按普遍程度排权重"） |
| `docs/solver.md` | 五 | 打码平台配置指南 |
| `agentic_planner/goal.py` | 六 | 自然语言 → 结构化 Goal（规则解析） |
| `agentic_planner/plan.py` | 六 | 步骤 + 依赖 → DAG，校验、拓扑排序 |
| `agentic_planner/planner.py` | 六 | Goal → Plan（Backend 可插拔） |
| `agentic_planner/state.py` | 六 | 状态机（合法转换的唯一入口） |
| `agentic_planner/memory.py` | 六 | 任务持久化（JSON，可断点恢复） |
| `agentic_planner/observer.py` | 六 | 执行前后各观测一次 |
| `agentic_planner/verifier.py` | 六 | 三态验证（像素 / UIA / DOM） |
| `agentic_planner/recovery.py` | 六 | 五种恢复策略与上限 |
| `agentic_planner/executor.py` | 六 | DAG 调度 + 风控闸 + 全链路记账 |
| `tests/conftest.py` | 六 | 全局夹具（光标复位 / 熔断产物清理 / 任务隔离） |
| `tests/test_phase6_*.py` | 六 | 7 个文件 309 项测试 |
| `docs/planner.md` | 六 | 规划器架构与 DAG 设计 |
| `docs/state_machine.md` | 六 | 状态机、失败分类、自愈上限 |
| `_win32/winrt.py` | 七 | WinRT 地基（HSTRING / vtable / IAsyncOperation） |
| `uia/ocr_fallback.py` | 七 | OCR（WinRT，含诊断与可换引擎） |
| `uia/vision_fallback.py` | 七 | 纯 Python 匹配（NCC / 颜色 / diff） |
| `vision/chain.py` | 七 | 五层视觉降级链 |
| `agentic_planner/llm_backend.py` | 七 | 多模型适配 + token 预算 + 重试 |
| `agentic_planner/planner.py` | 七 | `LLMPlanner` 实现（含回退） |
| `mcp/protocol.py` | 七 | JSON-RPC over stdio 编解码 |
| `mcp/server.py` | 七 | MCP Server（stdio 主循环） |
| `mcp/tools.py` | 七 | 12 个 MCP 工具 |
| `mcp/__main__.py` | 七 | `python -m avatar.mcp` |
| `mcp_config.json` | 七 | Claude Desktop 配置模板 |
| `examples/*.md` | 七 | 7 个可执行演示案例 |
| `examples/demo_recorder.py` | 七 | 演示录制器（生成报告 + 轨迹图） |
| `tests/test_phase7_*.py` | 七 | 5 个文件 204 项测试 |
| `docs/ocr.md` | 七 | WinRT OCR 接入原理与已知问题 |
| `docs/vision_chain.md` | 七 | 五层降级链设计 |
| `docs/llm_backend.md` | 七 | 多模型适配 + 预算 + 回退 |
| `docs/mcp.md` | 七 | MCP 使用指南 + Claude Desktop 接入 |
| `vision/llm_backend.py` | 八 | 六家厂商 / 两套协议适配 |
| `vision/image_prep.py` | 八 | 裁剪 → DPR → 缩放 → 编码（JPEG/PNG） |
| `vision/prompts.py` | 八 | prompt 模板 + 输出 Schema |
| `vision/validator.py` | 八 | **四道坐标校验闸门** |
| `vision/cost.py` | 八 | 预算 + 缓存 + 记账 |
| `vision/chain.py` | 八 | 第五层实现（`_layer_llm`） |
| `tests/test_phase8_*.py` | 八 | 6 个文件 233 项测试 |
| `docs/vision_llm.md` | 八 | **视觉大模型层完整设计** |
| `avatar_shell/state.py` | 九 | Presence 状态机（四态 + 转换表 + 颜色/动效） |
| `avatar_shell/ball.py` | 九 | 悬浮球窗口（分层透明 / 拖拽 / 穿透） |
| `avatar_shell/menu.py` | 九 | 原生右键菜单（`TrackPopupMenu`） |
| `avatar_shell/bridge.py` | 九 | 与 `avatar.mcp` 的 stdio 桥 |
| `avatar_shell/__main__.py` | 九 | `python -m avatar_shell` |
| `tests/test_phase9_shell.py` | 九 | 外壳测试（独立包，不依赖主包） |
| `.github/workflows/ci.yml` | 九 | 多 Python 版本矩阵 + 零依赖 AST 检查 |
| `.github/workflows/release.yml` | 九 | tag 触发 + 版本一致性校验 + Release |
| `.github/ISSUE_TEMPLATE/` | 九 | Bug / 功能请求模板（禁空 issue） |
| `CHANGELOG.md` `CONTRIBUTING.md` `SECURITY.md` | 九 | 开源基建三件套 |
| `docs/articles/*.md` | 九 | 4 篇设计取舍文章（832 行） |

## 两条关键一致性测试

这两条是阶段一的验收核心。它们用**完全独立的两条路径**读同一个像素并逐点比对：

1. **抓屏像素 vs GetPixel** —— `BitBlt` 整屏拷贝出来的图，其中
   `(dx, dy)` 处的像素，必须等于 `GetPixel(origin_x + dx, origin_y + dy)`。
   这证明抓图的 origin 与光标坐标系是同一套。

2. **客户区图 origin vs ClientToScreen(0,0)** —— 窗口客户区截图的原点
   必须等于 `ClientToScreen` 给出的屏幕坐标。这证明窗口内相对坐标可用。

DPI 或 origin 有任何偏差，这两条立刻失败。它们是"感知层可信"的机器证明，
而不是靠人肉观察。

## 阶段二的验收核心

对应上面三条铁律，各有一条测试钉住：

1. **坐标往返无损** —— `tests/test_phase2_windows.py`
   取客户区 4 个采样点（含右下角），
   `client_to_screen` → `screen_to_client` 必须**逐点还原**。
   这是"窗口相对坐标贯穿全链路"的地基；200% 缩放下差一像素就会红。

2. **作用域不泄漏** —— `tests/test_phase2_uia.py`
   限定 `window=<记事本>` 查元素，返回的每个元素的
   `window` 字段都必须等于该窗口。跨窗口串味意味着
   "在 A 窗口里点到了 B 窗口的按钮"。

3. **失败不降级** —— `tests/test_phase2_uia.py` + `test_phase2_windows.py`
   - 找不到元素 → `ElementNotFound`，不是坐标
   - 没有可点模式 + `allow_coord=False` → `UnsupportedError`，不是坐标
   - 非自启进程 + `allow_foreign=False` → `ProcessBlocked`，不是硬杀

这三条用 `⭐` 标记在测试输出里，红了就是底线破了。

