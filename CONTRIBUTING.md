# 贡献指南

**先说三件事**，省得你白写代码：

1. **本项目零依赖** —— 提 PR 时引入任何第三方包都会被 CI 拦下
2. **不硬解验证题** —— 全项目没有一行识别验证码的代码，将来也不会有
3. **每步都要能解释** —— 没有 `reason` 的失败在排查时等于没有信息

---

## 开发环境

**只需要 Python 3.10+ 和 Windows。** 不用装任何包。

```bash
git clone https://github.com/wczwdnb666/avatar-human-computer-use.git
cd avatar-human-computer-use

# 确认环境
python scripts/selfcheck.py --list      # 看会跑什么（不真跑）
python scripts/selfcheck.py             # 跑自检（约 30 秒）
python -m pytest tests/ -q              # 跑测试（约 5 分钟，会动鼠标）
```

**唯一的 `pip install` 是 pytest** —— 那是测试框架，属于**开发期**依赖：

```bash
python -m pip install pytest
```

**运行期依赖是空的**（`dependencies = []`），CI 会用 AST 扫描守着它。
除了 pytest，如果你还需要装别的包才能开发，那说明有人破坏了零依赖。

> 口径别混：说"零依赖"指的是**用户装完就能用**，
> 不是说开发者连测试框架都不需要。把 pytest 藏起来假装
> "什么都不用装"只会让新人 clone 下来第一步就跑不通。

### 推荐的编辑器配置

- **行宽 79**（PEP 8）。本项目所有代码都在这个宽度内。
- **UTF-8 无 BOM**。所有文件顶部有 `# -*- coding: utf-8 -*-`。
- **LF 换行**（`.gitattributes` 已配置）。

---

## 测试规范

### ⚠ 必须走"外部调用方的真实路径"

这是本项目最重要的一条测试规范。

**反面例子**：

```python
# ❌ 直接调内部函数 —— 测的是"这个函数对不对"，
#    不是"用户这么用会不会出问题"
def test_parse():
    assert _parse_clause("打开记事本") == ("open", "notepad")
```

**正面例子**：

```python
# ✅ 走公开入口 —— 测的是"用户这么用会得到什么"
def test_parse_via_public_api():
    g = parse_goal("打开记事本")
    assert g.intent == "open" and g.target == "notepad"
```

**为什么**：内部函数签名会变，公开契约不该变。
测内部函数会让你**为了改测试而改测试**，而不是发现真问题。

### 每个测试文件顶部要有"这个文件在守什么"

```python
# -*- coding: utf-8 -*-
"""阶段N · 模块名（``path/to/module``）。

## 这个文件在守什么

1. 第一条契约
2. 第二条契约

## 隔离

全程 mock，不联网、不动桌面。
"""
```

**不写这个的文件，review 时会打回** —— 三个月后没人知道它在测什么。

### 测试要能"单独跑也过"

```bash
python -m pytest tests/test_xxx.py -q          # 必须过
python -m pytest tests/ -q                     # 也必须过
```

**如果"单独跑过、连跑挂"**，那是**跨测试干扰** ——
说明有测试改了进程级全局状态（DPI / COM / locale / cwd / 环境变量）。

`tests/conftest.py` 里有 `_isolate_global_state` 夹具会自动复位，
但**更该做的是别改全局状态**。

### 需要真实桌面 / 网络的测试

用 `skipif` 标出来：

```python
_HAS_OCR = O.is_available()
_needs_ocr = pytest.mark.skipif(not _HAS_OCR, reason="本机 OCR 不可用")

@_needs_ocr
class TestRealRecognition:
    ...
```

**CI 在无头环境跑，这些会自动 skip。** 但**纯逻辑的部分必须能跑**。

---

## 命名规范

### ⚠ 新增函数/类前，**先 grep 全仓库**

```bash
# 加一个叫 find_template 的函数前，先查有没有重名
grep -rn "def find_template\|class find_template" avatar/ avatar_shell/
```

**为什么**：本项目**踩过这个坑**。

阶段二有个 `find_template(window, template)`（引擎包装版），
阶段七加了个 `find_template(haystack, needle)`（纯算法版）——
**后者覆盖了前者**，而且不报错，只是行为变了。
最后把旧的改名 `find_template_in_window` 才解决。

Python 不会为"模块内重名"报错，它只是**静默覆盖**。

### 命名约定

| 类型 | 风格 | 例 |
|---|---|---|
| 模块 | 小写下划线 | `human_entropy` |
| 类 | 大驼峰 | `PresenceState` |
| 函数 / 变量 | 小写下划线 | `make_circle_bgra` |
| 常量 | 大写下划线 | `DEFAULT_MAX_EDGE` |
| 私有 | 前缀下划线 | `_snapshot_global_state` |

**名字要说清"做什么"，不是"怎么做"**：

- ✅ `normalize_image` —— 说清了结果
- ❌ `process_image_data_2` —— 没说清任何事

---

## 代码规范

### 每个 `.py` 顶部必须有模块级 docstring，含「能力边界」一节

```python
# -*- coding: utf-8 -*-
"""一句话说这个模块做什么。

## 为什么这么设计

...（讲取舍，不是讲 API）

## 能力边界

**做**：...

**不做**：

- **不做 X** —— 因为 Y
- **不做 Z** —— 因为 W
"""
```

**「能力边界」这一节是硬性要求**，自检会机械检查（硬约束 ⑩/⑬）。

**为什么**：写清"不做什么"比写清"做什么"更重要 ——
它能挡住 80% 的"为什么不做 X"的 issue。

### 注释写"为什么"，不写"是什么"

```python
# ❌ 废话注释
# 把 x 加 1
x += 1

# ✅ 说清为什么
# ⚠ 必须 +1：Windows 的 GetWindowRect 返回的是**开区间**，
#   而我们要的是闭区间宽度
x += 1
```

**尤其要标出踩过的坑**：

```python
# ⚠ 这里是 [10] 不是 [9] —— 写成 [9] 会调到 TryCreateFromLanguage，
#   它的第一个参数是 ILanguage*（输入），而你塞的是输出指针
hr = vtbl_call(statics, 10, ...)
```

### 中文引号：内层用「」，**不要用 ASCII 双引号**

```python
# ❌ 语法错误！内层的 " 会提前结束字符串
_add("他说"你好"", True)

# ✅
_add("他说「你好」", True)
```

**这个坑踩了 8 次。** 中文内容里的引号一律用 `「」` 或 `『』`。

另外：`bytes` 字面量**只能含 ASCII** —— 中文要先 `.encode("utf-8")`。

---

## 硬约束

**108 条**，全部由自检机械检查。

```bash
python scripts/selfcheck.py              # 全量（含硬约束）
python scripts/selfcheck.py --list       # 只列出会跑什么
```

完整清单见 [SKILL.md](SKILL.md) 的「108 条硬约束」章节。

### 几条最容易违反的

| # | 约束 | 怎么检查 |
|---|---|---|
| 1 | **零第三方依赖** | CI 里 AST 扫 import |
| 2 | **模块 docstring 含「能力边界」** | 自检逐文件查 |
| 3 | **禁止裸 bool**（验证必须三态） | 代码 review |
| 4 | **所有状态转换过 `transition()`** | 代码 review |
| 5 | **不硬解验证题** | AST 查符号名 |
| 6 | **超预算抛异常，不静默调用** | 单测 |
| 7 | **降级要说明 reason** | 单测 |

---

## 提 PR 的流程

1. **先开 issue** —— 说清"要解决什么问题"。
   大改动没 issue 直接提 PR 会被打回。
2. **从 `main` 切分支** —— 命名 `feat/xxx` / `fix/xxx` / `docs/xxx`。
3. **写代码 + 写测试** —— 测试要覆盖新行为。
4. **跑全量自检与测试**：

   ```bash
   python scripts/selfcheck.py
   python -m pytest tests/ -q
   python -m avatar_shell --selftest
   ```

5. **按模板填 PR 描述** —— 尤其"怎么验证"那一栏，
   要给出**能直接跑的命令**和**预期输出**。

### Review 会看什么

- **有没有引入依赖**（一票否决）
- **测试走的是不是真实路径**
- **模块 docstring 有没有「能力边界」**
- **踩过的坑有没有写进注释**
- **有没有硬解验证题的痕迹**（一票否决）

### 不会被接受的改动

- 引入任何第三方运行时依赖
- 硬解验证码 / 绕过风控
- 把"失败的步骤"当成"成功"（比如 `unknown` 当 `ok`）
- 去掉 `reason` 字段（那是排查的唯一线索）
- 把硬约束常量改成可配置参数（那是安全边界，不是调优旋钮）

---

## 发布流程

**只有维护者能发布。** 流程见 [CHANGELOG.md](CHANGELOG.md) 与
`.github/workflows/release.yml`。

```bash
# 1. 改 pyproject.toml 的版本号
# 2. 在 CHANGELOG.md 加一段
# 3. 提交
git commit -am "chore: release v0.1.1"

# 4. 打 tag（会自动触发 Release workflow）
git tag -a v0.1.1 -m "v0.1.1"
git push origin main --tags
```

**CI 会校验 `pyproject.toml` 的版本与 tag 一致** ——
不一致会红，防止发错版本。

---

## 有问题？

- **使用问题** → GitHub Discussions
- **Bug** → Issue（用 bug_report 模板，**记得贴自检输出**）
- **安全漏洞** → 见 [SECURITY.md](SECURITY.md)，**不要开公开 issue**
