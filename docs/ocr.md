# OCR（两条路径：WinRT 原生 + Tesseract 旁路）

> 主路径：纯 ctypes 手搓 `Windows.Media.Ocr`。**零 SDK。**
> 旁路径：subprocess 调外部 `tesseract.exe`。**零 Python 依赖。**
> 这份文档讲的是**怎么调通的、哪里会卡、卡住了怎么办**。

最后更新：阶段七收尾（2026-10-16，Tesseract 旁路）

---

## 0. 两条路径一览

主路径（WinRT）在本机**位图通路不通**（见第 5 节）——所以有了旁路：

| | 路径一：Windows 原生 OCR | 路径二：Tesseract 旁路 |
|---|---|---|
| 实现 | `uia/ocr_fallback.py`（ctypes 手搓 WinRT） | `uia/ocr_tesseract.py`（subprocess） |
| 依赖 | Windows 自带，零安装 | **需装 Tesseract**（可选） |
| 状态（本机） | ❌ 位图通路不通（已知问题） | ❌ 未安装 |
| 速度 | 快（进程内，几十~几百 ms） | 慢（起进程 + 写临时 BMP + 识别） |
| 精度 | 中（对 UI 字体够用） | 高（尤其中文，`chi_sim` 模型更成熟） |
| 置信度 | ❌ 不提供（固定 1.0） | ✅ TSV 带 `conf` 列 |
| 坐标口径 | 相对传入图像（左上原点） | 相对传入图像（左上原点）——**一致** |

默认 `set_engine("auto")`：**WinRT 优先，不可用自动降级 Tesseract**，
调用方无感。两个都不可用时 `is_available()` 返回 False，
OCR 层整体跳过（走 UIA 或视觉链的其他层），主功能不受影响。

---

## 1. 为什么走 WinRT

Win32 侧**根本没有 OCR 能力**。这不是选型偏好，是"只有这一条路"。

```
GDI 抓屏（avatar.vision.capture）
    ↓  BGRA 像素（自下而上的 DIB）
SoftwareBitmap（WinRT）
    ↓  CreateCopyWithAlphaFromBuffer(buffer, Bgra8, w, h, Ignore)
OcrEngine.TryCreateFromUserProfileLanguages()
    ↓
RecognizeAsync(SoftwareBitmap) → IAsyncOperation<OcrResult>
    ↓  轮询 get_Status 等完成
OcrResult → Lines → Words → (Text, BoundingRect)
```

**全程 ctypes，不装 C++/WinRT，不装 `winrt` Python 包。**

---

## 2. WinRT 与 COM 的三个区别

手搓 WinRT 要跨过三道坎：

| | COM | WinRT |
|---|---|---|
| 激活 | `CoCreateInstance(CLSID)` | `RoGetActivationFactory(类名)` |
| 字符串 | `BSTR` | `HSTRING`（不可变、引用计数） |
| 异步 | 事件 / `WaitForSingleObject` | `IAsyncOperation<T>`（**必须轮询**） |

第三条最坑：WinRT 的异步**没有可等待的内核对象**。
`put_Completed` 要注册一个 COM 回调对象 —— 用 ctypes 手搓那个对象的
vtable 比轮询复杂得多，而且回调在 WinRT 自己的线程上触发。

**本项目的选择：轮询 `get_Status`，5ms 一次。**
对 OCR（几十到几百毫秒）完全够用，而且超时是我们说了算。

---

## 3. vtable 槽位（最容易错的地方）

WinRT 接口的前 6 槽是固定的：

```
[0] QueryInterface   [1] AddRef   [2] Release
[3] GetIids          [4] GetRuntimeClassName   [5] GetTrustLevel
```

**具体方法从 [6] 开始。**

### ⚠ `TryCreateFromUserProfileLanguages` 在 **[10]**，不是 [9]

```
IOcrEngineStatics:
  [6]  MaxImageDimension
  [7]  AvailableRecognizerLanguages
  [8]  IsLanguageSupported
  [9]  TryCreateFromLanguage
  [10] TryCreateFromUserProfileLanguages    ← 这个
```

**写成 [9] 的后果**：会调到 `TryCreateFromLanguage`，
它的第一个参数是 `ILanguage*`（输入），而你塞的是一个输出指针 ——
**行为未定义**。轻则返回垃圾，重则崩。

这个坑被测试钉住：

```
tests/test_phase7_winrt.py::TestCriticalSlots::
  test_try_create_from_user_profile_is_10
```

### 其他关键槽位

| 接口 | 槽位 |
|---|---|
| `IOcrEngine` | `[6] RecognizeAsync` `[7] RecognizerLanguage` |
| `IOcrResult` | `[6] Lines` `[7] TextAngle` `[8] Text` |
| `IOcrLine` | `[6] Words` `[7] Text` |
| `IOcrWord` | `[6] BoundingRect` `[7] Text` |
| `ISoftwareBitmapStatics` | `[9] CreateCopyFromBuffer` |
| `IBufferByteAccess` | `[3] Buffer` ← **经典 COM，没有 IInspectable** |

---

## 4. HSTRING 的生命周期陷阱

`WindowsCreateStringReference` **不复制字符串**。它让 HSTRING
直接指向你给的那块内存。

```python
# ❌ 错的
WindowsCreateStringReference(value, len(value), byref(hdr), byref(out))
# ctypes 为 value 建了个临时缓冲区，函数一返回就可能被回收
# → out 立刻变成野指针

# ✅ 对的（本项目的做法）
buf = ctypes.create_unicode_buffer(value)   # 显式分配
WindowsCreateStringReference(cast(buf, c_wchar_p), len(value), ...)
# 并且把 buf 挂在返回的 HStringRef 对象上，让它活到用完为止
```

这个坑被测试钉住：

```
tests/test_phase7_winrt.py::TestRealCalls::test_hstring_ref_roundtrip
```

---

## 5. ⚠ 已知问题：SoftwareBitmap 创建失败

**这是当前最实际的阻塞点。**

### 现象

本机（Windows，2880x1800）上：

| 步骤 | 结果 |
|---|---|
| `RoInitialize` / `RoGetActivationFactory` | ✅ 正常 |
| `OcrEngine.TryCreateFromUserProfileLanguages` | ✅ 成功，语言 `zh-Hans-CN` |
| `DataWriter` → `IBuffer` | ✅ 正常（`Length` = 64000） |
| `SoftwareBitmap.CreateCopyFromBuffer` | ❌ **`E_INVALIDARG (0x80070057)`** |

### 已排查并**排除**的可能

| 怀疑 | 怎么排除的 |
|---|---|
| IID 记错 | 用 `GetIids` 列出对象实现的接口，三个 IID 全对 |
| 槽位记错 | `[9]` 传 4 参数不崩、`[10]` 传 5 参数不崩，其余槽位必崩 |
| `BitmapPixelFormat` 值错 | 0~11 全试过，全部 `E_INVALIDARG` |
| buffer 长度不对 | `capacity = length = w*h*4` 精确匹配 |
| 图太大 | 64x64 同样失败 |
| buffer 来源不对 | `IBufferFactory` 造的也失败（那个 `Length=0`，另有原因） |

### 结论

**未定位。** 这是 WinRT 的深水区（`SoftwareBitmap` 的 ABI 细节），
继续挖的性价比低于"换一条路"。

### 本模块的应对

**探测到位、如实报告、可换引擎**：

```python
is_available()          # 会一路探测到**位图通路**，不通就报不可用
explain_unavailable()   # 给出逐项排查结果与可操作建议（含 Tesseract 指引）
set_engine("tesseract") # 换到内置旁路（装了 Tesseract 才有效）
```

⚠ **`is_available` 不只看引擎能建**。因为"引擎能建"和"能真的识别"
是两件事 —— 只看前者会报"可用"，然后每次识别都失败。
那是**假的可用性**，比"不可用"更糟。

✅ **2026-10-16 起**：`auto` 模式在 WinRT 不可用时会**自动降级**
到内置的 Tesseract 旁路（见第 6A 节），无需改代码。

---

## 6. 换成别的引擎

### 6.1 内置引擎选择（推荐）

```python
from avatar.uia import ocr_fallback as O

O.set_engine("auto")        # 默认：WinRT 优先，挂了自动降级 Tesseract
O.set_engine("windows")     # 只用 Windows.Media.Ocr（诊断 WinRT 用）
O.set_engine("tesseract")   # 强制 Tesseract（WinRT 能跑也绕开它）

O.engine_mode()             # 当前模式
O.engine_name()             # 实际生效的引擎名
```

`auto` 模式下的降级判定只看 `is_available()` 的结果——
WinRT 探测挂了（位图通路不通）就自动走 Tesseract，不需要写一行代码。

### 6.2 注入自定义引擎（第三方 / 测试用）

```python
def my_engine(window, region, min_confidence, lang):
    """约定签名：返回 [OcrHit]（client_rect 是客户区坐标）。"""
    # 用 PaddleOCR / 云 API 都行
    return [
        O.OcrHit(text="确定", window=window,
                 client_rect=(10, 20, 60, 40), confidence=0.95,
                 engine="paddleocr"),
    ]

O.set_engine(my_engine, "paddleocr")
O.find_text("确定", window=hwnd)     # 之后走你的引擎
```

**这也是测试隔离的手段** —— 塞个假引擎就能测上层逻辑，
不必依赖真机有 OCR（阶段五的自检就这么干）。

---

## 6A. Tesseract 旁路详解

### 它怎么工作

```
GDI 抓屏（vision.capture.Bitmap）
    ↓  BGRA 像素（内存序）
BMP 临时文件（纯字节拼接，零依赖，用完即删）
    ↓  subprocess: tesseract in.bmp stdout -l chi_sim+eng tsv
TSV 文本
    ↓  只取 level=5（词级），conf 除以 100 归一化
[OcrWord]（坐标相对传入图像，与 WinRT 路径口径一致）
```

### 安装（二选一）

**a) 官方安装包（推荐，UB-Mannheim 维护）**

```
https://github.com/UB-Mannheim/tesseract/wiki
```

装完**把安装目录加进 PATH**（默认 `C:\Program Files\Tesseract-OCR`）。
探测顺序是 PATH → 常见安装路径，所以不配 PATH 也能被找到
（前提是装在默认位置）。

**b) winget**

```powershell
winget install UB-Mannheim.TesseractOCR
```

### 中文语言包

安装包只带 `eng`。中文要单独下：

```
https://github.com/tesseract-ocr/tessdata_fast
```

下 `chi_sim.traineddata`，放进 Tesseract 的 `tessdata` 目录
（与 `tesseract.exe` 同级，或安装时选的那个 data 目录）。

**没装 chi_sim 的后果**：`is_available()` 仍为 True（exe 能跑），
但识别中文会失败 —— `languages()` 能看出装了哪些语言。

### 两个必须知道的点

1. **subprocess 一律带超时**（默认 10s）。大图上 Tesseract 能跑
   几十秒，没超时就是把自己的命交给外部进程。
2. **没装 Tesseract 时**：`is_available()` 返回 False，上层自动跳过
   OCR 层 —— 主功能（UIA 通道）**完全不受影响**。

---

## 7. 精度边界

- **置信度**：WinRT 路径不提供（固定 1.0）；Tesseract 路径从 TSV
  的 `conf` 列归一化到 0~1。低于 `min_confidence` 的结果直接丢弃，
  **不返回"最像的"**（错的位置比没位置危险）。
- **不做版面分析**。两条路径给的都是"块 → 段 → 行 → 词"，本模块
  只透出词级结果，不合并段落、不重排阅读顺序。
- **不做图像预处理**。不二值化、不去噪、不放大。
  **图不清楚就是不清楚** —— 预处理是"让 OCR 猜得更准"，
  而猜出来的坐标拿去点击是危险的。
- **中英混排**：Tesseract 用 `chi_sim+eng`（默认语言）；
  WinRT 用 `zh-Hans-CN`（通常能认英文）。

---

## 8. 相关命令

```bash
python -m avatar.uia.ocr_fallback         # OCR 自检 + 诊断输出（含 Tesseract 一节）
python -m avatar.uia.ocr_tesseract        # Tesseract 旁路自检 + 环境快照
python -m avatar vision chain --text "确定"  # 看 OCR 在降级链里的表现
python -m pytest tests/test_phase7_ocr.py -q
```
