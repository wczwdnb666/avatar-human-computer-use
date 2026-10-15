# 视觉大模型层（vision LLM）

> 五层降级链的**最后一层**。前四层全失败才启用。
> 启用意味着**高成本 + 低确定性**，所以必须设计得极其保守。

最后更新：阶段八 8A–8I（2026-10-07）

---

## 0. 为什么它是最后一层

```
UIA  →  CDP  →  OCR  →  模板匹配  →  视觉大模型
~2ms   ~20ms   ~300ms   ~500ms       ~3s
```

三个理由：

1. **最贵** —— 一次 1280x720 的图约 1229 图像 token，
   加上 prompt 和输出轻松上千 token
2. **最慢** —— 图片上传 + 视觉推理，几秒到几十秒
3. **最不确定** —— **大模型会编造坐标**（见第 4 节）

所以它**默认关闭**（硬约束 2），必须显式
`enabled=True` + `api_key` 才启用。

---

## 1. 用哪家：六家厂商，两套协议

| 厂商 | 默认模型 | 协议 |
|---|---|---|
| `openai` | `gpt-4o-mini` | OpenAI 兼容 |
| `deepseek` | `deepseek-vl2` | OpenAI 兼容 |
| `kimi` | `moonshot-v1-8k-vision-preview` | OpenAI 兼容 |
| `qwen` | `qwen-vl-plus` | OpenAI 兼容 |
| `glm` | `glm-4v` | OpenAI 兼容 |
| `anthropic` | `claude-3-5-sonnet` | Anthropic messages |

**五家走同一套协议** —— 换厂商只是换 base_url 和模型名。

### 两家的图片格式差异（写错模型看不到图，而且不报错）

```
OpenAI（image_url）:
  {"type": "image_url",
   "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQ..."}}

Anthropic（image + source）:
  {"type": "image",
   "source": {"type": "base64", "media_type": "image/jpeg",
              "data": "/9j/4AAQ..."}}
```

差异是"base64 包不包在 data URL 里"。**写错了模型只会说"图里没找到"** ——
所以转换集中在 `normalize_image()` 一处做。

---

## 2. 图怎么压：四步预处理

一张 2880x1800 的屏幕截图：

| | 数值 |
|---|---|
| 原始 BGRA | 20.7 MB |
| base64 后 | 27.6 MB |
| 估算 token | **6912** |
| 压缩后（1280x800） | 143 KB（JPEG q85） |
| 压缩后 token | **1366** |

**一次请求 6912 token 只为了找一个小按钮** —— 所以必须压。

### 处理顺序（不能换）

```
① 裁剪   先切掉无关区域（省的是**面积**，最划算）
② DPR    只记因子，**不动图**
③ 缩放   缩到最长边 ≤ 1280
④ 编码   JPEG（GDI+）优先，失败回退 PNG
```

**为什么裁剪在缩放前**：先缩再裁的话，裁剪坐标要除以缩放比才能对应原图 ——
多一次换算就多一次出错机会。

### 编码器

| 编码器 | 实现 | 特点 |
|---|---|---|
| **JPEG** | **GDI+（纯 ctypes）** | 有损，但小得多 |
| PNG | 纯 Python + `zlib` | 无损，但大 |

**为什么用 GDI+ 而不是手写 JPEG 编码器**：手写要 DCT + 量化 + 霍夫曼，
300+ 行，而且容易在边界条件上出错（产生肉眼可见的块效应）。
GDI+ 是 Windows 自带的，一次调用就出图。

**⚠ 两个 GDI+ 的坑**（都踩过）：

1. `EncoderParameterValueTypeLong` 是 **4 不是 1** —— 写错返回
   `InvalidParameter`，看不出是哪一步的问题
2. **`GlobalLock` 的 restype 必须声明为 `c_void_p`** ——
   不声明的话 ctypes 按 C int 处理，64 位指针被截断

### ⚠ 可逆性是硬要求

`scale` **必须记录**。模型返回的是**压缩后图像的坐标**：

```
图像坐标 (100, 200)
    ↓ ÷ scale = 0.444
原图坐标 (225, 450)
    ↓ + offset（裁剪偏移）
客户区坐标
    ↓ + screen_rect
屏幕坐标
```

**漏了这一步，缩放比 0.44 时点击位置会整体偏移 2.3 倍** ——
而且偏得很隐蔽。

---

## 3. prompt 怎么写：四条硬约束

```json
{
  "found": true,
  "x": 100, "y": 200,
  "confidence": 0.9,
  "reason": "蓝色的确定按钮"
}
```

### 四条必须写进 prompt 的约束

1. **坐标系** —— 坐标是**图像像素**，原点左上角，**不是屏幕坐标**
2. **不许编造** —— 找不到必须返回 `found=false`
3. **不许 Markdown** —— 只返回裸 JSON
4. **不许模糊** —— `confidence` 是 0~1 的数，不是 "high" / "probably"

外加两条：

5. **坐标范围**（`0 ≤ x < 宽`）—— 模型知道边界后越界会明显减少
6. **不处理验证题** —— 看到验证码/滑块直接返回 `found=false`

**输出格式只有一处定义**（`OUTPUT_SCHEMA`）—— 改格式忘了改校验，
会让模型返回的坐标被当成屏幕坐标直接点。

---

## 4. ⚠ 坐标怎么校验：四道闸门（本层最重要的设计）

**大模型会编造坐标。这不是"偶尔出错"，是结构性的**：

- 它被训练成"给出答案"，而不是"承认不知道"
- 它看到的是压缩后的图，坐标天然有偏差
- 它对"像素"没有真实概念 —— 只是在生成看起来合理的数字

所以模型返回的 `(x, y)` **在用来点击之前必须过四道闸门**：

```
模型返回 (x, y, confidence)
        │
        ▼
① 范围校验    0 ≤ x < 宽 且 0 ≤ y < 高          ← 越界直接拒绝
        │
        ▼
② 坐标换算    图像 → 原图 → 屏幕                ← 漏了会整体偏移
        │
        ▼
③ 像素校验    周围 50x50 区域必须有颜色变化      ← 空白区拒绝
        │
        ▼
④ 置信度阈值  confidence ≥ 0.5                 ← 模型没把握就拒绝
        │
        ▼
   （可选）⑤ 二次确认：截小图再问一次模型
        │
        ▼
    ValidatedResult（可以拿去点击了）
```

### 每道闸门挡的是什么

| 闸门 | 挡的真实情况 |
|---|---|
| ① 范围 | 模型给了个 (5000, 3000)，而图只有 1280x720 |
| ② 换算 | 图被缩到 0.44 倍，模型说 (100, 200)，原图其实是 (225, 450) |
| ③ 像素 | 模型在**空白处**报了个坐标（它"觉得"按钮应该在那儿） |
| ④ 置信度 | 模型说"可能是吧"（confidence=0.2），你却拿它去点 |

### ③ 是最有价值的一道

**它用真实像素否证模型的说法。** 前面两道是算术，只有这道是"看图说话"。

判据是 **50x50 区域的灰度方差**：

- 纯色背景 → 方差 ≈ 0 → 拒绝
- 有文字/边框/渐变 → 方差 > 8 → 通过

**为什么用方差而不是"有没有非背景色"**：因为**不知道背景色是什么**。
深色主题的按钮可能是浅灰，浅色主题的可能是深灰 ——
按颜色判断两头不讨好。方差只问"这一块有没有变化"，**与主题无关**。

### ⚠ 失败返回 None，不降级到盲点

校验失败意味着"我们**不知道**目标在哪"。这时候：

- **不能**用原始坐标凑合（那是盲点）
- **不能**返回一个"最接近的"候选（那是猜）
- **不能**抛异常（调用方还要继续沿降级链走）

**返回 `None` + 详细的失败原因。**

但"返回 None"有个副作用：**失败原因丢了**。所以加一个可选的
`trace` 参数：

```python
gates = []
r = validator.validate(res, img, bitmap=bmp, trace=gates)
if r is None:
    failed = [g for g in gates if not g.passed and not g.skipped]
    print(failed[0].gate, failed[0].reason)   # → "pixel 该位置周围是一片空白"
```

### ⑤ 二次确认：默认关闭

把该坐标周围的小图**再问一次模型**"这里是你说的元素吗"。

**为什么默认关**：

- 它**多花一次调用**（成本翻倍）
- 收益有限：模型第二次也可能说"是"
- 有副作用：它可能说"不是"，而第一次其实是对的
- 慢：多一次网络往返

**开启的场景**：点击代价极高的操作（比如"确认转账"）。

**它抛异常时按"通过"处理** —— 不让它成为单点故障。

---

## 5. 成本怎么控

### 三个上限

| 上限 | 默认值 | 挡什么 |
|---|---|---|
| 单次请求 | 2000 token | 一张巨大的图 |
| 单任务累计 | 20000 token | 一个循环调了十几次 |
| 缓存 | 256 条（开启） | 同一张图同一个目标问第二遍 |

**超预算抛 `CostExceeded`，不静默调用**（硬约束 6）。

静默调用会让"账单失控"变成一个**没有告警**的事故。

### 缓存的键：**截图 hash + 目标描述**

```
键 = blake2b(JPEG 数据)[:16] + "|" + 归一化的目标描述
```

**两个都要**：

- 只按目标缓存 → 界面变了还用旧结果 → **点到旧位置**
- 只按截图缓存 → 换了个目标 → 返回无关结果

**两个一起用才是"同一个界面上的同一个目标"。**

**hash 本身就是失效机制** —— 界面变了 hash 就变了，不需要额外的过期策略。

### 记账

```python
CostRecord(provider, model, prompt_tokens, completion_tokens,
           est_tokens, elapsed, ok, cached, at, cost)
```

`cost` 字段**本模块不填** —— 不同厂商、不同模型价格不同，而且经常变。
只统计 token，钱的事交给调用方。

---

## 6. 完整流程

```
1. 检查后端是否启用（默认关）
2. 抓图（grab_window(hwnd, client_only=True)）
3. 预处理（裁剪 → DPR → 缩放 → 编码）
4. 查缓存（**在预算之前** —— 命中不花钱）
5. 查预算（超了抛 CostExceeded）
6. 调模型（超时 60s + 重试 3 次）
7. 过四道校验闸门
8. 存缓存
9. 返回 ValidatedResult
```

**第 4 步在 5 之前**：缓存命中**不花钱**。先查缓存再查预算，
能省掉"同一张图问第二遍"这种纯浪费 —— 而预算被浪费掉之后，
真正需要的那次调用就没额度了。

---

## 7. 配置与使用

```bash
# 只看请求构造（**不发真实请求**）
python -m avatar vision llm --live --goal "确定按钮"

# 真调用
set AVATAR_LLM_API_KEY=sk-...
python -m avatar vision llm --live --goal "确定按钮" --enabled

# 指定厂商
python -m avatar vision llm --live --goal "确定按钮" \
    --provider deepseek --enabled

# 成本与缓存
python -m avatar vision llm --cost
python -m avatar vision llm --clear-cache
```

```python
from avatar.vision import cost as CO
from avatar.vision import llm_backend as LB
from avatar.vision import validator as VD
from avatar.vision.chain import VisionChain

chain = VisionChain(
    allow_llm=True,
    llm_backend=LB.get_vision_backend("deepseek", enabled=True,
                                      api_key="..."),
    cost_budget=CO.CostBudget(per_request=2000, total=20000),
    cache=CO.ResultCache(),
    validator=VD.CoordinateValidator(recheck=False),
)
r = chain.locate("确定按钮", window=hwnd)
print(r.explain())
```

---

## 8. 能力边界（本层不做的事）

- **不引入任何 SDK**。只用 `urllib.request` + 标准库。
- **不做图像增强**。不二值化、不去噪、不锐化 ——
  增强会让模型"看到"原本不存在的东西，而它给出的坐标会被用来点击。
- **不做坐标修正**。"差一点点"也不帮你挪 —— 挪多少都是猜，
  猜错会点到别的控件上。
- **不做多候选排序**。模型只给一个坐标，就只校验这一个。
- **不做持久化缓存**。缓存生命周期 = 进程生命周期。
- **不猜价格**。
- **不硬解验证题**（硬约束 #9 全项目生效）。
