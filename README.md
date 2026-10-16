# Avatar · 让 AI 以人类形式操纵电脑

[![CI](https://github.com/wczwdnb666/avatar-human-computer-use/actions/workflows/ci.yml/badge.svg)](https://github.com/wczwdnb666/avatar-human-computer-use/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](#环境要求)
[![依赖](https://img.shields.io/badge/dependencies-0-brightgreen)](#环境要求)
[![平台](https://img.shields.io/badge/platform-Windows-lightgrey)](#平台边界)
[![许可](https://img.shields.io/badge/license-MIT-green)](LICENSE)

**一句话目标 → 自动拆解 → 自动执行 → 自动验证 → 自动恢复。**
遇到验证题**停下来叫人**，不硬闯。

**零第三方依赖** —— 从 Win32 到 WinRT 到 MCP，全部手搓。

---

## 3 秒上手

```bash
git clone https://github.com/wczwdnb666/avatar-human-computer-use.git
cd avatar-human-computer-use

python -m avatar selfcheck                          # 先自检
python -m avatar plan --goal "打开记事本输入hello"    # 看它打算怎么做
python -m avatar run  --goal "打开记事本输入hello"    # 真的执行
```

**预期**（第三步）：记事本弹出，`hello` 被**一个字一个字**打进去
（真人化节奏，不是瞬间出现），然后：

```
✓ 任务完成  p...
  状态 success   耗时 2.3s   全部步骤完成
    ✓ 成功 s1
    ✓ 成功 s2
```

### 接进 Claude Desktop

```bash
python -m avatar mcp test     # 先验证 12 个工具注册正常
```

然后把 `mcp_config.json` 合并进 `%APPDATA%\Claude\claude_desktop_config.json`，
重启 Claude —— 对话里就能直接说"打开记事本输入你好"。

详见 [docs/mcp.md](docs/mcp.md)。

---

## 演示

<!-- 演示 GIF 位（**尚未录制**，所以这里不放图 —— 不放截图充数）：
     跑 python examples/demo_recorder.py --with-gui 会生成报告与轨迹图，
     录成 GIF 放到 docs/assets/demo.gif，再把下面这行解注释：

         ![演示](docs/assets/demo.gif)

     建议录：案例 1（打开记事本）+ 案例 6（风控熔断）+ 案例 7（Claude 调用）

     为什么不放一张静态截图：截图证明不了"它真的在动"，
     而本项目的卖点恰恰是**动作本身**（真人化轨迹、检出风险就停下）。
-->

**七个可执行案例**（每个都是"命令 + 预期输出"，不是伪代码）：

| # | 案例 | 演示什么 |
|---|---|---|
| 1 | [打开记事本输入 hello](examples/01_open_app.md) | 一句话 → DAG → 执行 |
| 2 | [浏览器搜索并提取](examples/02_browser_search.md) | CDP 直连，不走鼠标 |
| 3 | [跨端填表](examples/03_form_fill.md) | 网页抓数据 → 桌面输入 |
| 4 | [真人化对比](examples/04_human_like_demo.md) | 轨迹与节奏量化 |
| 5 | [LLM 自动拆解](examples/05_planner_task.md) | 自然语言 → 计划 |
| 6 | [风控拦截](examples/06_risk_intercept.md) | 熔断 + F9 恢复 |
| 7 | [Claude 通过 MCP 调用](examples/07_mcp_claude.md) | 外部客户端接入 |

```bash
python examples/demo_recorder.py              # 跑只读案例，生成报告
python examples/demo_recorder.py --with-gui   # 带上会动鼠标的
```

---

## 它解决什么问题

让 AI 操作电脑，最直接的做法是"截屏 → 让模型看图 → 模拟点击"。
**那个做法在真实环境里会碎成一地**：

| 问题 | 天真的做法 | 本项目的做法 |
|---|---|---|
| **点不准** | 直接 `SetCursorPos` | 真人化轨迹（速度曲线 + 震颤 + 落点抖动） |
| **找不着** | 每次都截屏 + OCR | **五层降级**：UIA(2ms) → CDP(20ms) → OCR(300ms) → 模板 → 大模型 |
| **不知道成没成** | 假设"点了就是成了" | **三态验证**：ok / fail / **unknown**（未知 ≠ 成功） |
| **撞上验证码** | 想办法绕过 | **停下来叫人**（响铃 + F9），不硬解 |
| **中途挂了** | 从头再来 | **断点恢复**：状态落盘，已成功的步骤不重跑 |
| **出错不知道为什么** | 只看最后结果 | 每步都记：状态转换 / 两次观测 / **自愈决策** |

---

## 九个阶段

| 阶段 | 解决什么 | 状态 |
|---|---|---|
| **一** 感知 | DPI / 显示器 / 坐标 / 快照 | ✅ |
| **二** 语义 | 窗口 / 进程 / UIA 控件树 | ✅ |
| **三** 输入 | 键鼠合成 + **真人化熵** | ✅ |
| **四** 验证 | 三态验证 / CDP / 重试 / 钩子 | ✅ |
| **五** 风控 | 检测 → 熔断 → 恢复 → 打码 | ✅ |
| **六** 规划 | 一句话 → DAG → 执行 → 自愈 | ✅ |
| **七** 多模态 | OCR / 视觉链 / LLM / **MCP** | ✅ |
| **八** 视觉大模型 | 五层链最后一层 + **四道坐标闸门** | ✅ |
| **九** 交互外壳 | ORB 式悬浮球（**独立包**，不 import 主包） | ✅ |

### 八条铁律

1. **元素定位优先，坐标兜底** —— 坐标在窗口移动后静默失效
2. **窗口相对坐标贯穿全链路** —— 不存屏幕绝对坐标
3. **纯 ctypes 封装 COM** —— 禁第三方封装
4. **静默优先，可达必查** —— 不可达时抛异常，不硬发
5. **真人熵必须可复现** —— 同 seed 逐点一致，否则测试没法写断言
6. **禁止裸 bool** —— 验证必须返回三态
7. **降级必须说明理由** —— 没有 reason 的降级在排查时等于没有信息
8. **知道何时该停** —— 检出风险一律叫人，**不硬闯**

---

## 核心能力速查

### 一句话执行

```python
from avatar.agentic_planner import parse_goal, get_planner, Executor
from avatar.agentic_planner.executor import DefaultRunner

plan = get_planner().plan(parse_goal("打开记事本输入hello"))
out = Executor(DefaultRunner()).run(plan)
print(out.describe())
```

### 五层降级查找

```python
from avatar.vision.chain import VisionChain

r = VisionChain().locate("确定", window=hwnd)
print(r.explain())    # ★ 每层的 reason 与耗时
```

### 风控

```python
from avatar.risk_detector import detect, should_intervene, guard

st = detect(hwnd)
if should_intervene(st)[0]:        # ★ 判据是 is_clean，不是 detected
    guard(st, hwnd=hwnd)           # 停下等人按 F9
```

### 真人化输入

```python
from avatar.human_entropy import mouse as HM, entropy

entropy.seed(42)                   # 可复现
HM.click_human(500, 300)           # 真人化轨迹
```

### MCP

```python
from avatar.mcp import build_default_tools, MCPServer

srv = MCPServer(registry=build_default_tools())
srv.serve_forever()                # 阻塞在 stdin
```

---

## 常用命令

```bash
# ---- 感知与操作 ----
python -m avatar selfcheck                  # 全量自检
python -m avatar windows --title "记事本"    # 找窗口
python -m avatar move --x 500 --y 300 --human
python -m avatar type --hwnd 0x123 --text "你好" --human

# ---- 规划与执行 ----
python -m avatar plan --goal "..."           # 打印 DAG，不执行
python -m avatar run  --goal "..."           # 真的执行
python -m avatar tasks                       # 历史任务
python -m avatar tasks --resume <id>         # 断点续跑

# ---- 风控 ----
python -m avatar detect                      # 单次检测
python -m avatar detect --watch              # 持续监听
python -m avatar pause-test                  # 演示熔断（按 F9）

# ---- 多模态 ----
python -m avatar vision chain --text "确定"   # 五层降级过程
python -m avatar llm plan --goal "..."       # LLM 规划（没 key 会回退）

# ---- MCP ----
python -m avatar mcp test                    # 验证工具注册
python -m avatar mcp serve                   # 启动（stdio）
python -m avatar mcp --list                  # 工具清单
```

---

## 交互外壳（ORB 式悬浮球）

一个 40x40 的桌面悬浮球。**看** Avatar 在干什么，**控** 它做什么。

```bash
python -m avatar_shell                    # 启动
python -m avatar_shell --selftest         # 自检
python -m avatar_shell --position br      # 指定初始位置（tl/tr/bl/br）
```

| 状态 | 颜色 | 动效 | 含义 |
|---|---|---|---|
| 空闲 | 灰 | 静止 | 没任务在跑 |
| 运行中 | 绿 | 呼吸 1.2s | 正常执行 |
| **需要人工** | **橙** | **闪 + 提示音 0.5s** | **它在等你** |
| 出错 | 红 | 静止 | 已经停了，不闪 |

> 橙色闪得最快（0.5s）是有意的：**"需要人工"是唯一需要你立刻看一眼的状态**。
> 红色反而不闪 —— 闪是"要你管"，错是"已经停了"。

右键菜单：启动任务 / 查看任务 / 暂停 / 显示状态 / 退出。

**三条硬约束**（写在 [`avatar_shell/__init__.py`](avatar_shell/__init__.py)）：

1. **不引入第三方 UI 框架** —— 不用 Electron / Tauri / PyQt。
   透明窗口走 `WS_EX_LAYERED` + `UpdateLayeredWindow`，菜单走原生
   `TrackPopupMenu`，提示音走标准库 `winsound`。
   一个 40x40 的球不该背 200MB 运行时。
2. **不 import avatar** —— 外壳与主包只通过 **stdio / MCP** 通信。
   外壳可以独立替换，主包保持零依赖。
3. **故障隔离** —— 外壳是独立进程。它挂了，Avatar 照常跑任务，
   只是没人给你显示状态了。

详见 [avatar_shell/README.md](avatar_shell/README.md)。

---

## 自检

```bash
python scripts/selfcheck.py                # 模块自检 + 硬约束（quick 档，约 20 秒）
python scripts/selfcheck.py --full         # 完整档：连实机探测一起跑（慢）
python scripts/selfcheck.py --tests        # 再连集成测试一起跑（更慢，会动窗口）
python scripts/selfcheck.py --only phase9  # 只看某一阶段（phase3 ~ phase9）
python scripts/selfcheck.py --list         # 只列出会跑什么

python -m pytest tests/ -q                 # 全部测试（约 5 分钟，会动鼠标）

python -m avatar_shell --selftest          # 外壳自检（独立包）
```

**当前状态**（`selfcheck.py` 实测）

| 项 | 数值 |
|---|---|
| 模块自检 | **79 个模块全通过** |
| 阶段检查项 | **153 项全通过**（阶段三 ~ 九） |
| 硬约束 | **111 条**（编号口径，见 [SKILL.md](SKILL.md)） |
| pytest | **1530 项** |
| 第三方运行期依赖 | **0** |

> **两个数字口径不同，别混**：「阶段检查项」是自检实际跑的条数
> （里面含配置可用性、环境探测这类**非硬约束**项）；
> 「硬约束」是 SKILL.md 里逐条编号的那 111 条。两个都对。

> ⚠ **OCR 层在本机不可用**，所以涉及它的检查会走"软失败"分支
> （打 `[--]` 而不是 `[OK]`），**不算失败** —— 那是环境事实，不是代码问题。
> 见下面「能力边界」。

---

## 当前能力边界（如实列出）

**这个项目能做什么、还不能做什么，一页说清。** 不吹、不含糊。

### ⚠ 三个已知限制

#### 1. OCR 层暂不可用（本机环境阻塞）

| 步骤 | 结果 |
|---|---|
| `RoInitialize` / `RoGetActivationFactory` | ✅ 正常 |
| `OcrEngine.TryCreateFromUserProfileLanguages` | ✅ 成功，语言 `zh-Hans-CN` |
| `DataWriter` → `IBuffer` | ✅ 正常 |
| `SoftwareBitmap.CreateCopyFromBuffer` | ❌ **`E_INVALIDARG (0x80070057)`** |

**已排查并排除**：IID 错（`GetIids` 验证全对）、槽位错、`BitmapPixelFormat` 值错
（0~11 全试）、buffer 长度错（`capacity = length = w*h*4` 精确）、图太大（64x64 同样失败）。

**未定位** —— WinRT 的 ABI 深水区。

**应对**：探测到位、如实报告、**可换引擎**：

```python
from avatar.uia import ocr_fallback as O
O.set_engine(my_tesseract_engine, "tesseract")
```

⚠ `is_available()` 会一路探测到**位图通路**，不通就报不可用 ——
**不报"假的可用性"**（那比"不可用"更糟）。

详见 [docs/ocr.md](docs/ocr.md)。

#### 2. 视觉大模型层默认关闭

需要 `enabled=True` + `api_key` 才启用。理由：

- **贵** —— 一次 1280x720 的图约 1229 图像 token
- **慢** —— 图片上传 + 视觉推理，几秒到几十秒
- **隐私外发** —— 会把你的屏幕截图发出去

```bash
set AVATAR_LLM_API_KEY=sk-...
python -m avatar vision llm --live --goal "确定按钮" --enabled
```

#### 3. 五层降级链实际四层在跑

```
UIA  →  CDP  →  OCR  →  模板匹配  →  视觉大模型
✅     ✅     ⚠️ 阻塞   ✅          ✅（默认关）
```

**OCR 那层因上面的 SoftwareBitmap 问题暂不可用**，降级链会自动跳过它继续往下走 ——
这是降级链的设计目的（一层不可用不影响整体）。

补上 OCR 有两条路：
- 解决 SoftwareBitmap 的 ABI 问题（需要更深的 WinRT 调试）
- 用 `set_engine()` 接 Tesseract / PaddleOCR / 云 OCR

### ✅ 已经能跑通的

| 能力 | 状态 | 验证方式 |
|---|---|---|
| 一句话 → DAG → 执行 → 自愈 | ✅ | `avatar run --goal "打开记事本输入hello"` |
| 五层降级查找（UIA/CDP/模板/LLM） | ✅ | `avatar vision chain --text "确定"` |
| 真人化输入（可复现） | ✅ | `avatar entropy --demo` |
| 风控检测 + 熔断叫人 | ✅ | `avatar pause-test` |
| 断点恢复 | ✅ | `avatar tasks --resume <id>` |
| MCP Server（12 个工具） | ✅ | `avatar mcp test` |
| LLM 规划（六家厂商） | ✅ | `avatar llm plan --goal "..."` |
| 视觉大模型层（六家厂商） | ✅ | `avatar vision llm --cost` |

### 平台边界

- **只在 Windows 上完整可用**（大量 Win32 / WinRT 调用）
- 非 Windows 只能跑：规划器 / MCP / 状态机 / 成本控制
- 感知与操作层（UIA / 输入 / 抓屏 / OCR）需要 Windows

---

## 环境要求

- **Windows 10 / 11**（大量 Win32 / WinRT 调用）
- **Python 3.10+**（CI 在 3.10 / 3.11 / 3.12 / 3.13 上跑）
- **零运行期依赖** —— `dependencies = []`，clone 完直接跑

> ⚠ 非 Windows 平台只能跑部分模块（规划器 / MCP / 状态机 / 成本控制），
> 感知与操作层（UIA / 输入 / 抓屏 / OCR）需要 Windows。

跑测试需要 `pytest` —— 这是**唯一的开发期依赖**：

```bash
python -m pip install pytest
```

「零依赖」指的是**运行期**。测试框架不属于运行期依赖，
把它藏起来假装"什么都不用装"没有意义。

### 可选增强（装了更好，不装也能跑）

| extra | 装什么 | 装了会怎样 |
|---|---|---|
| `uia` | `uiautomation` | 多一条 UIA 定位通道 |
| `vision` | `Pillow` | 图像编解码走成熟库 |
| `ocr` | `pytesseract` | 多一个 OCR 引擎（需另装 Tesseract） |
| `web` | `websocket-client` | 多一条 CDP 通道 |
| `dev` | `pytest` | 跑测试 |

```bash
python -m pip install -e ".[dev]"           # 开发：主包 + pytest
python -m pip install -e ".[uia,vision]"    # 只加想要的增强
```

**这些一个都不装，主链路照跑** —— 缺失时自动降级，
不会因为少一个可选包就整个不可用。

---

## 文档

| 文档 | 讲什么 |
|---|---|
| [architecture.md](docs/architecture.md) | 分层、降级链、八条铁律 |
| [philosophy.md](docs/philosophy.md) | 产品哲学 |
| [uia.md](docs/uia.md) | UIA 子系统与踩坑 |
| [human_entropy.md](docs/human_entropy.md) | 真人化的量化定义 |
| [web_cdp.md](docs/web_cdp.md) | CDP 层设计 |
| [verify.md](docs/verify.md) | 三态验证 |
| [risk_model.md](docs/risk_model.md) | 风险判定原则 |
| [solver.md](docs/solver.md) | 打码平台配置 |
| [planner.md](docs/planner.md) | 规划器与 DAG |
| [state_machine.md](docs/state_machine.md) | 状态机与自愈 |
| [ocr.md](docs/ocr.md) | **WinRT OCR 接入** |
| [vision_chain.md](docs/vision_chain.md) | **五层降级链** |
| [llm_backend.md](docs/llm_backend.md) | **多模型适配** |
| [mcp.md](docs/mcp.md) | **MCP 使用指南** |
| [vision_llm.md](docs/vision_llm.md) | **视觉大模型层（四道坐标闸门）** |
| [avatar_shell/README.md](avatar_shell/README.md) | **交互外壳（悬浮球）** |

### 技术文章

写**设计取舍**，不是 API 说明。

| 文章 | 讲什么 |
|---|---|
| [01 为什么只靠视觉不够](docs/articles/01_why_vision_only_is_not_enough.md) | 降级链为什么按**成本**排而不是按准确率排 |
| [02 真人化熵](docs/articles/02_human_entropy.md) | 怎么把"不精确"做得**可复现** |
| [03 零依赖的边界](docs/articles/03_zero_dependency.md) | 什么该手搓、什么该用库 |
| [04 必要的打断](docs/articles/04_necessary_interruption.md) | 为什么"停下来叫人"是功能而不是缺陷 |

---

## 贡献

欢迎 issue 和 PR。先读 [CONTRIBUTING.md](CONTRIBUTING.md) —— 那里写了
**八条铁律怎么在代码里落地**，以及提交前必须跑的三条命令。

安全问题请走 [SECURITY.md](SECURITY.md) 的私密渠道，**不要开公开 issue**。

---

## 主题

`computer-use` · `desktop-automation` · `windows-automation` ·
`ui-automation` · `rpa` · `ai-agent` · `llm-agent` · `mcp` ·
`model-context-protocol` · `ctypes` · `win32` · `winrt` ·
`zero-dependency` · `human-like` · `accessibility` · `ocr` · `cdp` ·
`python` · `agent` · `automation`

---

## 许可

仅供学习与研究使用。

**请勿用于绕过风控、批量注册、爬取受保护内容等用途。**
本库的设计（检出验证题就停下叫人）就是**明确拒绝**这类用法。
