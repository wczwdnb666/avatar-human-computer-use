# 案例 3 · 跨端填表（网页抓数据 → 桌面应用）

**演示什么**：把**网页里的数据**填进**桌面应用**。
这是"跨端自动化"的典型场景 —— 也是纯网页工具做不到的。

## 场景

从内部系统网页上抄三个字段，填进桌面的表格程序。

## 跑

```bash
# 1) 从网页抓数据（CDP 读 DOM）
python -m avatar web-eval --port 9222 ^
  --js "JSON.stringify({name:document.querySelector('#name').innerText, order:document.querySelector('#order').innerText, amount:document.querySelector('#amount').innerText})"
```

**预期**：返回一个 JSON 字符串。

```json
{"name":"张三","order":"SO-2026-1001","amount":"¥1,280.00"}
```

```bash
# 2) 找到桌面应用窗口
python -m avatar windows --title "表格"

# 3) 逐字段输入（真人化节奏）
python -m avatar type --hwnd 0x12345 --text "张三" --human
python -m avatar type --hwnd 0x12345 --text "SO-2026-1001" --human
python -m avatar type --hwnd 0x12345 --text "1280.00" --human
```

**或者用一句话搞定**：

```bash
python -m avatar run --goal "在表格里输入张三"
```

## 更完整的做法（用 Python）

```python
from avatar.web_cdp import session as SESS
from avatar.input import dispatcher as D
from avatar.app_manager import windows as W

# 1) 抓数据
sess = SESS.connect(9222)
data = sess.evaluate("""JSON.stringify({
  name:  document.querySelector('#name').innerText,
  order: document.querySelector('#order').innerText,
  amount: document.querySelector('#amount').innerText,
})""")
sess.close()

import json
fields = json.loads(data)

# 2) 找窗口
wins = W.list_windows(title_re="表格")
hwnd = wins[0].hwnd

# 3) 填
for v in fields.values():
    D.dispatch_text(hwnd, v, human=True)
    D.dispatch_key(hwnd, ["tab"])     # 跳到下一个单元格
```

## 为什么这个案例值得看

它同时用到了**三条不同的通道**：

- **CDP**（协议层）—— 读网页，最快
- **UIA / 窗口枚举**（语义层）—— 找桌面窗口
- **输入层**（物理层）—— 真人化打字

**判断"该用哪条通道"是这个项目的核心能力**，
而 `avatar vision chain` 就是把这个判断自动化了。
