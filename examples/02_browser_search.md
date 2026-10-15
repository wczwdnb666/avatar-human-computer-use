# 案例 2 · 浏览器搜索并提取结果

**演示什么**：对网页**不走鼠标** —— 直接通过 CDP 读 DOM、执行 JS。
比"模拟点击 + OCR 读屏"快一个数量级，而且准。

## 前提

浏览器要以调试端口启动：

```bash
"C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe" ^
  --remote-debugging-port=9222 --user-data-dir=C:/tmp/edge-debug
```

或者让 avatar 自己起：

```bash
python -m avatar web-open --url "https://www.bing.com" --port 9222
```

## 跑

```bash
# 1) 打开页面
python -m avatar web-open --url "https://www.bing.com" --port 9222

# 2) 在搜索框里输入（走 DOM，不模拟键盘）
python -m avatar web-eval --port 9222 ^
  --js "document.querySelector('input[name=q]').value='avatar 自动化'"

# 3) 提交搜索
python -m avatar web-eval --port 9222 ^
  --js "document.querySelector('form').submit()"

# 4) 等一会儿，抓结果标题
python -m avatar web-eval --port 9222 ^
  --js "Array.from(document.querySelectorAll('h2 a')).slice(0,5).map(a=>a.innerText).join('\n')"
```

**预期**：最后一步返回前 5 条搜索结果的标题。

## 为什么不用鼠标点

| | CDP 直连 | 模拟点击 |
|---|---|---|
| 耗时 | ~20ms | ~800ms（移动+点击+验证） |
| 准确度 | 精确 | 受窗口位置/DPI 影响 |
| 被风控概率 | 低 | 高（真人化只能降低，不能消除） |

**判断标准**：**能用协议就别用鼠标**。鼠标是最后手段。

## 什么时候必须回到鼠标

- 目标是 Electron 应用且没开调试端口
- 页面在 iframe 里且跨域（CDP 进不去）
- 需要触发真实的用户手势（某些验证组件会检查 `isTrusted`）

这时候走 `avatar vision chain` —— 五层降级链会自动选一条能走的路。
