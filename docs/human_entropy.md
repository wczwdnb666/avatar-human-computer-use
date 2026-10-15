# 输入层与真人熵

阶段三的两个交付物：**输入层**（把动作变成真实的系统事件）和
**真人熵**（让这些事件在时序和轨迹上像人，而不是像机器）。

两者分开是有意的：输入层管"能不能做到"，真人熵管"像不像人"。
输入层可以独立工作（`human=False` 就是机械模式），真人熵只在
输入层之上做轨迹与节奏的编排。

---

## 一、输入层的唯一坐标系

**物理屏幕绝对坐标是输入层唯一的坐标系。**

这不是风格问题，是正确性问题。Windows 的 `SendInput` 在
`MOUSEEVENTF_ABSOLUTE` 下接受的是 **0..65535 的归一化坐标**，
反算回像素时系统实际按下面的公式：

```
x = round(nx * screen_width / 65536)
```

注意分母是 **65536**（2^16），不是 65535。`nx` 的满刻度
（65535）落在 `screen_width - 1` 附近，所以：

```python
# 正算（我们发出去的）
nx = (x * 65536 + w // 2) // w

# 反算（Windows 实际做的）
x = round(nx * w / 65536)
```

**用 `(x * 65535) // (w - 1)` 会引入系统性 +1px 偏差** ——
短距离看不出来，跨屏就明显。这条被硬约束 #3 钉住：
扫描 480 个采样点，往返误差必须为 **0px**。

### 为什么上层不能直接给屏幕坐标

三条铁律之二说白了：**元素只存客户区坐标**。
所以 `uia.actions` 拿到的永远是 `Element.client_rect()`，
进输入层之前必须经过 `layout.client_to_screen()` 换算。

`dispatch_click()` 里有一条测试专门钉这个（`test_phase3_actions.py`）：
它 spy 了 `A.dispatch_click`，断言传进去的是**客户区坐标**，
不是屏幕坐标。串了就红。

---

## 二、物理模式 vs 静默模式

| | 物理 `physical` | 静默 `silent` |
|---|---|---|
| 机制 | `SendInput` | `PostMessage` / `SendMessage` |
| 焦点 | 需要前台 | 不需要，可后台 |
| 光标 | 会移动 | 不动 |
| 要求 | 无 | 窗口可达、非最小化 |
| 延迟 | 真实 | 极低 |
| 风险 | 抢用户焦点 | 目标不处理消息就无效 |
| 默认 | 降级路径 | ✅ **默认** |

### 为什么默认静默

Avatar 的目标是"以人类形式操纵电脑"，但**不是**"抢走用户的鼠标"。
如果每次点击都要先抢前台，用户正在看的视频会暂停、正在输的字会断。

静默模式走 `PostMessage(hwnd, WM_LBUTTONDOWN, ...)`，
直接投递到目标窗口的消息队列，`lParam` 里打包客户区坐标：

```python
lparam = ((y & 0xFFFF) << 16) | (x & 0xFFFF)   # 低位 x，高位 y
```

### 静默模式的纪律：可达性必须验证

静默模式的致命问题是**它会静默失败**：窗口最小化了、
句柄失效了，`PostMessage` 照样返回成功，但什么都不会发生。

所以硬约束 #6：**静默前必须验证目标窗口可达**。

```python
dispatcher.check_window_reachable(hwnd)
# null_hwnd / invalid_hwnd / minimized / invisible → TargetUnreachable
```

不可达时**直接抛异常**，绝不闷声失败。是否降级到物理模式由
`allow_fallback` 决定，默认允许 —— 但降级会在
`ActionResult.fallback_used` 里留痕。

### 静默文本还有个坑：`WM_CHAR` 必须用 `SendMessage`

`PostMessage` 是异步的：消息进队列就返回，目标线程什么时候处理不知道。
`WM_CHAR` 尤其危险 —— 投递之后立刻读回文本，读到的是**旧值**，
于是你会以为输入失败了。

所以 `_silent_text()` 走 `SendMessageW`（同步），
写完再 `silent_get_text()` 读回比对。读不回就按 `dispatch_text`
的规则降级物理，并在返回值里说明。

---

## 三、真人熵：不是加噪声，是复现运动学

这条是阶段三最容易被做错的地方。**"加噪声"和"像人"是两回事。**

给一条直线轨迹叠上随机抖动，得到的是"颤抖的机器人"，
不是"人的手"。真人度体现在四个**独立的**机制上：

### 机制 1 · 贝塞尔路径（曲率）

人的手臂不是直线电机。抬手去够远处目标时，肩、肘、腕的复合运动
让轨迹略微外凸。`submove.plan_movement()` 用二次贝塞尔生成路径，
控制点的偏移量与距离成正比。

判据：**直度 < 1.0**（`straightness`）。机械轨迹恒等于 1.0。

### 机制 2 · 非对称速度曲线（双相模型）

Woodworth 1899 年的双相模型：人做快速定位动作时，
先有一个**冲刺相**（ballistic，不受视觉反馈控制），
后有**修正相**（corrective，受视觉反馈控制）。

表现：速度峰值出现在行程的 **40%–55%** 处，不是正中。
`kinetics.AsymmetricProfile` 实现这个形状：

- 距离 < 80px → `peak_position ∈ [0.46, 0.53]`（偏对称，谨慎）
- 距离 < 400px → `[0.42, 0.50]`
- 距离 ≥ 400px → `[0.36, 0.45]`（明显偏前，果敢）

判据：**非对称度 < 0.98**（`asymmetry` 越小越偏前）。
注意这条**单独**不足以区分真人与机械 —— 机械的对称曲线
也可能落在 0.98 以下。真正的分界是下面三条的组合。

### 机制 3 · 微震颤（8–12Hz 相关噪声）

手部有生理性震颤，主频 8–12Hz。**关键：震颤被运动掩盖** ——
移动越快，震颤幅度越小（不然就是帕金森，不是正常人）。

```python
tremor.amplitude_for_speed(speed, peak_speed=..., base_amplitude=1.2,
                           exponent=1.6)   # 单调递减
```

`tremor_series()` 生成的是**相关噪声**（多个谐波叠加 + 慢漂移），
不是白噪声。白噪声在频谱上不像手。

### 机制 4 · 高斯延迟（节奏，不是定时器）

打字间隔、点击前后的停顿，都不是固定值，也不是均匀随机。
实测分布是**右偏的对数正态**：大多数间隔很快，偶尔有一个长的。

```python
keyboard._inter_char_pause(char)   # 标点后更长
entropy.clamped_gauss(...)         # 有界的正态，不会出负值
```

`DEFAULT_WPM = 320.0`（对照数值，不是限制）。

### 判据：真人 vs 机械必须分得干净

`metrics.verdict()` 按 6 条 AND 判定，任一失败即"不像人"。
`metrics.PASS_CRITERIA` 是阈值表（测试直接引用这些常量，
避免两处硬编码）：

| 键 | 阈值 | 检查含义 |
|---|---|---|
| `min_peak_count` | 2 | 速度曲线至少 2 个峰 |
| `max_landing_error` | 2.0 px | 落点误差上限 |
| `min_landing_error` | 0.0 | `allow_no_landing_error=False` 时才查 |
| `min_straightness` | 0.85 | 直度下限 |
| `max_straightness` | 1.0 | **严格小于**才过（机械恒 == 1.0） |
| `max_asymmetry` | 0.98 | 加速段不能长于减速段 |

**为什么 `max_straightness` 用 1.0 而不是 0.9999**：
长距离直线移动天然能做到 0.99992，用四位小数判会
"仅仅因为路径绕了 0.14px"就判失败。真正的分界线只有一条 ——
路径长度是否**恰好等于**直线距离。用 1.0，误差留给浮点本身，
语义也干净："只要路径不是数学意义上的直线，就算过关"。

实测：**真人 400 条全通过，机械 400 条全不通过**。
机械**必然**失败的是 `peak_count>=2`（恒 1）和 `straightness<1.0`（恒 1.0），
`asymmetry<0.98` 视距离而定。分界干净。

---

## 四、可复现性：熵源纪律

硬约束 #4 和 #5：

> 所有随机源必须通过 `entropy.py` 统一入口，**禁止直接调用 `random.*`**。
> 同一 seed + 同一路径 = 完全相同的轨迹。

理由：真人化必然引入随机，而随机会让 bug 无法复现。
如果轨迹每次都不一样，"点歪了"这件事就永远查不出原因。

### 熵源纪律

```python
# 对
from . import entropy
d = entropy.uniform(0.85, 1.20)

# 错 —— 绕过可复现性
import random
d = random.uniform(0.85, 1.20)
```

`scripts/selfcheck.py` 用**源码正则**扫这 7 个模块，
出现裸 `random.` 调用就报 fail（硬约束 #4）。

### 独立流：`derive(tag)`

不同子系统各自用同一条流会互相干扰 —— 移动鼠标消耗的随机数
会影响打字节奏，于是"改一个 bug 动了别的地方"。

```python
rng = entropy.derive("mouse")     # 每次返回同一个持续状态的对象
rng.uniform(...)                  # 推进它，不影响主流
```

**踩过的坑**：最初的 `derive()` 每次都 new 一个同种子的 `Rng`，
于是连续 5 次调用返回 **5 个相同的值** —— 不是随机流，是常数源。
修法是引入 `_DerivedRng`（`derive()` 返回自身，幂等）+ `(base, tag)` 缓存，
`seed()` 时清缓存。

### 验证：逐点比对

硬约束 #5 的检查不看统计量，直接**逐点比对**：

```python
entropy.seed(12345)
a = [plan_movement(...).flat()[i] for i in range(30)]
entropy.seed(12345)
b = [plan_movement(...).flat()[i] for i in range(30)]
assert a == b            # 30 个点逐点一致
```

统计量对得上但点对不上 = 不可复现。只有逐点才算。

---

## 五、`count_peaks` 与"峰位判据"这个坑

真人度判定依赖数速度曲线上的峰。这件事看着简单，实际翻过车。

### 错误的判据：相对全局最大峰

```python
threshold = max(v) * 0.08        # ❌
```

720px 的真人轨迹：主峰 8300，修正峰 559，两侧谷是 1 和 0 ——
**559 是货真价实的峰**，但 `559 / 8300 = 0.067 < 0.08`，被筛掉了。
40 个 seed 漏判 8 条。

根因：全局阈值会自动丢弃"小但有意义的峰"，而修正相本来就是小峰。

### 正确的判据：相对候选峰自身

```python
keep = val * min_prominence       # ✅ 相对峰自身高度
if (val - left_low) >= keep and (val - right_low) >= keep: 保留
```

同时要求**两侧谷校验**（左谷、右谷都要显著低于峰），
避免把一个宽峰的"平台边缘"当成两个峰。

参数定稿：`min_prominence=0.10`、`smooth=2`。

`_savgol_smooth` 的窗口值做过扫描：窗口 4 在四条行程上都更差
（短距 7/40、中短 13/40 漏判），窗口 2 最优。

验证：改后**四种行程 160 条真人轨迹零漏判，机械仍全拒**。

---

## 六、`duration_for_distance` 的 `min_dist` 折算

`min_dist` 的用途：修正相和微调段的位移**必然**小于主运动，
但不能因此让它们快得像机器。传 `min_dist=主运动距离` 告诉它
"参照这个尺度，别按绝对像素算"。

这个分支翻过三次车，值得记下来。

### 坑 1：基数错配

参考尺度用**默认参数**算，而调用方带着自己的 `base` / `per_pixel` 进来：

```
ref_dur = duration_for_distance(ref)                  # base=0.085
实际调用 = duration_for_distance(d, base=0.075, ...)   # 基数不同
```

结果 `min_dist` 折算后的值比不传还小（0.079 vs 0.107）。

**修法**：参考尺度用调用方自己的 `base` / `per_pixel`。

### 坑 2：非单调

试过至少五个版本，全部非单调：

| 方案 | 问题 |
|---|---|
| 幂函数版 | 崩到硬下限 0.03 |
| 纯对数版 | `d ≪ ref` 处递减 |
| 对数 + 下限 | d=40 只有 0.066s |
| 振幅倍数版 `amp = 1 + k(ref/d - 1)` | 非单调 |
| 向 `FLOOR * scale` 插值 | 非单调 |

**定稿**：

```python
peak   = base + per_pixel * (ref ** 0.85)
folded = max(peak * min_ratio, peak * ((d / ref) ** shape))
# min_ratio = 0.78, shape = 0.10
```

验证：`ratio@100px = 1.514 > 1.5`、3→700px 全程单调、
`d == min_dist` 处无缝接合、`min_dist < d` 时退化为不传（逐位相同）。

### 坑 3：抖动次数不一致

`duration_for_distance` 末尾乘 `uniform(0.85, 1.20)`。
如果折叠分支内部也消耗随机数，那么走折叠和不走折叠的调用
**消耗的随机数次数不同** → 同 seed 下结果不一致。

**修法**：只在**最终值上抖一次**，中间量保持确定性。

### 单点采样不保证单调（自检写法上的坑）

即使曲线本体单调，`duration_for_distance(d)` **单次调用**也不单调 ——
末尾 35% 幅度的抖动远大于相邻距离的增量。

所以自检里断言单调性必须用**大样本均值**：

```python
entropy.seed(99)
t = sum(duration_for_distance(d) for _ in range(200)) / 200   # ✅ 均值
```

用单点会误报"单调性破了"，这是测试的错，不是代码的错。

---

## 七、`ActionResult`：静默失败是最大的敌人

硬约束 #7：**每个动作必须返回 `ActionResult`**，
`method` / `mode_used` / `fallback_used` **全部必填**。

```python
@dataclass
class ActionResult:
    ok: bool
    method: str          # 怎么做的："uia_invoke" / "silent_click" / "physical_move"
    mode_used: str       # 实际用了哪个模式："silent" / "physical"
    fallback_used: bool  # 有没有降级
    detail: str = ""
    point: Optional[Tuple[int, int]] = None
    hwnd: int = 0
    duration: float = 0.0
    extra: dict = field(default_factory=dict)
    error: Optional[str] = None
```

### 为什么这三个字段必填

因为"动作看起来成功了但其实没有"是这类库最常见的 bug 形态：

- 静默投递成功，但窗口最小化 → 什么也没发生
- 降级到物理，但焦点没抢到 → 点到了别的窗口
- UIA `Invoke` 返回成功，但控件是禁用状态

`mode_used` 告诉你**实际发生了什么**，而不是**你打算做什么**。
`fallback_used=True` 是"我本来想静默，但不得不动真格"的意思。

`scripts/selfcheck.py` 的硬约束 #7 检查这几个字段
**没有默认值** —— 有默认值就意味着调用方可以不小心漏掉。

---

## 八、文件清单

| 文件 | 职责 |
|---|---|
| `input/keyboard.py` | 虚拟键映射、`SendInput` 键事件、`type_text`、`hotkey` |
| `input/mouse.py` | 绝对坐标归一化、`SendInput` 鼠标事件、点击/滚动/拖拽 |
| `input/dispatcher.py` | 模式管理、静默/物理分发、`ActionResult`、可达性检查 |
| `human_entropy/entropy.py` | 统一随机源、可复现种子、`derive(tag)` 独立流 |
| `human_entropy/kinetics.py` | 非对称速度曲线、时长模型、`min_dist` 折算 |
| `human_entropy/tremor.py` | 8–12Hz 相关噪声震颤、幅度-速度关系 |
| `human_entropy/submove.py` | 子运动分解（主运动 + 修正 + 微调）、贝塞尔路径 |
| `human_entropy/mouse.py` | 真人化鼠标动作（移动/点击/拖拽/滚动） |
| `human_entropy/keyboard.py` | 真人化打字（对数正态间隔、邻键误触） |
| `human_entropy/metrics.py` | 轨迹指标、峰值检测、真人度判定 |

---

## 九、验证

```bash
# 模块自检（含 7 个熵模块的 selfcheck）
python scripts/selfcheck.py --only human_entropy

# 硬约束（8 条，含归一化 480 点扫描、同 seed 逐点比对）
python scripts/selfcheck.py --list        # 看硬约束清单

# 四个测试文件
python tests/test_phase3_input.py         # 27 项
python tests/test_phase3_entropy.py       # 46 项
python tests/test_phase3_dispatcher.py    # 21 项
python tests/test_phase3_actions.py       # 14 项
```

当前状态：**108/108 通过，模块自检 7/7，硬约束 8/8。**

---

## 十、真人熵的实测对比

想看机械和真人的差别，可以直接跑：

```python
from avatar.human_entropy import entropy, submove, metrics

entropy.seed(42)
human = submove.plan_movement(100, 100, 820, 620, human=True)
entropy.seed(42)
robot = submove.plan_movement(100, 100, 820, 620, human=False)

print(metrics.analyze_move(human).describe())
print(metrics.analyze_move(robot).describe())
```

典型输出差异（1440×900 屏，seed=42，100,100 → 820,620）：

| 指标 | 真人 | 机械 |
|---|---|---|
| `peak_count` | 5 | 1 |
| `straightness` | 0.9922 | 1.0 |
| `asymmetry` | 0.1364 | 0.9375 |
| `tremor_energy` | 0.1793 | 0.0 |
| `submovement_count` | 3 | 1 |
| `duration` | 0.6567s | 0.418s |
| `path_length` | 895.12 | 888.14 |

真人轨迹在速度曲线上的形态：一个主峰（冲刺），
后面跟若干小峰（视觉修正 + 微调）。机械只有一次冲到底的单峰。

> ⚠ `asymmetry` / `straightness` 的具体数值随距离和 seed 变化，
> 不要把它们当固定常数。稳定的判据是
> **`peak_count >= 2`、`straightness < 1.0`、`tremor_energy > 0`**
> 这三条 —— 机械轨迹恒为 `1 / 1.0 / 0`。
