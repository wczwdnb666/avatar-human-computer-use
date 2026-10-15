# 规划器（planner）

> 一句话目标 → DAG → 执行 → 验证 → 恢复。
> 这份文档讲的是**为什么这么设计**，不是怎么调 API。

最后更新：阶段六 6A / 6H（2026-10-01）

---

## 0. 这一层解决什么

产品哲学是「**除了必要全是自动**」。前五个阶段：
感知 → 操作 → 真人化 → 验证 → 风控。阶段六是"自动"的核心实现层。

它回答一个问题：**给我一句话，怎么把它做完？**

```
"打开记事本输入hello"
        │
        ▼  parse_goal（规则解析）
   Goal{intent=composite, actions=[open(notepad), type(hello)]}
        │
        ▼  RuleBasedPlanner.plan
   Plan{s1: open(notepad), s2: type(hello) ← s1}
        │
        ▼  plan.validate()   ← 无环 + 依赖完整
        │
        ▼  Executor.run
   风控闸 → 观测 → 执行 → 验证 → 恢复
        │
        ▼  memory.save()     ← 可断点恢复
```

---

## 1. 为什么是 DAG 而不是列表

线性列表表达不了"这两步可以同时做"。而真实任务里这种情况很多：

```
打开浏览器 ─┬─→ 打开标签页 A ─┐
            └─→ 打开标签页 B ─┴─→ 对比结果
```

用列表写，就只能拍个顺序，白白串行化。
用 DAG 写，执行器可以直接问"现在哪些步骤的前置都完成了"，然后**并行跑**。

### 规则规划器怎么推断依赖

核心规则：**连续的"建立上下文"动作（`open` / `navigate`）互相独立，
可以并行；其余动作依赖前一个。**

```
打开浏览器，打开记事本，输入hello
─────────┬────────  ─────────┬────────  ────┬────
          │                  │              │
       s1(msedge)        s2(notepad)      s3(type)
          └──── 并行 ────────┘              │
                                            └─ 依赖 s2（要往记事本里打字）
```

**为什么"其余"只依赖前一步，而不是"前面所有步"**：
依赖前面所有步会让 DAG 退化成"每一步都等前面全部完成"，
那样并行永远不可能发生，DAG 就白建了。

### 平局规则

同一层里按 `steps` 的**原始顺序**排。这很重要：
Kahn 算法的结果不唯一，不定平局规则的话，
同一份计划每次跑出来的顺序都可能不同 —— 那会让测试和复盘都变成猜谜。

---

## 2. 三个校验

`Plan.validate()` 返回问题列表（空 = 通过），`assert_valid()` 抛异常。

| # | 检查 | 为什么 |
|---|---|---|
| 1 | id 唯一 | 重名会让依赖引用产生歧义 |
| 2 | 依赖完整 | `depends_on` 引用的 id 必须真的存在 |
| 3 | **无环** | 有环就没有合法的执行顺序 |

**为什么在生成时就校验**：一个不合法的 DAG 交到执行器手里，
最可能的结果是"跑到一半卡住"或者"顺序错乱" —— 两种都很难排查。
在**生成的那一刻**就炸掉，代价最小。

### 一个细节：缺依赖时不报"有环"

如果依赖不完整，Kahn 算法算不出拓扑序，会报"有环" ——
那是**假问题**。一次报两个假问题会误导排查，所以：

```python
if not any("依赖了不存在" in p for p in problems):
    # 只有依赖都完整时，无环检查才有意义
    self.topological_order()
```

---

## 3. PlannerBackend 可插拔

```
PlannerBackend（抽象，唯一方法是 plan(goal) -> Plan）
  ├── RuleBasedPlanner   规则驱动，阶段六的实现
  └── LLMPlanner         大模型驱动（阶段七实现；失败自动回退规则规划）
```

**为什么一定要抽象**（硬约束 2）：规划是整个系统里最不确定的一环。
把它焊死在某个实现上，就意味着"想换个规划策略就得改调用方"。

**两个实现不允许共享具体实现**（硬约束 2）。它们只共享
`PlannerBackend` 的**接口**，不共享任何私有方法 ——
否则"换一个后端"就会变成"改一个后端，另一个跟着变"。

这条被测试钉住：

```
tests/test_phase6_goal.py::TestPlannerBackend::
  test_backends_do_not_share_implementation
```

---

## 4. 为什么阶段六先做规则版

因为规划器的难点**不在理解语言**，在状态机、DAG 调度、自愈、恢复。

用一个不稳定的 LLM 做规划，会让这些更难的部分无法被稳定测试 ——
LLM 今天把"打开记事本"解析成 `open`，明天可能解析成 `launch`，
测试就得跟着它改。

**规则版的覆盖面窄，但它是确定的。** 先把确定的部分做到极致。

### 规则解析的能力与边界

**能处理**：

```
"打开记事本输入hello"           → open + type（按动词位置切分）
"打开记事本，然后输入 hello"     → 同上（按分隔词切分）
"在记事本里输入 hello 然后保存"  → type(target=notepad) + save
"打开 https://example.com"      → navigate（URL 优先于动词分类）
"点击保存"                      → click(target=保存)  ← 见下
```

**「点击保存」为什么不会被拆成两个动作**：中文里"保存"既是动词
也是按钮名。判据是**两个动词之间有没有夹字符**：

```
点击保存          打开记事本输入hello
^^^^               ^^^^      ^^^^
紧邻 → 合并        隔了"记事本" → 不拆分
→ click(保存)     → open(记事本) + type(hello)
```

**不做**：指代消解（"把那个关掉"）、多轮对话、歧义消解、可行性验证。

**解析不出来时返回 `intent="unknown"` 的 Goal，不抛异常** ——
解析失败是常态（用户会说各种奇怪的话），把它做成异常会逼调用方
到处写 try。而且 unknown 的 Goal 仍然带着 `instruction` 原文，
可以直接交给 LLM 后端再试一次。

---

## 5. LLMPlanner 现在能做什么

**能用的部分**（阶段六就位）：

- `can_handle(goal)` —— 判断"配了 key 没有"
- `build_payload(goal)` —— 构造 OpenAI 兼容请求体（**纯函数，可测**）
- `build_headers()` —— 构造请求头（带 User-Agent / Authorization）
- `describe()` —— 配置摘要（**api_key 脱敏**）

**不能用的部分**：`plan()` 抛 `NotImplementedError`。

**为什么现在就把请求构造写出来**：因为**请求构造是可以被测的**
（不需要网络），而"能测的部分先写好"能让阶段七只剩
"发请求 + 解析响应"两件事。

**为什么默认关闭**：与 `risk_detector.solver` 同一个理由 ——
调外部服务要花钱，且是隐私外发，必须显式打开。

---

## 6. 每步的验证判据从哪来

`planner` 为每个步骤生成 `verify` 判据，交给 `verifier`：

| 动作 | `verify.kind` | 判据 |
|---|---|---|
| `open` | `window` | 目标窗口出现 |
| `navigate` | `dom` | 页面 URL 匹配 |
| `type` | `uia` | 控件里的文本等于期望值 |
| `click` / `key` / `scroll` | `pixel` | 界面发生变化 |
| `wait` | `elapsed` | 等待时长已耗尽 |
| `screenshot` | `file` | 截图文件已落盘且非空 |
| `save` | `window` | 标题栏不再有未保存标记 |
| `close` | `window_gone` | 目标窗口消失 |

**每步都必须有判据**（硬约束 5）。判据是给 `verifier` 的，
而 `verifier` 返回的是**三态**（`ok` / `fail` / `unknown`）。

**未知动作也配判据** —— 退到像素对比，至少能看出"变没变"。

---

## 7. 失败建议（`on_fail`）

| 动作 | 建议 | 判据 |
|---|---|---|
| `open` / `navigate` / `type` / `save` | `retry` | 可能是暂态 |
| `click` / `key` | `retry_with_fallback` | 重试可能没用，得换定位方式 |
| `wait` / `scroll` / `screenshot` / `close` | `skip` | 本来就不关键 |

**这只是建议** —— 真正的决策在 `recovery`，它还要看
attempts / switches / risk / error 四样东西。

---

## 8. 能力边界

**做**：目标解析（规则）、DAG 生成、依赖推断、判据生成、失败建议、
生成时校验、后端可插拔。

**不做**：

- **不执行**。生成 Plan 就结束 —— 执行是 `executor`。
- **不重试**。`on_fail` 只是建议，决策在 `recovery`。
- **不调 LLM**（规则版）。`LLMPlanner.plan` 抛 `NotImplementedError`。
- **不猜应用路径**。"打开 Photoshop" 生成的步骤里 target 就是
  `"Photoshop"`，能不能找到是执行层的事。
- **不做步骤合并**。合并会让验证判据变模糊，而"每步可验证"是硬约束。
- **不硬解验证题**（硬约束 #9 全项目生效）。

---

## 9. 相关命令

```bash
# 生成计划并打印 DAG（不执行）
python -m avatar plan --goal "打开记事本输入hello"

# 真的执行
python -m avatar run --goal "打开记事本输入hello"

# 从文件加载计划执行
python -m avatar plan --goal "..." --json > plan.json
python -m avatar run --plan-file plan.json

# 历史任务
python -m avatar tasks
python -m avatar tasks --show <task_id>
python -m avatar tasks --resume <task_id>

# 模块自检
python -m avatar.agentic_planner.goal
python -m avatar.agentic_planner.planner
python -m avatar.agentic_planner.executor
```
