# -*- coding: utf-8 -*-
"""阶段五 · 误报率测试（5G 重点）。

## 这个文件在守什么

风险检测器最贵的错误不是"漏报"（漏了还有 5C 的熔断兜底），
而是**误报** —— 它会把正常流程拦下来，而产品哲学是
「除了必要全是自动」。一次误报 = 一次无谓的人工打断。

所以这里用 20 个**真实的正常页面**做基准，要求：

    误报率 < 5%（20 个样本里最多 1 个）

## 样本从哪来

``tests/fixtures/normal_pages/*.html`` —— 20 个本地 HTML 文件，
覆盖电商（首页/详情/购物车/结算）、新闻（详情/列表）、
账号（登录/注册/找回密码）、表单、隐私政策、服务条款、
帮助中心、404、关于我们、银行网银、企业官网、博客、
招聘、文档中心。

**不依赖 Chrome、不依赖网络**（硬约束 #3）—— 纯 HTML 剥标签取文本。

## 样本设计的两条原则（很重要，别改坏）

1. **必须带真实的危险词**。注册页真的有"获取验证码"按钮，
   银行页面真的有"风险控制"和"短信验证码"，文档页真的有"正在初始化"。
   把这些词去掉再测误报率，等于把体温计放进冰水里量 —— 好看，但没意义。
2. **必须带完整页脚**。真实站点都有页脚，页脚里全是 footer 档弱词。
   这是"正常页面"最稳定的特征，也是本模型压制误报的主要机制。

## 消融测试（硬约束 #4）

``penalty_scale={WEAK_TIER_FOOTER: 0.0}`` 把页脚档惩罚清零，
**误报率必须上升**。这不是"跑一下看看"，而是**断言**。

它验证的是："页脚反证"这一档**真的在干活**，不是摆设。
如果哪天有人把 footer 档权重调没了、或者把页脚词从表里删了，
这条会红。

## 已知误报（诚实记录）

``16_bank_dashboard.html`` 会被误报。**没有偷偷调参把它压掉**，
理由写在 :data:`KNOWN_FALSE_POSITIVES` 里。这是刻意留着的 ——
一个 5% 的误报率如果连"误报的是哪一类"都说不清，
那这个数字就没有意义。
"""

from __future__ import annotations

import json
import os
import re

import pytest

from avatar.risk_detector.fingerprints import (
    WEAK_TIER_AMBIGUOUS,
    WEAK_TIER_FOOTER,
    WEAK_TIER_OCR_NOISE,
    text_hit_score,
)

#: 样本目录
FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "fixtures", "normal_pages")

#: 允许的误报上限。**用户明确要求：20 个样本最多 1 个。**
MAX_FALSE_POSITIVES = 1

#: 误报率上限（百分比）
MAX_FP_RATE = 5.0

#: 已知误报 —— **刻意留着，不偷偷调参压掉**。
#:
#: 根因：银行网银的"安全提示"里必然出现
#: 「短信验证码」「风险控制」「操作异常」「验证码」，
#: 这四条 text 指纹的正分（1.4 + 1.0 + 1.0 + 0.8 = 4.2）
#: 超过了页脚反证能压住的范围（-1.5 左右），最终 2.65 > 阈值 1.0。
#:
#: 为什么**不**现在修：改权重属于 5B 指纹库的职责，且会牵动
#: 已验收的 52 项指纹自检。这个判断留给用户。
#:
#: 修法建议（如果要修）：这正是"按在正常页面的普遍程度排权重"
#: 那条原则的又一次应用 —— 「风险控制」「操作异常」在**金融类
#: 正常页面**里的普遍程度很高，应该降权或移进弱词表。
KNOWN_FALSE_POSITIVES = {
    "16_bank_dashboard.html":
        "银行安全提示里的「风险控制/操作异常/短信验证码」被当成风险信号。"
        "根因是这几条指纹没考虑金融类正常页面 —— "
        "建议按「在正常页面的普遍程度」原则降权。",
}


# ==========================================================================
# 样本加载
# ==========================================================================
def html_to_text(html: str) -> str:
    """把 HTML 剥成文本。**模拟 ``innerText`` 的效果。**

    真实场景里 detector_dom 通过 CDP 读的是 ``document.body.innerText``
    —— 包含页面上所有可见文字（含 header / nav / footer）。

    这里用正则剥标签，够用且**零依赖**（不引入 bs4/lxml）。
    """
    h = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    h = re.sub(r"(?s)<!--.*?-->", " ", h)
    h = re.sub(r"(?s)<[^>]+>", " ", h)
    h = (h.replace("&nbsp;", " ").replace("&amp;", "&")
         .replace("&lt;", "<").replace("&gt;", ">")
         .replace("&quot;", '"').replace("&#39;", "'"))
    return re.sub(r"\s+", " ", h).strip()


def load_samples():
    """加载全部样本。返回 ``[(文件名, 文本)]``。"""
    if not os.path.isdir(FIXTURE_DIR):
        return []
    out = []
    for name in sorted(os.listdir(FIXTURE_DIR)):
        if not name.endswith(".html"):
            continue
        path = os.path.join(FIXTURE_DIR, name)
        with open(path, encoding="utf-8") as f:
            out.append((name, html_to_text(f.read())))
    return out


def score_all(penalty_scale=None):
    """给全部样本打分。返回 ``[(文件名, 结果dict)]``。"""
    return [(n, text_hit_score(t, penalty_scale=penalty_scale))
            for n, t in load_samples()]


@pytest.fixture(scope="module")
def samples():
    s = load_samples()
    if not s:
        pytest.skip("样本目录不存在：%s" % FIXTURE_DIR)
    return s


@pytest.fixture(scope="module")
def baseline(samples):
    """全部样本的原始打分结果（不含消融）。"""
    return {n: text_hit_score(t) for n, t in samples}


# ==========================================================================
# 1. 样本本身的质量（先保证基准是可信的）
# ==========================================================================
class TestSampleQuality:
    """样本不可信，误报率就没意义。这一节先验样本。"""

    def test_exactly_20_samples(self, samples):
        assert len(samples) == 20, "样本数应为 20，实际 %d" % len(samples)

    def test_no_chrome_needed(self):
        """硬约束 #3：不 import 浏览器 / CDP / 网络相关模块。

        用 AST 检查**真实 import**，不做文本搜索 —— 文本搜索会被
        这条断言自己的字符串命中（5C 里踩过这个坑）。
        """
        import ast as _ast
        tree = _ast.parse(open(__file__, encoding="utf-8").read())

        mods = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Import):
                for a in node.names:
                    mods.add(a.name or "")
            elif isinstance(node, _ast.ImportFrom):
                mods.add(node.module or "")

        tops = {m.split(".")[0] for m in mods}
        forbidden = {"playwright", "selenium", "pyppeteer", "subprocess",
                     "requests", "urllib", "http", "socket", "asyncio"}
        hit = tops & forbidden
        assert not hit, "误报率测试不该依赖：%s" % sorted(hit)

        # 内部模块也不许碰 CDP
        assert not any("web_cdp" in m for m in mods), \
            "不该 import web_cdp —— 样本是本地 HTML"

    def test_samples_are_substantial(self, samples):
        """样本不能是几个词的碎片。

        阈值定在 80 字：结算页、404 页这类真实页面本来就短，
        不能按文章页的标准要求它们。但也不许出现"只有一行标题"的假样本。
        """
        for name, text in samples:
            assert len(text) >= 80, "%s 太短（%d 字）" % (name, len(text))

    def test_every_sample_has_footer_words(self, samples):
        """★ 每个样本都必须带页脚弱词。

        这是"正常页面"最稳定的特征。如果某个样本没有页脚，
        它就不是一个真实的正常页面样本 —— 消融测试会因此失真。
        """
        for name, text in samples:
            r = text_hit_score(text)
            footer_n = r["tiers"].get(WEAK_TIER_FOOTER, 0)
            assert footer_n >= 3, \
                "%s 只有 %d 个页脚弱词，不像真实站点" % (name, footer_n)

    def test_samples_contain_risky_words(self, samples):
        """★ 样本必须真的带危险词，否则这个测试是自欺欺人。

        至少要有一批样本命中 text 层的正分指纹。
        （注册页的"获取验证码"、银行页的"风险控制"、
          表单页的"请点击"、文档页的"正在初始化"…）
        """
        with_hits = [n for n, t in samples
                     if text_hit_score(t)["hits"]]
        assert len(with_hits) >= 3, \
            "只有 %d 个样本带正分指纹，样本太'干净'了" % len(with_hits)

    def test_html_stripping_works(self):
        assert html_to_text("<p>你好</p><b>世界</b>") == "你好 世界"

    def test_html_stripping_removes_script(self):
        assert "var x" not in html_to_text("<script>var x=1</script><p>正文</p>")

    def test_html_stripping_removes_style(self):
        assert "color" not in html_to_text(
            "<style>.a{color:red}</style><p>正文</p>")


# ==========================================================================
# 2. 误报率（核心）
# ==========================================================================
class TestFalsePositiveRate:
    def test_no_more_than_one_false_positive(self, baseline):
        """★★ 核心断言：20 个正常页面，最多 1 个误报。"""
        fp = [n for n, r in baseline.items() if r["detected"]]
        assert len(fp) <= MAX_FALSE_POSITIVES, \
            "误报 %d 个（上限 %d）：%s" % (len(fp), MAX_FALSE_POSITIVES, fp)

    def test_fp_rate_under_5_percent(self, baseline):
        """★★ 误报率 < 5%。"""
        n = len(baseline)
        fp = sum(1 for r in baseline.values() if r["detected"])
        rate = 100.0 * fp / n
        assert rate < MAX_FP_RATE or fp <= MAX_FALSE_POSITIVES, \
            "误报率 %.1f%%（%d/%d）" % (rate, fp, n)

    def test_false_positives_are_the_known_ones(self, baseline):
        """★ 误报的必须是**已知**的那几个，不许出现新的。

        这条比"数量 <= 1"更有用：数量相同但换了个样本误报，
        说明模型在别的地方退化了，而总数看不出来。
        """
        fp = {n for n, r in baseline.items() if r["detected"]}
        unknown = fp - set(KNOWN_FALSE_POSITIVES)
        assert not unknown, \
            "出现未记录的误报：%s\n（如果是新问题，先查根因再决定" \
            "是修还是记进 KNOWN_FALSE_POSITIVES）" % sorted(unknown)

    def test_known_fp_still_reproduces(self, baseline):
        """已知误报必须**还在误报**。

        如果它某天不误报了，说明指纹库变了 —— 那是好事，
        但要去更新 KNOWN_FALSE_POSITIVES 并复核其他样本，
        不能让它悄悄消失。
        """
        for name in KNOWN_FALSE_POSITIVES:
            if name not in baseline:
                pytest.skip("样本 %s 已不存在" % name)
            assert baseline[name]["detected"] is True, \
                "%s 不再误报了 —— 请复核并更新 KNOWN_FALSE_POSITIVES" % name

    def test_all_samples_have_score_and_reason(self, baseline):
        """★ 每个样本的 score 和 reason 都必须有记录（方便回归）。"""
        for name, r in baseline.items():
            assert isinstance(r["score"], (int, float)), name
            assert r["reason"], "%s 没有 reason" % name
            assert r["reason"] in (
                "hit", "below_threshold", "no_fingerprint", "empty"), \
                "%s 的 reason 是意料外的值：%s" % (name, r["reason"])

    def test_clean_samples_say_why(self, baseline):
        """判干净的样本必须能说清是哪种干净。"""
        for name, r in baseline.items():
            if not r["detected"]:
                assert r["reason"] in ("no_fingerprint", "below_threshold"), \
                    "%s 判干净但 reason=%s" % (name, r["reason"])

    def test_no_sample_scores_far_positive(self, baseline):
        """没有任何正常样本应该拿到很高的正分。

        "高分"意味着模型强烈认为这是验证题 —— 在正常页面上
        出现这种情况，说明指纹权重配得离谱。
        留 3.0 的余量（已知误报是 2.65）。
        """
        for name, r in baseline.items():
            assert r["score"] < 3.5, \
                "%s 得分 %.2f，正常页面不该这么高" % (name, r["score"])


# ==========================================================================
# 3. 消融测试（硬约束 #4）
# ==========================================================================
class TestAblation:
    """把某一档的惩罚清零，看误报率变化 —— 验证这一档**真的在干活**。"""

    def test_footer_ablation_raises_fp_rate(self, baseline):
        """★★ 硬约束 #4：footer 档清零后，误报率**必须上升**。

        ## 机制（这是真实发生的，不是造出来的）

        有几个样本靠页脚反证"压住"了正分指纹：

        - ``08_register.html``：页面上真有"获取验证码"按钮（+1.2），
          被 8 个页脚弱词（-2.4）压到 0.30，判干净。
          清零页脚 → 2.70，**误报**。
        - ``10_form_contact.html``：真有"请点击"（+1.0），
          被 7 个页脚弱词压到 -0.40。清零 → 1.70，**误报**。

        所以清零后误报数会从 1 涨到 3。

        ## 这条断言在守什么

        守"页脚反证"这一档**确实承担了误报压制的主要工作**。
        如果哪天有人觉得"页脚词扣分太狠"把它调轻、或者把页脚词
        从表里删掉，这条会红 —— 而那时候误报率会真的上升。
        """
        ablated = score_all(penalty_scale={WEAK_TIER_FOOTER: 0.0})
        fp_before = sum(1 for r in baseline.values() if r["detected"])
        fp_after = sum(1 for _, r in ablated if r["detected"])

        assert fp_after > fp_before, (
            "清零 footer 档后误报数没上升（%d → %d）—— "
            "说明页脚反证要么没生效，要么样本里根本没有页脚词。"
            "这是硬约束 #4 明确要求的。" % (fp_before, fp_after))

    def test_footer_ablation_changes_scores_materially(self, baseline):
        """不只是误报数变，**每个样本的分数都应该变**（页脚词遍地都是）。"""
        ablated = dict(score_all(penalty_scale={WEAK_TIER_FOOTER: 0.0}))
        changed = [n for n, r in baseline.items()
                   if abs(ablated[n]["score"] - r["score"]) > 1e-6]
        assert len(changed) >= 15, \
            "只有 %d 个样本分数变化，页脚档看起来没在起作用" % len(changed)

    def test_footer_ablation_moves_scores_upward(self, baseline):
        """清零惩罚 = 去掉扣分 → 分数**只会升高**，不会降低。"""
        ablated = dict(score_all(penalty_scale={WEAK_TIER_FOOTER: 0.0}))
        for name, r in baseline.items():
            assert ablated[name]["score"] >= r["score"] - 1e-6, \
                "%s 清零后分数反而降了（%.2f → %.2f）" % (
                    name, r["score"], ablated[name]["score"])

    def test_footer_penalty_is_the_largest_lever(self, baseline):
        """★ footer 档的杠杆**最大** —— 这正是"最有力的反证"的量化。

        三档分别清零，比较各自把平均分抬高了多少。
        footer 必须排第一（或者至少显著大于 ocr_noise）。
        """
        gains = {}
        for tier in (WEAK_TIER_FOOTER, WEAK_TIER_AMBIGUOUS,
                     WEAK_TIER_OCR_NOISE):
            abl = dict(score_all(penalty_scale={tier: 0.0}))
            total = sum(abl[n]["score"] - baseline[n]["score"]
                        for n in baseline)
            gains[tier] = total / max(1, len(baseline))

        assert gains[WEAK_TIER_FOOTER] > gains[WEAK_TIER_OCR_NOISE], \
            "footer 档的杠杆竟然不比 ocr_noise 大：%s" % gains
        assert gains[WEAK_TIER_FOOTER] > 0, "footer 档没起作用"

    def test_ambiguous_ablation_also_raises(self, baseline):
        """ambiguous 档清零也应该让误报数不减少（通常上升或持平）。"""
        abl = dict(score_all(penalty_scale={WEAK_TIER_AMBIGUOUS: 0.0}))
        fp_before = sum(1 for r in baseline.values() if r["detected"])
        fp_after = sum(1 for r in abl.values() if r["detected"])
        assert fp_after >= fp_before, \
            "ambiguous 档清零后误报反而少了？%d → %d" % (fp_before, fp_after)

    def test_zero_all_penalties_is_worst_case(self, baseline):
        """★ 三档全部清零 = 最坏情况，误报率最高。

        这条给"弱词扣分机制整体值多少钱"一个上限估计。
        """
        abl = dict(score_all(penalty_scale={
            WEAK_TIER_FOOTER: 0.0,
            WEAK_TIER_AMBIGUOUS: 0.0,
            WEAK_TIER_OCR_NOISE: 0.0,
        }))
        fp_all_off = sum(1 for r in abl.values() if r["detected"])
        fp_baseline = sum(1 for r in baseline.values() if r["detected"])
        assert fp_all_off >= fp_baseline, \
            "全清零反而误报少了？%d → %d" % (fp_baseline, fp_all_off)
        assert fp_all_off >= 2, \
            "全清零只有 %d 个误报，说明弱词机制影响很小（或样本太干净）" \
            % fp_all_off

    def test_doubling_footer_penalty_does_not_hurt(self, baseline):
        """★ 反过来：**加倍** footer 惩罚不该引入新问题。

        加倍惩罚会让分数更低 → 误报更少。但要确认它不会
        把某些本该检出（虽然这里都是正常样本）的分数压得离谱。
        这里只断言"不引入新误报"。
        """
        abl = dict(score_all(penalty_scale={WEAK_TIER_FOOTER: 2.0}))
        fp = sum(1 for r in abl.values() if r["detected"])
        assert fp <= MAX_FALSE_POSITIVES, \
            "加倍页脚惩罚反而出现 %d 个误报？" % fp

    def test_ablation_does_not_mutate_baseline(self, baseline):
        """消融是纯函数 —— 跑完不该改掉基线结果。"""
        before = {n: r["score"] for n, r in baseline.items()}
        score_all(penalty_scale={WEAK_TIER_FOOTER: 0.0})
        after = {n: r["score"] for n, r in baseline.items()}
        assert before == after


# ==========================================================================
# 4. 回归基线
# ==========================================================================
class TestRegressionBaseline:
    """每个样本的分数落盘，改动指纹库时能 diff 出"谁变了"。"""

    def _path(self):
        return os.path.join(FIXTURE_DIR, "_baseline.json")

    def test_baseline_file_is_valid_if_present(self, baseline):
        p = self._path()
        if not os.path.isfile(p):
            pytest.skip("尚无基线文件（首次运行正常）")
        with open(p, encoding="utf-8") as f:
            saved = json.load(f)
        assert set(saved.get("samples", {})) == set(baseline), \
            "样本集变了，请重新生成基线"

    def test_scores_match_baseline_if_present(self, baseline):
        """★ 分数与基线比对 —— 偏差超过 0.01 就说明指纹库变了。

        红了**不一定是坏事**（可能是修好了），但必须人工复核
        每个变化，确认是有意的。
        """
        p = self._path()
        if not os.path.isfile(p):
            pytest.skip("尚无基线文件（首次运行正常）")
        with open(p, encoding="utf-8") as f:
            saved = json.load(f).get("samples", {})

        drifted = []
        for name, r in baseline.items():
            old = saved.get(name)
            if old is None:
                continue
            if abs(float(old["score"]) - float(r["score"])) > 0.01:
                drifted.append("%s: %.2f → %.2f"
                               % (name, old["score"], r["score"]))
        assert not drifted, \
            "分数漂移（复核后重新生成基线）：\n  " + "\n  ".join(drifted)

    def test_report_is_reproducible(self, baseline):
        """同一份样本跑两次，结果必须完全一致（不能有随机性）。"""
        again = {n: text_hit_score(t) for n, t in load_samples()}
        for name in baseline:
            assert again[name]["score"] == baseline[name]["score"], name
            assert again[name]["detected"] == baseline[name]["detected"], name


# ==========================================================================
# 5. 汇总报告（人看的）
# ==========================================================================
class TestReport:
    def test_report_is_written_and_readable(self, baseline):
        """★ 报告落盘 —— "每个样本的 score 和 reason 必须记录"的落点。

        和 ``_baseline.json``（给机器 diff）互补：这份是给人读的。
        红了的时候直接打开看，不用去翻 pytest 输出。
        """
        path = write_report()
        assert os.path.isfile(path), "报告没生成"

        with open(path, encoding="utf-8") as f:
            body = f.read()

        # 每个样本都要在报告里
        for name in baseline:
            assert name in body, "报告里缺 %s" % name

        # 关键结论要在
        assert "误报率" in body
        assert "清零 footer" in body
        assert "已知误报" in body

        # 20 行数据行
        rows = [ln for ln in body.splitlines()
                if ln.startswith(tuple(baseline.keys())[:1][:1])
                or re.match(r"^\d\d_\w+\.html\s", ln)]
        assert len(rows) == 20, "报告里只有 %d 行样本" % len(rows)

    def test_report_mentions_known_fp(self):
        path = write_report()
        with open(path, encoding="utf-8") as f:
            body = f.read()
        for name in KNOWN_FALSE_POSITIVES:
            assert name in body, "已知误报 %s 没写进报告" % name


def write_report(path: str = "") -> str:
    """生成人读的误报率报告。返回文件路径。

    测试会调它（保证报告是最新的），也可以手动跑::

        python -c "from tests.test_phase5_false_positive import write_report; \\
                   print(write_report())"
    """
    import time as _t
    baseline = dict(score_all())
    ablated = dict(score_all(penalty_scale={WEAK_TIER_FOOTER: 0.0}))

    lines = [
        "正常页面误报率报告（text 层）",
        "=" * 100,
        "生成时间：%s" % _t.strftime("%Y-%m-%d %H:%M:%S"),
        "样本目录：tests/fixtures/normal_pages/（%d 个）" % len(baseline),
        "",
        "%-26s %9s %-5s %-17s %9s %-5s %s"
        % ("样本", "score", "误报", "reason", "清零后", "误报", "弱词分档"),
        "-" * 100,
    ]

    fp = fp_abl = 0
    for name, r in baseline.items():
        a = ablated[name]
        is_fp, is_fp_abl = r["detected"], a["detected"]
        fp += 1 if is_fp else 0
        fp_abl += 1 if is_fp_abl else 0
        lines.append("%-26s %9.2f %-5s %-17s %9.2f %-5s %s"
                     % (name, r["score"], "★" if is_fp else "",
                        r["reason"], a["score"],
                        "★" if is_fp_abl else "", r["tiers"]))

    n = len(baseline) or 1
    lines += [
        "-" * 100,
        "误报率          ：%d/%d = %.1f%%（上限 %d 个 / %.0f%%）"
        % (fp, n, 100.0 * fp / n, MAX_FALSE_POSITIVES, MAX_FP_RATE),
        "清零 footer 后  ：%d/%d = %.1f%%   ← 消融对比（必须上升）"
        % (fp_abl, n, 100.0 * fp_abl / n),
        "",
        "消融：footer 档清零后多出的误报 —— 说明这些样本原本是被页脚反证压住的",
        "（这一档确实在干活，不是摆设）",
    ]
    for name, r in baseline.items():
        a = ablated[name]
        if a["detected"] and not r["detected"]:
            lines.append("  + %-26s %.2f → %.2f" % (name, r["score"],
                                                   a["score"]))
    lines.append("")

    lines.append("命中明细（原版）")
    lines.append("-" * 100)
    for name, r in baseline.items():
        if r["hits"]:
            lines.append("  %-26s %s" % (
                name, ", ".join("%s(%.1f)" % (h["id"], h["weight"])
                                for h in r["hits"])))
    lines.append("")

    lines.append("已知误报（刻意保留，未调参压掉）")
    lines.append("-" * 100)
    for k, v in KNOWN_FALSE_POSITIVES.items():
        lines.append("  · %s" % k)
        lines.append("    %s" % v)
    lines.append("")

    p = path or os.path.join(FIXTURE_DIR, "_report.txt")
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return p


# ==========================================================================
# 7. 单独生成基线的小工具（不是测试）
# ==========================================================================
def write_baseline(path: str = "") -> str:
    """生成/刷新基线文件。**手动调用**，不要在测试里调。

    ::

        python -c "from tests.test_phase5_false_positive import write_baseline; \\
                   write_baseline()"
    """
    import time as _t
    data = {
        "generated_at": _t.strftime("%Y-%m-%d %H:%M:%S"),
        "note": "正常页面样本的 text 层打分基线。指纹库改动后请复核并重新生成。",
        "samples": {},
    }
    for name, r in score_all():
        data["samples"][name] = {
            "score": r["score"],
            "detected": r["detected"],
            "reason": r["reason"],
            "tiers": r["tiers"],
            "hits": [h["id"] for h in r["hits"]],
        }
    p = path or os.path.join(FIXTURE_DIR, "_baseline.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return p
