# 五层视觉降级链

> "找到那个按钮"这件事，**没有一种方法能覆盖所有情况**。
> 这份文档讲的是**为什么这么排、每层怎么判、失败怎么解释**。

最后更新：阶段七 7C / 7I（2026-10-07）

---

## 1. 五层，按成本排序

```
UIA  →  CDP  →  OCR  →  模板匹配  →  视觉大模型
~2ms   ~20ms   ~300ms   ~500ms       ~3s
```

| 层 | 能看见什么 | 看不见什么 |
|---|---|---|
| **UIA** | 暴露了无障碍信息的控件 | 自绘界面、canvas、游戏 |
| **CDP** | 网页 DOM（最准最快） | 非浏览器 / 没开调试端口 |
| **OCR** | 屏幕上的**文字** | 图标、图形、纯色按钮 |
| **模板匹配** | 长得像模板图的区域 | 界面改版后失效 |
| **视觉大模型** | 什么都能认 | **最慢最贵**（阶段八实现） |

### ⚠ 顺序不能换

**按成本排，不按准确率排。**

贵的层先跑等于每次查找都付最贵的代价。代价是后面几层
很少有机会跑到 —— **那正是目的**。

---

## 2. 两个硬要求（硬约束 4）

### 2.1 必须按序执行

一层失败才降级到下一层。**不是并行赛跑。**

并行会让"为什么用了这层"变得不可解释，而且贵层会白白消耗资源。

### 2.2 每层都要记

```python
result.attempts   # [LayerAttempt, ...]
```

每条记录带：

- `ok` / `skipped` —— 成功了吗 / 被关掉了吗
- `reason` —— **为什么失败**（这是重点）
- `elapsed` —— 花了多久
- `rect` / `confidence` —— 命中时的结果

**第二条比第一条重要**：出问题时你要能回答"为什么走了这么远"。
只记"最后用了哪层"是不够的 —— 那看不出中间几层为什么失败。

```
$ python -m avatar vision chain --text "确定"

查找过程（按成本从低到高）：

✗ UIA 语义     1228.0ms  UIA 树里没有「确定」
        ↳ UIA 树里没有「确定」
✗ CDP 网页        0.0ms  没有 CDP 会话
        ↳ 没有 CDP 会话
...
```

---

## 3. 每层都可关

```python
VisionChain(
    allow_uia=True,       # 关掉可以单独测后面几层
    allow_cdp=False,      # 没有浏览器时省一次探测
    allow_ocr=True,
    allow_template=True,
    allow_llm=False,      # ★ 默认关（最贵）
)
```

关掉的层在 `attempts` 里标记为 `skipped` —— **不是消失**，
而是明确写"本层已关闭"。这样"为什么没用 OCR"有答案。

---

## 4. 返回什么

```python
@dataclass
class LocateResult:
    ok: bool
    rect: Optional[Tuple[int, int, int, int]]   # 客户区坐标
    method: str          # 最终用了哪层
    confidence: float
    fallback_used: bool  # 降级过吗（第一层就命中 = False）
    reason: str
    attempts: List[LayerAttempt]   # ★ 完整尝试记录
    elapsed: float
```

**坐标一律客户区口径** —— 与 `uia.Element` / `ocr.OcrHit` 一致。
不提供绝对屏幕坐标：那会因窗口移动而静默失效。

---

## 5. 每层的"没有输入"要有明确原因

一个常见的坏设计是：某层因为没参数而失败，却报"没找到"。
那会让排查变成猜谜。

本项目的做法：**每层对缺什么说得清清楚楚**。

| 层 | 缺什么 | reason |
|---|---|---|
| UIA | 没控件名 | `没有控件名可查` |
| CDP | 没会话 | `没有 CDP 会话` |
| CDP | 没选择器 | `没有 CSS 选择器` |
| OCR | 没文字 | `没有文字可查` |
| OCR | 没窗口 | `没有窗口句柄（OCR 需要窗口来换算坐标）` |
| 模板 | 没模板图 | `没有提供模板图` |
| 模板 | 没窗口 | `没有窗口句柄` |

---

## 6. 第五层：视觉大模型（阶段八已实现）

**完整设计见 [docs/vision_llm.md](vision_llm.md)。** 这里只讲它在链条里的位置。

### 五层完整对比

| 层 | 耗时 | 成本 | 能看见什么 | 失败时 |
|---|---|---|---|---|
| **UIA** | ~2ms | 0 | 暴露了无障碍信息的控件 | 降级 |
| **CDP** | ~20ms | 0 | 网页 DOM（最准） | 降级 |
| **OCR** | ~300ms | 0 | 屏幕上的**文字** | 降级 |
| **模板匹配** | ~500ms | 0 | 长得像模板图的区域 | 降级 |
| **视觉大模型** | ~3s | **~1400 token/次** | 什么都能认 | **链条终止** |

### ⚠ 第五层失败 → 链条终止（**不降级到盲点**）

前四层失败是"换个方法再试"，第五层失败是**"我们不知道目标在哪"**。

这时候**不能**：

- 用模型给的原始坐标凑合（那是盲点）
- 返回一个"最接近的"候选（那是猜）
- 抛异常（调用方还要继续）

**返回 `None` + 详细原因。**

### 启用方式

```python
from avatar.vision.chain import VisionChain
from avatar.vision import llm_backend as LB, cost as CO

chain = VisionChain(
    allow_llm=True,                                    # ★ 默认 False
    llm_backend=LB.get_vision_backend("deepseek", enabled=True,
                                      api_key="..."),
    cost_budget=CO.CostBudget(),                       # 单次 2000 / 累计 20000
    cache=CO.ResultCache(),                            # 键 = 截图 hash + 目标
)
r = chain.locate("确定按钮", window=hwnd)
print(r.explain())          # ★ 逐层说明
print(r.cost)               # 花了多少 token
```

### 结果里多了两个字段

```python
r.cost          # 这次查找花掉的 token（只有走了第五层才有）
r.validation    # 第五层的闸门记录（哪道过了、哪道没过）
```

### CLI

```bash
# 只看请求构造（**不发真实请求**）
python -m avatar vision llm --live --goal "确定按钮"

# 真调用
python -m avatar vision llm --live --goal "确定按钮" --enabled

# 成本 / 缓存
python -m avatar vision llm --cost
python -m avatar vision llm --clear-cache
```

---

## 7. 纯 Python 匹配算法（第四层的基础）

`uia/vision_fallback.py` 里有四个不需要引擎的函数：

### `find_template(haystack, needle, ...)`

用**归一化互相关（NCC）**，不是逐像素差。

**为什么**：逐像素差对**亮度变化**极其敏感 —— 界面换了个主题、
或者有个半透明遮罩，就废了。NCC 先减均值再算相关，
对线性亮度变化免疫。

⚠ **纯色图永远匹配不上**（方差为 0）。那是**正确行为**：
一块纯色没有"特征"可匹配，说它匹配是胡说。

### `find_template_multi(..., scales=(1.0,), suppress=0)`

多尺度 + 非极大值抑制。**注意代价**：每个 scale 跑一遍完整搜索。

### `find_color(bitmap, rgb, ...)`

找颜色簇**中心**。为什么聚类：一个纯色按钮有几千个同色像素，
直接返回全部调用方没法用。

### `diff(a, b)`

平均绝对差，0.0~1.0。

**与阶段四 pHash 的分工**：
- pHash 回答"**结构**变了吗"（抗噪、抗亮度）
- `diff` 回答"**像素**变了多少"（敏感、精确）

---

## 8. 相关命令

```bash
python -m avatar vision chain --text "确定"           # 完整降级过程
python -m avatar vision chain --text "确定" --no-ocr  # 关掉某层看效果
python -m avatar vision chain --text "确定" --json    # 机器可读
python -m avatar.vision.chain                         # 模块自检（56 项）
python -m avatar vision llm --cost                    # 第五层成本预算
python -m pytest tests/test_phase7_vision_chain.py -q
```
