# 案例 5 · LLM 自动拆解

**演示什么**：一句话 → 大模型拆成 DAG → 执行。
和案例 1 的区别是：**案例 1 靠规则，这里靠理解**。

## 先看没配 key 时的行为

```bash
python -m avatar llm plan --goal "把这周的订单导出成表格"
```

**预期**：

```
LLMPlanner：未启用（默认）
  provider : openai
  model    : gpt-4o-mini
  api_key  : (未设置)
  回退     : 开（硬约束 7）

计划 p...  ←  把这周的订单导出成表格
生成器 rule(llm-fallback)   步骤 2   边 1
...
⚠ 回退到规则版：LLM 后端未启用（需要 enabled=True 且 api_key 非空）
```

**注意**：规则版对这种句子只能勉强解析出两个动作。
但它**不会崩** —— 这就是硬约束 7（失败必须回退）。

## 配上 key 再跑

```bash
set AVATAR_LLM_API_KEY=sk-你的key
set AVATAR_LLM_PROVIDER=deepseek
```

```bash
python -m avatar llm plan --goal "把这周的订单导出成表格" --enabled
```

**预期**：模型返回一个**更细的** DAG，比如：

```
[s1] navigate 「订单管理页」
  [s2] click 「筛选」 ← s1
    [s3] click 「本周」 ← s2
      [s4] click 「导出」 ← s3
        [s5] wait 「3s」 ← s4
          [s6] click 「保存」 ← s5
```

## 支持的厂商

```bash
python -m avatar llm plan --goal "..." --provider deepseek --enabled
python -m avatar llm plan --goal "..." --provider kimi     --enabled
python -m avatar llm plan --goal "..." --provider qwen     --enabled
python -m avatar llm plan --goal "..." --provider anthropic --enabled
```

**DeepSeek / Kimi / Qwen 共用一套协议**（OpenAI 兼容），
只有 base_url 和模型名不同 —— 所以换厂商只是换个参数。

## 成本控制（硬约束 5）

| 限制 | 默认值 | 作用 |
|---|---|---|
| 单次请求 | 8000 tokens | 防"一次塞进去一本书" |
| 累计 | 200000 tokens | 防"一个循环调了一万次" |

超预算时**不发请求**，直接返回错误。要调：

```python
from avatar.agentic_planner.llm_backend import TokenBudget, get_backend
b = get_backend("deepseek", enabled=True, api_key="...",
                budget=TokenBudget(per_request=4000, total=50000))
```

## 失败时会发生什么

四种失败都会**回退规则版**，不会崩：

1. 没配 key
2. 网络断了 / 限流
3. 模型返回的不是合法 JSON
4. 模型生成的计划有环

回退原因记在 `planner.last_fallback_reason`，也写进计划的 `notes`。
