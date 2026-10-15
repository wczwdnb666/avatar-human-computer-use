# 状态机与恢复（state_machine）

> 状态机错了，恢复就无从谈起。
> 这份文档讲的是**状态怎么流转、失败怎么分类、自愈怎么收敛**。

最后更新：阶段六 6B / 6E / 6H（2026-10-01）

---

## 0. 为什么要有状态机

因为"自动恢复"的前提是**知道现在在哪一步、哪一步失败了**。

散落的布尔标志（`is_running` / `has_failed` / `is_done`）会组合出
几十种互相矛盾的状态，而排查问题时你需要的是**一个**确定的答案。

**硬约束 4：所有状态转换必须通过 `transition()`，禁止直接赋值。**
直接赋值不会检查合法性，`SUCCESS → RUNNING` 这种回退在代码里
不会报错，只会在某天让任务莫名其妙重跑一遍。

---

## 1. 两条状态线

```
TaskState   PENDING → RUNNING → (SUCCESS | FAILED | BLOCKED)
StepState   PENDING → RUNNING → (SUCCESS | FAILED | SKIPPED | BLOCKED)
```

区别在于步骤多了个 `SKIPPED`（人工选择跳过），
而任务没有 —— "整个任务被跳过"没有意义。

### 状态值同时存在于两个枚举里

`pending` / `running` / `success` / `failed` / `blocked` 这五个值
**在两级都有**。光看字符串分不清是哪一个 —— 而它们的转换表**不同**。

所以 `transition` / `can_transition` / `is_terminal` 都**必须**带 `scope`：

```python
transition("running", "skip", scope="step")    # → skipped
transition("running", "skip", scope="task")    # ✗ 抛 StateError
```

默认 `scope="step"`（步骤级转换是绝大多数调用点）。

**这是设计上真实存在的歧义，不能靠猜。** 猜错的后果是：
任务级的非法转换被放行，或者步骤级的合法转换被拒。

---

## 2. ⚠ `failed` 在两级下结论相反

这是整个状态机里最容易搞错的一条：

| 状态 | 任务级 | 步骤级 |
|---|---|---|
| `success` | 终态 | 终态 |
| `failed` | **终态** | **不是终态** |
| `skipped` | （不存在） | 终态 |
| `blocked` | 非终态 | 非终态 |

**为什么步骤级 `failed` 不是终态**：如果它是终态，自愈就没法工作了 ——
而自愈是阶段六的核心目标。

`failed --retry--> running` 这条转换是"**再试一次**"，
是新的一次尝试（`attempts` 会 +1）。

**真正"不可再试"的判断在 `recovery`**（它看的是 attempts 上限），
不是状态机。状态机只管"这个转换合不合法"。

---

## 3. 转换表

### 任务级

| 当前 | 事件 | 转到 |
|---|---|---|
| `pending` | `start` | `running` |
| `running` | `succeed` | `success` |
| `running` | `fail` | `failed` |
| `running` | `block` | `blocked` |
| `running` | `abort` | `failed` |
| `blocked` | `resume` | `running` |
| `blocked` | `succeed` | `success` |
| `blocked` | `fail` | `failed` |
| `blocked` | `abort` | `failed` |

### 步骤级

| 当前 | 事件 | 转到 |
|---|---|---|
| `pending` | `start` | `running` |
| `pending` | `skip` | `skipped` |
| `running` | `succeed` | `success` |
| `running` | `fail` | `failed` |
| `running` | `skip` | `skipped` |
| `running` | `block` | `blocked` |
| **`failed`** | **`retry`** | **`running`** ← 自愈的关键 |
| `blocked` | `resume` | `running` |
| `blocked` | `retry` | `running` |
| `blocked` | `succeed` / `fail` / `skip` / `abort` | 各自终态 |

### 幂等

在终态上调 `abort` 会返回它自己（不抛）。理由：
执行器可能在"已经失败"之后又收到一次放弃请求，那不该是错误。

---

## 4. 失败分类

`recovery` 把失败分成三类：

| 类别 | 例子 | 处置 |
|---|---|---|
| **暂态** | 元素没渲染完、网络抖了一下 | `retry` |
| **需要换路** | 定位策略不对、界面变了 | `retry_with_fallback` |
| **不可恢复** | 权限不足、进程被杀、账号被封 | `abort` |

### 不可恢复的识别

两类判据：

1. **异常类型**在 `UNRECOVERABLE_TYPES` 里
   （`PermissionError` / `ProcessBlocked` / `UnsupportedError` …）
2. **错误消息**含 `UNRECOVERABLE_HINTS` 里的词
   （`access denied` / `权限不足` / `进程已退出` / `账户已被封禁` …）

**为什么还要看消息**：很多底层错误（COM 的 `HRESULT`）会被包成
通用异常，类型信息丢了，但消息里还留着 `Access denied`。

**认不出来时返回"可恢复"** —— 这是刻意的方向：
**误判成 abort 会丢掉整个任务，误判成 retry 只是多花几秒。**
两害相权取其轻。

---

## 5. 恢复策略与决策顺序

```
1. 有风险（detected=True）        → PAUSE_FOR_HUMAN   ← 硬约束 9，最高优先
2. 不可恢复错误                    → ABORT
3. 换策略次数用完                  → PAUSE_FOR_HUMAN
4. 重试次数没用完                  → RETRY
5. 重试用完但还能换                → RETRY_WITH_FALLBACK
6. 兜底                            → PAUSE_FOR_HUMAN
```

### ⚠ 顺序不能换

**风险排第一**：风控是**全局**判断。一个步骤检出验证码，
说明整个会话都处于风险中 —— 这时候"再试一次"不但没用，
还可能让情况更糟（连续触发风控 → 账号被限流）。

如果先判断重试次数，那么"第一次就撞上验证码"会被判成 `retry` ——
而重试只会再撞一次。

这条被测试钉住：

```
tests/test_phase6_recovery.py::TestDecisionOrder::
  test_risk_beats_retry_count
  test_risk_beats_unrecoverable
```

### 五种策略的含义

| 策略 | 什么时候用 |
|---|---|
| `retry` | 可能是暂态 |
| `retry_with_fallback` | 重试没用，得**换条路**（UIA 换坐标、换个选择器） |
| `skip` | 这步本来就不关键（等待、截图、滚动） |
| `pause_for_human` | 机器解决不了，叫人 |
| `abort` | 重试和换路都没意义 |

**`retry_with_fallback` 具体换什么路，是执行层的决定** ——
`recovery` 只说"该换路"，不说"换成哪条"。

---

## 6. 三条上限（硬约束 6）

| 上限 | 默认值 | 作用 |
|---|---|---|
| 单步重试 | 3 次 | 同一策略最多试几次 |
| 单步换策略 | 2 次 | 换了几条路之后就不再换了 |
| 全局超时 | 30 分钟 | 整任务的时间预算 |

**硬编码为常量，不是参数**（`MAX_RETRIES` / `MAX_STRATEGY_SWITCHES` /
`TASK_TIMEOUT`）。理由：这是**安全边界**，不是调优旋钮。
做成参数会诱使人"这次赶时间，把重试上限调到 100" —— 那等于没有上限。

`executor` 和 `recovery` 各有一份常量，**必须一致** ——
自检里有一条专门查这个（"两处不许分叉"）。

### 收敛性被测试钉住

模拟 20 次失败，必须收敛：

```
retry → retry → retry → retry_with_fallback → retry_with_fallback → pause_for_human
```

`tests/test_phase6_recovery.py::TestLimitsEnforced::test_converges`

---

## 7. 记忆与恢复

### 记什么

| 类别 | 内容 |
|---|---|
| 步骤状态 | 每步的 state / attempts / strategy_switches / elapsed |
| 事件流水 | 状态转换、观测、错误、**自愈决策**（按时间序） |
| 观测快照 | 执行前后各一次的 Observation（**摘要**，不是全量） |
| 计划快照 | `Plan.to_dict()`，用于断点恢复 |

### 硬约束 5：JSON，禁用 pickle

- **JSON** —— 人能直接打开看。排查问题时"能读懂存档"比"存得省空间"重要得多。
- **禁 pickle** —— pickle 反序列化会执行任意代码，而任务存档是
  **可能被外部写入**的文件（人手工改、别的版本写、备份还原）。
  读一个 pickle 等于执行那个文件里的代码。

### 原子写

`save()` 先写 `<id>.json.tmp` 再 `os.replace` 覆盖目标。
写到一半断电/被杀，不会留下半截 JSON。

### 断点恢复的语义

```python
out = ex.resume("task_id")
```

- **已成功的步骤不重跑**（`success` / `skipped` 保持）
- **失败/卡住的重新进入调度**（`failed` / `blocked` / `running` → `pending`）
- **自愈计数跨恢复累加** —— 不累加的话，"重启一次就重置重试次数"
  会变成一个无限重试的后门

### 路径安全

`task_id` 会被拼进文件路径（`~/.avatar/tasks/<task_id>.json`），
所以**必须校验**：

```python
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
```

不校验就等着 `../../` 这种路径穿越。

---

## 8. 能力边界

**做**：定义状态集合；校验并执行状态转换；给"能不能转"的纯查询；
记录状态与决策；从磁盘恢复；三条上限的收敛保证。

**不做**：

- **不决定该转到哪**。状态机只回答"能不能转"，
  "该不该重试"是 `recovery` 的判断。
- **不执行策略**。`recovery` 只回答"该走哪条路"，
  真的去重试/跳过/叫人 是 `executor`。
- **不计数**。`attempts` / `strategy_switches` 由执行器维护。
- **不监控超时**。全局超时是 `executor` 的事。
- **不做并发控制**。DAG 调度保证同一步骤不会被并行执行两次。
- **不清理任务存档**。`resume` 只读，删除要显式调 `delete_task`。
- **不硬解验证题**（硬约束 #9）。遇到验证题一律 `pause_for_human`。
