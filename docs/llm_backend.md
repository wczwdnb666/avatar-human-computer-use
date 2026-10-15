# LLM 后端（多模型适配）

> 零 SDK，纯 `urllib`。**五家厂商，两套协议。**
> 这份文档讲的是**适配、预算、回退**。

最后更新：阶段七 7D / 7I（2026-10-07）

---

## 1. 为什么不用官方 SDK

不只是"零依赖"这个洁癖。更实际的理由：

- **版本兼容是长期负担**。各家 SDK 大版本升级经常破坏 API，
  而 HTTP 协议十年没变过
- **SDK 会拖依赖**。`openai` 包会带 `httpx` / `pydantic` / `anyio` ——
  为了发一个 POST 请求装一堆东西
- **看得见**。自己发的请求长什么样，读代码就知道

---

## 2. 五家厂商，两套协议

```
              ┌──────────────────────────┐
  统一请求 ──▶ │  LLMBackend（抽象）       │
              │   build_payload()        │
              │   parse_response()       │
              └───┬──────────────┬───────┘
                  │              │
        ┌─────────▼────┐  ┌──────▼──────────┐
        │ OpenAI 兼容   │  │ Anthropic       │
        │ GPT/DeepSeek  │  │ Claude          │
        │ Kimi/Qwen     │  │                 │
        └───────────────┘  └─────────────────┘
```

| 厂商 | 协议 | 默认端点 |
|---|---|---|
| `openai` | OpenAI | `api.openai.com/v1/chat/completions` |
| `deepseek` | OpenAI | `api.deepseek.com/v1/chat/completions` |
| `kimi` | OpenAI | `api.moonshot.cn/v1/chat/completions` |
| `qwen` | OpenAI | `dashscope.aliyuncs.com/compatible-mode/v1/...` |
| `anthropic` | Anthropic | `api.anthropic.com/v1/messages` |

**DeepSeek / Kimi / Qwen 把自己做成了"换个 base_url 就能用"的兼容层** ——
所以本模块不为它们各写一套。

### Anthropic 的三处差异

| | OpenAI | Anthropic |
|---|---|---|
| 系统提示 | 放在 `messages` 里 | **顶层 `system` 字段** |
| 工具 | `{"type":"function","function":{...}}` | `{"name":..., "input_schema":{...}}` |
| 认证头 | `Authorization: Bearer` | `x-api-key` + `anthropic-version` |

差异很小，但**写错了模型会完全忽略工具** —— 而且不报错。
所以转换集中在 `normalize_tools()` 一处做。

---

## 3. 三条硬约束（5 / 6）

### 3.1 token 预算

```python
TokenBudget(per_request=8000, total=200000)
```

**两个上限，缺一不可**：

- **单次上限**防"一次塞进去一本书"
- **累计上限**防"一个循环调了一万次"

超预算时**不发请求**，直接返回错误。

**估算方式**：中文 1.4 字符/token，其余 3.6 字符/token。

⚠ 这是**粗略估算** —— 精确计数要各家自己的 tokenizer，那是另一个 SDK。
估算**偏保守**：高估的代价是"少发几次"，低估的代价是"账单超预期"。

### 3.2 超时

默认 60 秒。**必须设** —— 不设超时的 HTTP 调用会永久挂住。

### 3.3 重试：只重试可重试的

| 状态码 | 重试？ | 为什么 |
|---|---|---|
| `429` | ✅ | 限流，等一会儿再来 |
| `5xx` | ✅ | 服务端问题，不是我们的错 |
| `408` / `409` / `425` | ✅ | 网络层问题 |
| **`4xx`（其他）** | ❌ | **参数错误重试一万次也还是错** |

指数退避：0.5s → 1s → 2s，**上限 8s**。最多 3 次。

---

## 4. 默认关闭

```python
get_backend("openai").active          # False
```

**必须 `enabled=True` 且 `api_key` 非空**才启用。

理由与 `solver` / `LLMPlanner` 一样：调外部服务要花钱，
且是隐私外发（会把你的指令发出去），必须显式打开。

---

## 5. LLMPlanner：失败必须回退（硬约束 7）

```python
LLMPlanner(enabled=True, provider="deepseek", api_key="...")
```

流程：

```
Goal
  ↓  build_messages()      系统提示 + 目标 + JSON 约束
LLMBackend.complete()
  ↓  parse_plan()          解析成 Plan
Plan.validate()            无环 + 依赖完整
  ↓  不合法 / 调用失败
RuleBasedPlanner           ★ 回退
```

### 为什么必须回退

因为"LLM 不可用"是个**常态**：没配 key、网络断了、限流了、
模型今天不听话返回了非法 JSON。这些都不该让整个任务崩掉。

**回退不是"降级到差不多的东西"，是"降级到一个永远不会崩的东西"。**

### 四种失败都会回退

1. 没配 key
2. 网络断了 / 限流
3. 模型返回的不是合法 JSON
4. 模型生成的计划有环

回退原因记在 `planner.last_fallback_reason`，也写进计划的 `notes`：

```
计划 p...  ←  打开记事本输入hello
生成器 rule(llm-fallback)   步骤 2   边 1
...
· ⚠ LLM 规划失败，已回退规则版：LLM 后端未启用（需要 enabled=True 且 api_key 非空）
```

### 解析的容错程度

**容错**：
- 剥 markdown 代码块（模型很爱包 ` ```json `）
- 从文本里找第一个 JSON 对象（模型有时先写一句解释）

**不容错**：
- 缺 `action` 就判失败，**不猜**

最后一条是刻意的：**猜错 action 的代价是执行一个错误的动作。**

---

## 6. 配置

```bash
set AVATAR_LLM_API_KEY=sk-...
set AVATAR_LLM_PROVIDER=deepseek
set AVATAR_LLM_BASE_URL=        # 可选，覆盖厂商预设
set AVATAR_LLM_MODEL=           # 可选
```

```python
from avatar.agentic_planner.llm_backend import get_backend, TokenBudget

b = get_backend("deepseek", enabled=True,
                budget=TokenBudget(per_request=4000, total=50000))
r = b.complete([{"role": "user", "content": "..."}])
print(r.describe())      # ✓ 补全成功（1234 tokens，2.1s，第 1 次尝试）
```

---

## 7. 脱敏

`api_key` **任何输出路径都脱敏**：

```python
b.describe()        # api_key : sk-a…78
b.to_dict()         # 同样脱敏
```

被测试守着（`TestFactory::test_key_redacted`）。

---

## 8. 相关命令

```bash
python -m avatar llm plan --goal "打开记事本输入hello"           # 回退演示
python -m avatar llm plan --goal "..." --provider deepseek --enabled
python -m avatar.agentic_planner.llm_backend                     # 自检（72 项）
python -m pytest tests/test_phase7_llm.py -q                     # 全程 mock
```
