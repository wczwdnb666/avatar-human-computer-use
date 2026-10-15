# UIA 元素定位

> 阶段二的核心。把"屏幕上一堆像素"变成"一个名字叫『保存』的按钮"。

## 为什么必须自己封装 COM

候选方案是 `pywinauto` 或 `comtypes`。都否决了，理由不是"不信任开源"，
而是这三条：

1. **控不住生命周期。** UIA 里每个元素都是引用计数的 COM 对象。
   别人封装好了，你没法知道它在什么时候 `Release`。用久了必然泄漏，
   而且泄漏的现场（`E_OUTOFMEMORY` 或干脆崩进程）离原因很远。
2. **错误被吞掉。** `ctypes.HRESULT` 有个隐式行为：调用失败会抛
   `OSError`，而不是返回错误码。于是"探测某模式是否可用"这种**预期会失败**
   的调用，会突然变成一个异常，把正常的探测逻辑打断。
3. **拿不到真值。** `UIA_*PatternId` / `UIA_*PropertyId` 的取值、
   vtable 方法顺序，只能从 SDK 头文件抄。抄错了症状是
   `E_NOINTERFACE`（"模式明明检测到了，却取不到接口"）——
   不自己封装就查不到这一层。

所以 `_win32/` 下的 COM 全部手写。代价是有几千行样板代码，
换来的是每一处 `Release` 都可见、每一个 `HRESULT` 都能自定义处置。

## 分层

```
uia/find.py        定位：Query → 三级通道 → Element
uia/describe.py    描述：Element → 给人看 / 给模型看
uia/actions.py     动作：Element → 模式调用 → 键盘/鼠标兜底
uia/patterns.py    模式：Invoke / Value / Toggle / ... 的类型化封装
uia/tree.py        COM 层：IUIAutomation 的封装、遍历、属性读取
uia/ocr_fallback.py    WinRT OCR（手搓 ABI；位图通路受阻时**如实报不可用**）
uia/vision_fallback.py 模板匹配 / 颜色聚类 / 差分（纯 Python，无第三方库）
```

依赖是单向的：`actions → patterns → tree → _win32`。
`find` 依赖 `tree`；`describe` 只依赖 `Element` 这个纯数据对象，
所以它能在没有 COM 会话的情况下工作（方便写测试）。

## 一、Element 是值快照，不是活的指针

```python
class Element:
    name: str
    control_type: int
    automation_id: str
    ...
    hwnd: int      # 元素**自己**的顶层窗口（多数是 0）
    window: int    # 元素**所在**的顶层窗口（client_rect 用它）
    rect: Rect     # 屏幕物理像素 —— 快照，窗口一动就过时
```

`Element` 构造完就把底层 COM 引用放了。它不持有指针，
所以**不会**因为忘记释放而泄漏，也**不会**因为持有太久而变成野指针。

代价是它会过时。这不是缺陷，是设计：一个缓存的屏幕坐标比"重新读一次"
危险得多——窗口一移动，缓存坐标就指向了别的东西，而调用方毫不知情。

### 两个 hwnd 的区别

这是最容易搞混的地方：

| 字段 | 含义 | 什么时候非零 |
|---|---|---|
| `hwnd` | 元素**自身**的窗口句柄 | 只有元素本身就是个窗口时（如记事本主窗口） |
| `window` | 元素**所在**的顶层窗口 | 几乎总是有值 |

一个按钮的 `hwnd` 是 `0`，但它的 `window` 是那个记事本窗口。
`client_rect()` 用的是 `window` —— 拿 `hwnd` 去换算会算出垃圾。

## 二、坐标：只存客户区，屏幕坐标按需算

**元素从不持有屏幕坐标。**

```
Element.rect         屏幕物理像素（UIA 原生输出，快照）
Element.client_rect  换算到所属窗口客户区 —— 对外的主口径
```

要屏幕坐标，现算：

```python
sx, sy = layout.client_to_screen(el.window, *el.client_rect().center)
```

为什么不让元素直接存屏幕坐标？因为**窗口一移动，所有屏幕坐标全部作废**，
而客户区坐标不变。客户区坐标是"相对于窗口的语义位置"，它才是稳定的。

`describe.to_dict(..., include_screen=True)` 会现算一份屏幕坐标给你，
但它**不缓存**，每次调用都重算。

## 三、定位：三级通道

`find.py` 的核心是把"遍历整棵桌面树"变成"只走必要的那一小块"。

### 通道 A · 按窗口切分（顶层窗口查询）

从桌面根节点 `FindAll` 查 `control_type=window` 只能拿到 1 个元素——
原生 `FindAll` **不跨进程**。所以先走 Win32 `EnumWindows` 枚举顶层窗口，
再在每个窗口内部查。

### 通道 B · 原生下推（窗口内的元素查询）

```python
elements.FindAll(TreeScope.Descendants, condition, &array)
```

把过滤条件交给 UIA 自己去执行。这一条是本模块性能的关键。

> 实测：Python 侧全桌面遍历 **30.8 秒**；原生 `FindAll` 在窗口子树内
> **1.06～1.33 秒**。差 23 倍。

### 通道 C · Python 侧遍历（兜底）

条件太复杂、原生下推表达不了时，退回逐节点遍历。

三级通道的选择由 `_gather_candidates()` 决定。这里有个**必须区分的语义**：

```
_candidates_via_uia 返回 None  → "这个查询不适用原生下推"，走通道 C
_candidates_via_uia 返回 []    → "原生下推执行了，确实 0 个结果"，就此结束
```

早期把两者都返回 `[]`，导致一个真正零结果的查询会掉进通道 C 的
25 秒遍历里。区别只在这一个 `None`，但这是 25 秒和 1 秒的差距。

### 匹配强度与排序

`_matches_one()` 返回匹配强度，`_score()` 在其上叠加各项修正：

| 强度 | 条件 |
|---|---|
| 100 | 名称完全相等 |
| 80 | 名称前缀匹配 |
| 60 | 名称包含 |
| 50 | 正则匹配 |
| 40 | 仅文本内容匹配 |

排序修正：可交互控件加分、`enabled=False` 重罚（−25）、
`offscreen=True` 重罚（−30）、面积做对数惩罚（避免选到整个窗口容器）、
深度做惩罚（浅层优先）。

### Query

```python
Query(name="保存", control_type="button")
Query(automation_id="btnSave")        # 最稳
Query.by_name("登录")
Query(name="确定").any_of(Query(name="OK"))
```

> ⚠ `any_of` 这个类方法**不能**叫 `alternatives` —— 会和 `__slots__`
> 里的同名字段冲突（Python 报 `conflicts with class variable`）。

## 四、描述：两套受众

`describe.py` 服务两类读者，输出格式不同：

### 给人看

- `one_line(el, i)` —— 表格里的一行
- `describe(el)` —— 单个元素的完整详情
- `table(els)` / `tree(els)` —— 列表与树形
- `summarize(els)` —— 统计摘要

### 给模型看

- `to_dict(el)` —— 结构化字典
- `to_click_target(el, i)` —— **模型动作契约**，字段固定
- `to_prompt(els)` —— 拼成提示词片段
- `to_json(...)` —— 序列化（中文不转义成 `\uXXXX`）

`to_click_target` 里**不预置屏幕坐标**。模型只拿到
`index + name + type + client_rect + actions`，真正的坐标在点击那一刻现算。
预置坐标的话，模型排好计划到执行之间窗口动一下，就点歪了。

## 五、动作：三级执行阶梯

```
1. 控件模式   Invoke / Value / Toggle / SelectionItem / ExpandCollapse
2. 键盘/鼠标  （阶段三输入层，焦点 + 真实按键）
3. 坐标点击   必须显式 allow_coord=True
```

### 三条不可违反的规则

1. **永不猜坐标。** 定位失败就抛 `ElementNotFound`，不"大概在这附近"
   点一下。
2. **动作后要验证。** `expect` 参数给出期望状态，`_verify()` 轮询确认。
   不验证的话，"点了一下但没生效"会被当成成功。
3. **坐标一律现算。** `_center_screen_with_source()` 用
   `layout.client_to_screen` 实时换算，从不复用快照。

### 坐标兜底是显式 opt-in

```python
actions.click(el, allow_coord=False)   # 默认
```

默认为 `False`。坐标兜底的实现在阶段三的输入层，
阶段二只留了注册钩子：

```python
register_coord_fallback(fn)
```

没注册却请求坐标兜底 → 抛 `UnsupportedError(capability="coord_input")`。
宁可失败，不要静默点错地方。

### `_pattern_do` 的契约

```python
obj, ok = _pattern_do(el, "Invoke", "invoke", required=False)
if obj is not None and ok:     # ← 必须同时判两个
    ...
```

早期只判 `if ok is not None`，而 `ok=False` 时 `False is not None` 为真
—— 于是"模式不可用"被误判为"执行成功"。这是本模块最隐蔽的一个 bug。

## 六、降级链与如实报告

```python
from avatar import uia
uia.fallback_chain()
# [{'name': 'uia',    'available': True,  'reason': None},
#  {'name': 'ocr',    'available': False, 'reason': '...'},
#  {'name': 'vision', 'available': False, 'reason': '...'},
#  {'name': 'coord',  'available': False, 'reason': '...'}]

uia.has_fallback()   # False —— 阶段二只有 uia 可用
```

`has_fallback()` 问的是 **"UIA 之外还有没有兜底"**，它不包括 `uia` 自己。
阶段二返回 `False`，意思是：**UIA 一旦定位失败，这个任务只能放弃**。
上层规划阶段就该据此决定要不要接这个任务，而不是执行到一半才发现没退路。

这是能力**如实报告**，不是悲观。谎报 `True` 会让上层误以为有后备方案。

`available: False` 的每一环都必须给出 `reason` —— 空原因等于没解释。

## 七、踩过的坑

### `ctypes.HRESULT` 会自动抛 `OSError`

`HRESULT` 类型带 `_check_retval_` 钩子，失败的返回值会被转成 `OSError`。
这打破了"用 HRESULT 探测能力"的整个模式。

解法：模块内定义一个 `HRESULT_SAFE = ctypes.c_long`（**不**带钩子），
`ComPtr.method()` 内部静默替换 restype。对外仍声明 `HRESULT`，
所以 argtypes/restype 依然完整，只是运行时换个底。

### `TreeScope` 是 enum，不是指针

```python
# 错：传 NULL 指针 → E_POINTER (0x80004003)
fn(ptr, ctypes.c_void_p(scope), ...)

# 对：它是 int 枚举
fn(ptr, ctypes.c_int(scope), ...)
```

取值：`Element=0x1, Children=0x2, Descendants=0x4, Parent=0x8,
Subtree=0x7`。

### `get_CurrentBoundingRectangle` 不能用

vtable 43 号那个方法返回的是垃圾。必须走属性：

```python
GetCurrentPropertyValue(30001)   # 返回 VT_ARRAY|VT_R8
```

注意它是 `[left, top, width, height]`，**不是** ltrb。
用完 `SafeArrayDestroy` 释放，再把 `val.vt` 置 `VT_EMPTY`。

### 两套编号不能混用

| 表 | 取值 | 含义 |
|---|---|---|
| `VT_ELEMENT_*` | 3, 5, 6, 10, 14... | **vtable 方法索引** |
| `UIA_PROPERTY_IDS` | 30000 段 | **属性 ID** |

混用 → `E_INVALIDARG (0x80070057)`。

### `VT_BOOL` 是 short，`-1` 为真

不是 `1`/`0`，是 `-1`/`0`。判假写 `if not v` 会漏掉所有非零非 -1 的值。

### `IUIAutomationElementArray` 的引用计数

`FindAll` 返回的数组归我们所有，数组里的元素归数组所有。正确做法：

```python
arr = ComPtr(parr)          # 只建一个拥有者
for i in range(length):
    el = arr.method(VT_ARRAY_GET_ELEMENT, i)   # 每个元素 AddRef
    ...
arr.release()               # finally 里释放一次
```

早期在函数体内和 `finally` 里各建了一个拥有 `parr` 的 `ComPtr`
→ 双重 `Release` → 进程直接退出 127，**没有 Python traceback**。

### 模式接口要 `QueryInterface`

`GetCurrentPattern(pid)` 拿到的是 `IUnknown`，得再 `QueryInterface`
成对应模式的 IID 才能调它的方法。`PATTERN_IIDS` 表就是这个映射。

### `UIA_*PatternId` 有 5 个值曾经是编的

`Window` / `TableItem` / `SelectionItem` / `Table` / `LegacyIAccessible`。
症状：模式检测得到，`QueryInterface` 却 `E_NOINTERFACE`。

现在 `tools/dump_uia_vtbl.py` 能从 SDK 头文件解析出权威值，
用来核对。`tools/UIAutomationClient.h` 是配套数据文件。

## 八、性能参照

200% 缩放、单显示器、Windows 11：

| 操作 | 耗时 |
|---|---|
| 顶层窗口枚举（通道 A） | ~1.3s |
| 窗口内按钮查找（通道 B） | ~1.1s |
| 全桌面元素遍历（通道 C） | ~30.8s |
| 树遍历（40 个后代） | ~0.3s |
| `set_value` 写入 + 读回 | ~0.05s |
| 模式获取 | <0.01s |

通道 C 只该在条件复杂到原生下推表达不了时使用。默认路径是 A + B。

## 九、自检

```bash
python scripts/selfcheck.py --only uia     # UIA 相关模块自检
python tests/test_phase2_uia.py            # UIA 集成测试（会自己拉起记事本）
```

模块自检的返回结构是统一契约：

```python
{"checks": [...], "failures": [...], "ok": not failures}
```

**`ok` 键不能省。** 早期有 5 个模块漏了它，导致统一 runner 把它们
全判成"未知失败"。
