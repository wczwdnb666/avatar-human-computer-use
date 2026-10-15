# OCR（WinRT 接入）

> 用纯 ctypes 手搓 `Windows.Media.Ocr`。**零 SDK。**
> 这份文档讲的是**怎么调通的、哪里会卡、卡住了怎么办**。

最后更新：阶段七 7B / 7I（2026-10-07）

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
explain_unavailable()   # 给出逐项排查结果与可操作建议
set_engine(fn, name)    # 换引擎（Tesseract / PaddleOCR / 云 OCR）
```

⚠ **`is_available` 不只看引擎能建**。因为"引擎能建"和"能真的识别"
是两件事 —— 只看前者会报"可用"，然后每次识别都失败。
那是**假的可用性**，比"不可用"更糟。

---

## 6. 换成别的引擎

```python
from avatar.uia import ocr_fallback as O

def my_engine(window, region, min_confidence, lang):
    """约定签名：返回 [OcrHit]。"""
    # 用 Tesseract / PaddleOCR / 云 API 都行
    return [
        O.OcrHit(text="确定", window=window,
                 client_rect=(10, 20, 60, 40), confidence=0.95,
                 engine="tesseract"),
    ]

O.set_engine(my_engine, "tesseract")
O.find_text("确定", window=hwnd)     # 之后走你的引擎
```

**这是唯一的可控路径** —— 也是测试隔离的手段
（塞个假引擎就能测上层逻辑，不必依赖真机有 OCR）。

---

## 7. 精度边界

- **不提供置信度**。WinRT OCR 不返回置信度，本模块统一给 `1.0`。
  只有注入自定义引擎时 `confidence` 才有意义。
- **不做版面分析**。WinRT 给的是"行 → 词"，本模块原样透出。
  不合并段落、不重排阅读顺序。
- **不做图像预处理**。不二值化、不去噪、不放大。
  **图不清楚就是不清楚** —— 预处理是"让 OCR 猜得更准"，
  而猜出来的坐标拿去点击是危险的。
- **中英混排**要引擎自己支持（`zh-Hans-CN` 通常能认英文）。

---

## 8. 相关命令

```bash
python -m avatar.agentic_planner.goal     # 不相关，但同属阶段六
python -m avatar.uia.ocr_fallback         # OCR 自检 + 诊断输出
python -m avatar vision chain --text "确定"  # 看 OCR 在降级链里的表现
python -m pytest tests/test_phase7_ocr.py -q
```
