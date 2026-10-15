# 更新日志

本文件记录每个版本的主要变化。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

---

## [v0.1.0] - 2026-10-07

**首个版本。** 九个阶段全部交付，从"能看见屏幕"到"被 Claude 调用"。

### 一句话概括

> 为你的 AI 注入人类灵魂：**纯 ctypes 驱动、零依赖、
> 除了必要全是自动**的 Windows/Web 终极控制引擎。

---

### 阶段一 · 感知层

**解决什么**：知道"屏幕是什么样、坐标系怎么算"。

- `avatar/_win32/` —— 纯 ctypes 封装 Win32 / COM / DPI / 结构体
- `avatar/perception.py` —— 显示器枚举、DPI 查询、坐标换算
- `avatar/vision/capture.py` —— 屏幕 / 窗口 / 显示器抓图（GDI）
- **DPI 感知**：区分物理像素与逻辑像素，坐标换算贯穿全链路

### 阶段二 · 语义层

**解决什么**：知道"界面上有什么"。

- `avatar/uia/` —— UI Automation 全栈（tree / find / actions / patterns）
- `avatar/app_manager/` —— 窗口枚举、进程、启动器、布局
- **元素定位优先，坐标兜底** —— 坐标在窗口移动后静默失效

### 阶段三 · 输入层

**解决什么**：像人一样操作键盘鼠标。

- `avatar/input/` —— SendInput 合成（键盘 / 鼠标 / 滚轮）
- `avatar/human_entropy/` —— **真人化熵引擎**
  - 速度曲线（非对称加减速）
  - 8~12Hz 生理震颤
  - 子运动分解（一次移动由多次微调组成）
  - **可复现**：同 seed 逐点一致，否则测试没法写断言

### 阶段四 · 验证层

**解决什么**：知道"这一步到底成了没有"。

- `avatar/verify/` —— **三态验证**（ok / fail / **unknown**）
- `avatar/web_cdp/` —— CDP 直连（浏览器操作不走鼠标）
- **禁止裸 bool** —— `unknown` 必须和 `fail` 分开：
  前者"没查成"，后者"查了确实没成"

### 阶段五 · 风控层

**解决什么**：遇到验证题**停下来叫人**，不硬闯。

- `avatar/risk_detector/` —— 三层检测（DOM → 窗口 → 像素）
- **熔断**：响铃 + 桌面通知 + 等 F9
- **打码平台**（默认关闭）：配置化接入
- **硬约束 #9**：全项目**没有一行**硬解验证码的代码，将来也不会有

### 阶段六 · 规划与执行

**解决什么**：一句话目标 → 自动拆解 → 执行 → 自愈。

- `avatar/agentic_planner/goal.py` —— 自然语言 → 结构化 Goal（规则解析）
- `avatar/agentic_planner/plan.py` —— 步骤 + 依赖 → **DAG**，校验、拓扑排序
- `avatar/agentic_planner/planner.py` —— `PlannerBackend` 可插拔
- `avatar/agentic_planner/state.py` —— 状态机（**合法转换的唯一入口**）
- `avatar/agentic_planner/memory.py` —— JSON 持久化（**可断点恢复**，禁 pickle）
- `avatar/agentic_planner/observer.py` —— 执行前后各观测一次
- `avatar/agentic_planner/verifier.py` —— 三态验证（像素 / UIA / DOM）
- `avatar/agentic_planner/recovery.py` —— 五种恢复策略 + 三条上限
- `avatar/agentic_planner/executor.py` —— DAG 调度（含并行）+ 风控闸

**三条上限**（硬编码常量）：单步重试 3 次 / 换策略 2 次 / 全局 30 分钟。

### 阶段七 · 多模态

**解决什么**：看懂屏幕、自己思考、被任何 MCP 客户端调用。

- `avatar/_win32/winrt.py` —— **纯 ctypes 手搓 WinRT**
  （HSTRING / RoGetActivationFactory / IAsyncOperation / GetIids）
- `avatar/uia/ocr_fallback.py` —— WinRT OCR（含**逐项诊断**与可换引擎）
- `avatar/vision/chain.py` —— **五层视觉降级链**
- `avatar/agentic_planner/llm_backend.py` —— 多模型适配（零 SDK）
- `avatar/mcp/` —— **MCP Server**（12 个工具，接得上 Claude Desktop）

**五层降级链**：

```
UIA  →  CDP  →  OCR  →  模板匹配  →  视觉大模型
~2ms   ~20ms   ~300ms   ~500ms       ~3s
```

**按成本排序，不按准确率排序。** 贵的层先跑等于每次查找都付最贵的代价。

### 阶段八 · 视觉大模型层

**解决什么**：前四层全失败时的最后一道防线。

- `avatar/vision/llm_backend.py` —— 六家厂商 / 两套协议
- `avatar/vision/image_prep.py` —— 裁剪 → DPR → 缩放 → 编码
  （**JPEG 用 GDI+，纯 ctypes**；PNG 用 zlib 兜底）
- `avatar/vision/prompts.py` —— prompt 模板 + 输出 Schema
- `avatar/vision/validator.py` —— **四道坐标校验闸门**
- `avatar/vision/cost.py` —— 预算 + 缓存 + 记账

**四道闸门**（因为**大模型会编造坐标**）：

```
① 范围校验   0 ≤ x < 宽 且 0 ≤ y < 高
② 坐标换算   图像 → 原图 → 屏幕（漏了会整体偏移 2.3 倍）
③ 像素校验   周围 50x50 灰度方差 > 8（**最有价值的一道**）
④ 置信度     confidence ≥ 0.5
```

**失败返回 `None`，不降级到盲点，不抛异常。**

### 阶段九 · 发布

- `avatar_shell/` —— **ORB 式悬浮球交互外壳**（独立包）
  - 纯 ctypes 调 Windows 原生 API（无 UI 框架）
  - 四种状态可视化（空闲 / 运行中 / 需要人工 / 出错）
  - 原生右键菜单、拖拽移动、点击穿透
  - 与主包**只通过 stdio 通信**（不 import avatar）
- 开源打榜资产：README / CI / Issue 模板 / CHANGELOG / CONTRIBUTING / SECURITY
- `docs/articles/` —— 四篇技术文章

---

### 数字

| 项 | 数量 |
|---|---|
| 阶段 | 9 |
| 模块 | 79（自检登记数） |
| 硬约束 | **111 条**（编号口径，见 SKILL.md） |
| 阶段检查项 | **153 项全通过**（阶段三 ~ 九） |
| pytest | **1530 项** |
| 第三方运行期依赖 | **0** |
| MCP 工具 | 12 |
| 支持的 LLM 厂商 | 6 |
| CI 矩阵 | 4 个 Python 版本（3.10 / 3.11 / 3.12 / 3.13） |

---

### 已知限制

**如实列出，不藏。**

1. **OCR 层暂不可用**（本机环境阻塞）
   `OcrEngine.TryCreateFromUserProfileLanguages` 成功，
   但 `SoftwareBitmap.CreateCopyFromBuffer` 返回 `E_INVALIDARG`。
   已排查并排除 IID / 槽位 / format / buffer 长度 / 图大小，
   **未定位**。可 `set_engine()` 接 Tesseract。

2. **视觉大模型层默认关闭**
   需要 `enabled=True` + `api_key`。理由：贵、慢、隐私外发。

3. **五层降级链实际四层在跑**
   OCR 那层因上述问题跳过，降级链会自动继续往下走。

4. **只在 Windows 上完整可用**
   非 Windows 只能跑规划器 / MCP / 状态机 / 成本控制。

详见 [README 的「当前能力边界」](README.md#当前能力边界如实列出)。

---

### 三条设计原则（贯穿始终）

1. **零依赖** —— 从 Win32 到 WinRT 到 GDI+ 到 MCP，全部手搓。
   不装 `pywin32`、不装 `openai`、不装官方 `mcp` SDK。
2. **失败要能解释** —— 没有 `reason` 的失败在排查时等于没有信息。
   降级要说明"为什么降级"，校验要说明"哪道闸门没过"。
3. **知道何时该停** —— 检出验证题一律叫人，**不硬闯**。
   这是产品哲学「除了必要全是自动」里那个"必要"。

---

## [未发布]

### 计划中

- OCR 层：解决 `SoftwareBitmap` 的 ABI 问题，或内置 Tesseract 适配器
- 视觉大模型层：接入更多厂商（Gemini / Claude Vision 新版本）
- 外壳：托盘图标模式、多显示器 DPI 适配
- 规划器：LLM 后端接入更多协议

---

[未发布]: https://github.com/wczwdnb666/avatar-human-computer-use/compare/v0.1.0...HEAD
[v0.1.0]: https://github.com/wczwdnb666/avatar-human-computer-use/releases/tag/v0.1.0
