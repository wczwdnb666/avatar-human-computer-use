# 执行结果验证机制（双端）

> 阶段四的后半。核心只有一句：**每个动作都必须有明确的成功判据，
> 且判据不足时要敢说"不知道"。**

## 为什么需要这一层

阶段一到三把"怎么操作"解决了：UIA 能找到控件、输入层能真人化地
移动鼠标、CDP 能在浏览器里执行 JS。

但这些都只回答了"我发出了一个动作"。**没有回答"它成功了吗"。**

不回答这个问题的后果不是"偶尔失败"，而是**失败没法被发现**：

- 点了一个按钮，它没反应 —— 上层以为成功，继续走下一步，
  然后在第三步炸掉，错误现象离原因很远。
- 点了一个按钮，它反应慢 —— 上层以为失败，重试，结果点了两次，
  变成"提交了两遍订单"。

更隐蔽的第三种：**根本判不出来**。页面在加载、控件在动画、
截图抓到的是一片灰。这时候硬判"成功"或"失败"都是错的。

所以这一层的设计目标不是"更准"，而是**把"不知道"当成一等公民**。

## 三态，不是 bool

```python
VERIFY_OK      = "ok"       # 确认成功
VERIFY_FAIL    = "fail"     # 确认失败
VERIFY_UNKNOWN = "unknown"  # 无法判定
```

`VerifyResult` 是唯一的返回类型。**禁止裸 bool。**

### 为什么 bool 不够

一个 bool 必须把"不知道"塞进 `True` 或 `False`，两种选择都是错的：

| 塞进 | 后果 |
|---|---|
| `True`（乐观） | 假装成功，错误往下传播 |
| `False`（悲观） | 触发无谓重试，可能造成重复操作 |

而"不知道"恰恰携带了最重要的信息：**该重试**。

### 三态与重试的对应

```
ok      → 停，成功
fail    → 停（默认），判据确定性不成立，重试没意义
unknown → 重试（默认），多半是时序问题，等一会儿再试就好
```

这个映射是整个验证层的价值兑现点。见下面"重试"一节。

### 陷阱：`__bool__` 返回 `ok`

```python
class VerifyResult:
    def __bool__(self):
        return self.ok
```

于是 `unknown` 是 **falsy** 的。后果：

```python
# ✗ 错：unknown 走 else，验证结论被丢掉
reason = out.verify.reason if out.verify else None

# ✓ 对：显式判 None
reason = out.verify.reason if out.verify is not None else None
```

这个坑真实发生过 —— `retry.py` 的 selfcheck 里有一条断言长期失败，
探针显示同一行里 `repr(out.verify)` 有 reason，而
`out.verify.reason if out.verify else None` 是 `None`。
查了很久才定位到 `__bool__`。

`tests/test_phase4_verify.py::test_retry_outcome_carries_verify`
专门守着这条。

### 陷阱：`__slots__` 让属性遮蔽同名类方法

```python
class VerifyResult:
    __slots__ = (..., "unknown", ...)      # 属性名

    @classmethod
    def unknown(cls, **kw): ...            # ✗ 被 slot descriptor 遮蔽
    @classmethod
    def unknown_(cls, **kw): ...           # ✓ 带下划线
```

构造方法叫 `unknown_`（带下划线），构造方法本身也需要注意：
`ok_` 而不是 `ok`（`ok` 是属性）。

## 统一入口，内部分流

桌面和 Web **必须**走同一个 `verify_action`：

```python
verify_action("auto",     ...)   # 自己选路
verify_action("desktop",  ...)   # 强制桌面
verify_action("web",      ...)   # 强制 Web
```

分成两个入口的话，调用方就得自己判断"这是桌面还是网页" ——
而这个判断恰恰最容易出错（Electron 既是也不是）。

路由依据是**判据键**：给了 `selector` / `url_contains` 就是 Web，
给了 `hwnd` / `uia_target` 就是桌面。`prefer` 参数控制优先级。

### 无论内部怎么炸，返回类型不变

```python
try:
    ...
except Exception as exc:
    return VerifyResult.unknown_(
        method=route, reason="验证过程异常：%s" % exc, error=str(exc))
```

内部异常一律转成 `unknown` + `error` 字段。**绝不往外抛**
（除了 `RouteError` —— 那是编程错误，不是运行期未知）。

## 判据不足 → unknown，不是猜

```python
verify_action("desktop")            # 什么都没给
# → unknown, reason="没能确定判据（缺少必要参数）"
```

如果这里返回 `fail`，上层会去重试一个根本不可能成功的东西；
返回 `ok` 更糟 —— 假装成功。

同理，**目标不存在**也是 `unknown` 而不是 `fail`：
可能是元素还没出现（该等），也可能是判据写错了（该改）。
分不清就说分不清。

## 桌面端判据

| 判据 | 方法 | 说明 |
|---|---|---|
| 像素变化 | `phash` | 截图 pHash 距离 ≥ 阈值 |
| 窗口标题 | `window_title` | `contains` / `exact` / `regex` |
| 控件存在 | `uia_exists` | |
| 控件值 | `uia_value` | 精确 / contains / regex / nonempty / any |
| 控件状态 | `uia_state` | enabled / visible / focused / toggled / expanded |

### pHash 的两个必须知道的事实

**一、它是 63 位，不是 64。**

跳过 DC 系数（`[0][0]`）。DC 是整块的灰度均值乘以常数，**永远**
远大于其它所有系数 —— 留在里面的话无论图是什么样那一位恒为 1，
等于白占一位，还让纯色图（本该是"无结构"）变成一个非零值。

**二、它只关心低频，细密纹理的图会全部撞到 0。**

`phash` 内部先把**任意尺寸**用最近邻缩放到 `DCT_SIZE = 16`，
只保留 8×8 低频块。所以图案周期必须是源图尺寸的 1/16 量级才看得见。

实测（源图 256×256）：

| 图案 | pHash | 与纯色的距离 |
|---|---|---|
| 全黑 | 0 | — |
| 四象限明暗相反 | 0x1100... | **8** |
| 大面积渐变 | 0x2A61... | **31** |
| 周期 32px 棋盘格 | 0 | **0** |
| 周期 32px 竖直条纹 | 0 | **0** |
| 周期 32px 对角纹 | 0 | **0** |

所以：**"屏幕明明变了却没检出"时，先怀疑被测画面本身是细密纹理**
（加载动画、噪点背景、扫描线），而不是立刻改阈值。

### ⚠ UIA 判据有个 30 倍的成本悬崖

这是最容易踩、也最贵的坑。**同样的 `--expect-exists`，可能 2 秒，
也可能 53 秒。**

`find` 有三条候选通道，选路规则是"条件能下推给 UIA 原生 `FindAll`
就走 B，下推不了就走 C"：

| 通道 | 手段 | 耗时量级 |
|---|---|---|
| A | Win32 `EnumWindows` | 毫秒（找窗口） |
| B | UIA 原生 `FindAll`（属性等值下推） | 1~2 秒 |
| C | Python 全遍历 | **数十秒** |

真机实测（多屏 2560 宽，元素 4000+）：

```
find_all(Query("xxx", name_exact=True), window=fg)    →   1.75s   ← 通道 B
find_all(Query("xxx", name_exact=True), scope=root)   →   2.09s   ← 通道 B
T.descendants(root)                                   →  52.29s   ← 通道 C
```

问题在于：**"字符串按名字子串找"恰好是下推不了的那一类**。
`_candidates_via_uia` 一看到「有 name 但 `name_exact=False`」就直接
`return None`（表示"我表达不了"），于是掉进通道 C。

于是 `avatar verify --target "某个中文控件名" --expect-exists`
就会卡 50 秒 —— 而且此时 `--timeout 2` **完全无效**：

> 轮询的外层预算拦不住**单次查询自己就超预算**的情况。
> 一次尝试跑 52.8 秒，`while` 循环根本没机会判断 deadline。

**修法：给三个"收紧旋钮"，把条件变回可下推的形状。**

| 旋钮 | 效果 |
|---|---|
| `--name-exact` | 名字完全相等 → 走 Name 属性下推 |
| `--control-type button` | 限定类型下推，再在结果内子串过滤 |
| `--hwnd <窗口>` | scope 缩到单窗口（几十元素 vs 全桌面几千） |

```bash
# 慢（50s 级）：宽松子串 + 全桌面
avatar verify --mode desktop --target "保存" --expect-exists

# 快（2s 级）：收紧成可下推
avatar verify --mode desktop --target "保存" --expect-exists --name-exact
avatar verify --mode desktop --target "保存" --expect-exists --control-type button
```

**可观测性**：`evidence` 里永远带 `probes` / `probe_seconds`。
看到 `probe_seconds` 远大于 `timeout`，就是撞上了这个悬崖 ——
此时还会附一条 `probe_note` 明说「单次查询耗时超过 timeout，
轮询预算未生效」。**不许沉默地慢**，这是三态原则在性能上的延伸。

> 顺带一个真实误匹配：`--target "不存在的元素zzz"` 返回了 ok。
> 因为那条命令本身把字符串打在了终端窗口上，UIA 的宽松子串匹配
> 就把那行文本捞了出来（匹配到的是"终端里的文本控件"）。
> **验证中文目标时，`--name-exact` 或 `--hwnd` 几乎是必须的。**

### 灰区：宁可说不知道

```python
DEFAULT_UNCHANGED_MAX = 5    # ≤5 认为没变
DEFAULT_CHANGED_MIN = 6      # ≥6 认为变了
```

距离落在 `unchanged_max < d < threshold` 之间时返回 **unknown** ——
这时噪声和真实变化概率相当，硬判会制造假阳性，
而假阳性会把重试逻辑引向错误方向。

阈值可调，灰区宽度也可调（见 `images_differ` 的 `threshold` /
`unchanged_max` 参数）。

## Web 端判据

| 判据 | 方法 |
|---|---|
| DOM 存在/可见 | `dom` |
| 文本内容 | `dom_text` |
| 表单值 | `dom_value` |
| URL 变化 | `url` |
| 页面标题 | `title` |
| 网络请求 | `network` |

Web 端天然比桌面端可靠：`document.querySelector` 要么找到要么找不到，
不需要猜。所以 Web 端的 `unknown` 主要来自"连接断了"
和"页面还在导航"这两类。

## 组合

```python
verify_all([...])   # AND：有一个 fail 就 fail
verify_any([...])   # OR：任一 ok 就 ok
```

### `verify_any` 的严格语义

```
[unknown, fail] → unknown     ← 不是 fail
[fail,    fail] → fail
[]              → unknown     ← 空列表不是"全失败"
```

只要有一支是 `unknown`，整体就是 `unknown`。

理由：分支 B 失败了，不代表分支 A 的条件也失败 —— 它只是"没测出来"。
报成 `fail` 会让上层以为"确定不成立"，进而放弃重试或误判业务失败。

## 重试

```python
out = retry_call(func, *args, verify=..., policy=RetryPolicy(...))
```

`retry_call` **不向外抛异常** —— 返回 `RetryOutcome`，
里面有 `ok` / `value` / `verify` / `attempts` / `attempt_log` /
`gave_up_reason`。这样重试的"全过程"才是可记录的。

### 默认策略的语义

| 情况 | 默认 | 为什么 |
|---|---|---|
| 异常 | 重试 | 多半是暂时的 |
| `fail` | **不重试** | 判据确定不成立，重试只是浪费时间 |
| `unknown` | **重试** | 多半是时序问题（页面没渲染完、窗口在动画） |

`fail` 不重试要显式开 `retry_on_fail=True`。

### deadline 的两个要点

**一、它走真实的 `time.time()`，不是注入的 sleep。**

```python
retry_call(..., sleep=lambda s: None)   # 只是不真等，deadline 照旧按真实时钟算
```

所以测 deadline 必须 `monkeypatch` 掉 `retry.time.time`，
光喂假 sleep 不管用。（这个坑在 `tests/test_phase4_verify.py`
里花了一轮才定位到。）

**二、检查点在两次尝试之间，不打断正在跑的 func。**

意味着一个卡死的 func 不会被 deadline 救回来 —— 超时得靠
func 自己的 timeout 参数处理。

### `RetryOutcome` 没有 `error` 字段

失败信息在 `gave_up_reason` 里（可读文本，如"超过总时限 0.5s"）。
`attempt_log` 是每次尝试的记录。

## 与真人化的接合

`human_entropy/verify_hook.py` 把真人化落点和验证连起来。

真人化鼠标的落点有随机抖动，所以存在**偏差**。三档判定：

| 级别 | 条件 |
|---|---|
| `exact` | 末端落点在容差内 |
| `corrected` | 超容差，但轨迹**末端收敛**（后半段方差 < 前半段） |
| `drifted` | 超容差且不收敛 |

`corrected` 算成功 —— 真人也有"先冲过头再修正"的动作。
只看终点距离会把这种正常行为判成失败。

```python
MECHANICAL_LANDING_TOL = 1.0   # 机械化落点容差
HUMAN_LANDING_TOL      = 5.0   # 真人化落点容差
```

拿不到轨迹（`move_result` 里没有点序列）时 → `drifted`，
不是 `unknown`：这里的分工是"**拿不到数据**是 drifted"，而
`unknown` 留给"验证逻辑本身判不出来"。

## 钩子

`verify/hook.py` 提供全局钩子，让动作层**自动带上验证**：

```python
from avatar.verify.hook import set_verify_hook, set_enabled
from avatar.verify import auto_verify_hook

set_verify_hook(auto_verify_hook)
set_enabled(True)
```

**默认关闭。** 打开后：
- 动作成功 → 按 `ctx["suggest"]` 自动构造判据并验证
- 动作失败 → 跳过（没意义）
- 动作没给 suggest → 跳过
- `run_verify_hook` **永不抛异常**（除 KeyboardInterrupt）

`human_entropy` 侧还有 `install_default()`，它把真人化上下文
（容差、是否真人化）搬进 `res.evidence["human_context"]` ——
这样验证结论里能看出"这次落点判定用的是哪档容差"。

## CLI

```bash
# 桌面：控件存在性
python -m avatar.cli verify --mode desktop \
    --target "保存" --expect-exists

# 桌面：窗口标题
python -m avatar.cli verify --mode desktop --expect-title "WorkBuddy"

# 桌面：控件存在（**务必加 --name-exact 或 --hwnd**，否则可能 50 秒级）
python -m avatar.cli verify --mode desktop --target "保存" \
    --expect-exists --name-exact

# Web：URL 变化
python -m avatar.cli verify --mode web --port 9222 \
    --url-contains "example.com"

# 机器读
python -m avatar.cli --json verify --mode desktop --expect-title "WorkBuddy"
```

`--json` 输出完整结构：`verdict` / `ok` / `unknown` / `method` /
`reason` / `evidence` / `confidence` / `elapsed`。

> ⚠ `--json` 必须放在**子命令之前**（`avatar --json verify ...`），
> 它是全局参数。并且此模式下 stdout **只有 JSON**，没有任何
> 标题栏/分隔线 —— 保证 `json.loads(stdout)` 直接可用。

**只给 `--expect-title` 时自动取前台窗口** —— 不用手抄 hwnd。

退出码：`0` = ok，`5` = fail，`11` = unknown。

## 自检与测试

```bash
python -m avatar.verify                                   # 10 项契约检查
python scripts/selfcheck.py --only verify                  # 阶段四硬约束
python -m pytest tests/test_phase4_verify.py -q            # 70 项
```

模块级 selfcheck（`scripts/selfcheck.py` 会全跑）：

| 模块 | 项数 |
|---|---|
| `verify.result` | 124 |
| `verify.desktop` | 95 |
| `verify.web` | 91 |
| `verify.action` | 67 |
| `verify.retry` | 84 |
| `verify.hook` | 36 |
| `human_entropy.verify_hook` | 52 |

### 性能注意

`verify.desktop` 的 selfcheck 曾经跑 **15 秒**。profile 显示
`verify_uia_state` 被调 14 次、共花 12 秒，全是 `time.sleep` 轮询 ——
selfcheck 里没传 `timeout`，吃满了默认的 2 秒。

给 selfcheck 里所有 UIA 调用补上 `timeout=0.2` 后降到 4 秒。

**教训**：selfcheck 的定位是"随手跑"，一旦某项跑到十几秒，
人就不愿意跑了，等于没有。任何带轮询的验证函数在 selfcheck 里
都必须显式传小 `timeout`。

## 踩过的坑汇总

| 现象 | 真因 |
|---|---|
| `out.verify.reason` 是 None 但 `repr()` 有值 | `__bool__` 返回 `ok`，unknown 是 falsy |
| `TypeError: 'str' object is not callable` | 形参名 `verify_window_title` 与模块级同名函数互相遮蔽 |
| `verify_any([unknown, fail])` 返回 fail | 严格语义应返回 unknown（已修正） |
| 断言消息里有中文引号导致 `SyntaxError` | `"""里面有"引号"会截断字符串"""` |
| `verify_web(url_contains=...)` 报参数不存在 | 该路径的参数叫 `body_contains` |
| `image_similarity` 对细密纹理恒返回 1.0 | pHash 塌到 0，属设计内行为 |
| `verify --expect-exists` 跑 50 秒，`--timeout 2` 无效 | 字符串目标走通道 C 全遍历，单次查询就超预算 |
| 加了 `--name-exact` 却仍然 50 秒 | `verify_desktop` 签名里没有该形参，参数被静默吞掉 |
| `--target "不存在的元素zzz"` 返回 ok | 命令自己把该字符串打在终端上，被宽松子串匹配捞到 |
| `avatar verify --json` 报 `unrecognized arguments` | `--json` 是**全局**参数，要放在子命令**之前** |
| `--json` 的 stdout 被标题栏污染，`json.loads` 失败 | `_hr()` 在 json 分支之前无条件打印（已修正） |
