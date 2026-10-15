# -*- coding: utf-8 -*-
"""一次性脚本：生成 20 个正常页面的本地 HTML 样本。

这些样本是 **5G 误报率测试的基准**。设计原则：

1. **必须是真实的正常页面**，不是"为了通过测试而写干净文本"。
   每个样本都带完整页脚（真实站点都有），页脚里全是 footer 档弱词。
2. **要包含真实的危险词**。注册页真的有"获取验证码"按钮，
   银行页面真的有"风险控制"，地图页面真的有"拖动"。
   把这些去掉就是在自欺欺人 —— 误报率测试的意义正是"带危险词还能判干净"。
3. **不依赖网络、不依赖 Chrome**。纯 HTML 文件，测试时剥标签取文本。

生成后**不再需要这个脚本**，样本文件是交付物。
"""

import os

OUT = os.path.join("tests", "fixtures", "normal_pages")

#: (文件名, 页面标题, <body> 内容)
PAGES = [
    ("01_shop_home.html", "星辰优选 - 首页", """
<header class="site-header">
  <a class="logo" href="/">星辰优选</a>
  <nav><a href="/new">新品</a> <a href="/sale">促销</a> <a href="/brand">品牌</a></nav>
  <div class="user"><a href="/login">登录</a> <a href="/register">注册</a></div>
</header>
<main>
  <h1>今日推荐</h1>
  <ul class="products">
    <li>无线蓝牙耳机 ¥299</li>
    <li>便携充电宝 20000mAh ¥159</li>
    <li>机械键盘 87 键 ¥399</li>
    <li>人体工学椅 ¥1299</li>
  </ul>
  <section class="promo">全场满 199 减 30，活动时间 10 月 1 日至 10 月 7 日。</section>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <a href="/help">帮助中心</a>
  <p>本站使用 cookie 改善浏览体验。</p>
  <p>版权所有 © 2026 星辰优选 保留一切权利</p>
</footer>
"""),

    ("02_shop_product.html", "无线蓝牙耳机 - 星辰优选", """
<header><a href="/login">登录</a> <a href="/register">注册</a></header>
<main>
  <h1>无线蓝牙耳机 Pro</h1>
  <p class="price">¥299.00</p>
  <p class="desc">主动降噪，续航 36 小时，支持多设备连接。佩戴舒适，适合通勤与办公。</p>
  <h2>规格参数</h2>
  <ul><li>蓝牙版本：5.3</li><li>续航：36 小时</li><li>重量：4.2g（单耳）</li></ul>
  <h2>用户评价</h2>
  <p>音质不错，降噪效果超出预期。 —— 用户 A</p>
  <p>戴着很舒服，续航确实够用。 —— 用户 B</p>
  <button>加入购物车</button> <button>立即购买</button>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权 © 2026 星辰优选</p>
</footer>
"""),

    ("03_shop_cart.html", "购物车 - 星辰优选", """
<header><a href="/login">登录</a></header>
<main>
  <h1>我的购物车</h1>
  <table>
    <tr><th>商品</th><th>单价</th><th>数量</th><th>小计</th></tr>
    <tr><td>无线蓝牙耳机 Pro</td><td>¥299.00</td><td>1</td><td>¥299.00</td></tr>
    <tr><td>便携充电宝</td><td>¥159.00</td><td>2</td><td>¥318.00</td></tr>
  </table>
  <p class="total">合计：¥617.00</p>
  <button>去结算</button>
  <p class="tip">满 199 免运费，本单已满足。</p>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权所有 © 2026 星辰优选</p>
</footer>
"""),

    ("04_shop_checkout.html", "结算 - 星辰优选", """
<header><a href="/login">登录</a> <a href="/register">注册</a></header>
<main>
  <h1>订单结算</h1>
  <form>
    <label>收货人 <input name="name"></label>
    <label>手机号 <input name="phone"></label>
    <label>收货地址 <input name="addr"></label>
    <label>发票抬头 <input name="invoice"></label>
    <button type="submit">提交订单</button>
  </form>
  <p class="note">提交后可在"我的订单"查看物流。</p>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权 © 2026 星辰优选</p>
</footer>
"""),

    ("05_news_article.html", "城市轨道交通新线路开通 - 晨报", """
<header>
  <a href="/">晨报</a>
  <nav><a href="/news">要闻</a> <a href="/finance">财经</a> <a href="/tech">科技</a></nav>
  <a href="/login">登录</a>
</header>
<main>
  <h1>城市轨道交通新线路今日正式开通运营</h1>
  <p class="meta">2026-10-01 08:30　来源：晨报　记者：李明</p>
  <article>
    <p>今日上午，本市轨道交通新线路正式开通运营。线路全长 32.6 公里，共设车站 18 座，
       贯穿城市东西主干道，串联起三个主要居住区与两个产业园区。</p>
    <p>据运营方介绍，新线路采用全自动运行系统，高峰期最小发车间隔可达 2 分钟。
       全线配备无障碍电梯与母婴室，站台门同步启用。</p>
    <p>市民王女士表示，新线路开通后，她每天通勤时间缩短了约 40 分钟。
       运营首日客流平稳，各项设备运转正常。</p>
    <p>相关负责人表示，后续将根据客流情况动态调整运力，并持续完善接驳公交线路。</p>
  </article>
  <section class="related">
    <h2>相关阅读</h2>
    <ul><li>轨道交通票价调整方案公开征求意见</li><li>全市公交线路优化调整公告</li></ul>
  </section>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <a href="/jobs">加入我们</a>
  <p>版权所有 © 2026 晨报　未经许可不得转载</p>
</footer>
"""),

    ("06_news_list.html", "要闻 - 晨报", """
<header><a href="/">晨报</a> <a href="/login">登录</a></header>
<main>
  <h1>要闻</h1>
  <ul class="list">
    <li><a href="/a1">城市轨道交通新线路今日开通</a> <span>08:30</span></li>
    <li><a href="/a2">秋季招聘会本周末举行 提供岗位逾万个</a> <span>09:15</span></li>
    <li><a href="/a3">本地农产品电商平台上线 助力乡村振兴</a> <span>10:02</span></li>
    <li><a href="/a4">图书馆延长开放时间 周末至 21 时</a> <span>11:40</span></li>
    <li><a href="/a5">空气质量优良天数同比增加 12 天</a> <span>14:20</span></li>
  </ul>
  <nav class="pager"><a href="?p=1">1</a> <a href="?p=2">2</a> <a href="?p=3">3</a> 下一页</nav>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权所有 © 2026 晨报</p>
</footer>
"""),

    ("07_login.html", "登录 - 星辰优选", """
<header><a href="/">星辰优选</a></header>
<main class="auth">
  <h1>账号登录</h1>
  <form>
    <label>手机号 / 邮箱 <input name="account" type="text"></label>
    <label>密码 <input name="password" type="password"></label>
    <label class="remember"><input type="checkbox"> 记住我</label>
    <button type="submit">登录</button>
  </form>
  <p><a href="/forgot">忘记密码？</a> <a href="/register">还没有账号？立即注册</a></p>
  <p class="note">为保护您的账号安全，请勿在公共设备上保存密码。</p>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权 © 2026 星辰优选</p>
</footer>
"""),

    ("08_register.html", "注册 - 星辰优选", """
<header><a href="/">星辰优选</a> <a href="/login">登录</a></header>
<main class="auth">
  <h1>注册新账号</h1>
  <form>
    <label>手机号 <input name="phone" type="tel"></label>
    <button type="button" class="send">获取验证码</button>
    <label>验证码 <input name="code" type="text" maxlength="6"></label>
    <label>设置密码 <input name="password" type="password"></label>
    <label>确认密码 <input name="password2" type="password"></label>
    <label class="agree"><input type="checkbox"> 我已阅读并同意
      <a href="/terms">服务条款</a> 与 <a href="/privacy">隐私政策</a></label>
    <button type="submit">注册</button>
  </form>
  <p>已有账号？<a href="/login">直接登录</a></p>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权 © 2026 星辰优选</p>
</footer>
"""),

    ("09_forgot_password.html", "找回密码 - 星辰优选", """
<header><a href="/">星辰优选</a> <a href="/login">登录</a></header>
<main class="auth">
  <h1>找回密码</h1>
  <p>请输入注册时使用的手机号，我们会发送验证码用于验证您的身份。</p>
  <form>
    <label>手机号 <input name="phone" type="tel"></label>
    <label>验证码 <input name="code"></label>
    <button type="submit">下一步</button>
  </form>
  <p class="note">收不到验证码？请检查短信拦截设置，或稍后重试。</p>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权 © 2026 星辰优选</p>
</footer>
"""),

    ("10_form_contact.html", "联系我们 - 星辰优选", """
<header><a href="/">星辰优选</a></header>
<main>
  <h1>联系我们</h1>
  <p>如需帮助，请填写下面的表单，我们会在 1 个工作日内回复。</p>
  <form>
    <label>您的姓名 <input name="name"></label>
    <label>邮箱 <input name="email" type="email"></label>
    <label>问题类型
      <select name="type"><option>订单问题</option><option>退换货</option><option>其他</option></select>
    </label>
    <label>详细描述 <textarea name="msg" rows="5"></textarea></label>
    <button type="submit">提交</button> <button type="reset">取消</button>
  </form>
  <p>提交后请留意邮件通知。也可以直接拨打客服电话 400-000-0000。</p>
  <p class="tip">请点击页面右上角的"登录"查看您的历史工单。</p>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权所有 © 2026 星辰优选</p>
</footer>
"""),

    ("11_privacy_policy.html", "隐私政策 - 星辰优选", """
<header><a href="/">星辰优选</a> <a href="/login">登录</a></header>
<main>
  <h1>隐私政策</h1>
  <p>更新日期：2026 年 9 月 1 日</p>
  <h2>一、我们收集哪些信息</h2>
  <p>当您注册账号、下单或联系我们时，我们可能收集您的手机号、收货地址、
     订单信息与设备信息。这些信息用于完成交易、提供客服与改进服务。</p>
  <h2>二、我们如何使用 cookie</h2>
  <p>本站使用 cookie 与类似技术记住您的登录状态、购物车内容与偏好设置。
     您可以在浏览器设置中清除或禁用 cookie，但这可能影响部分功能。</p>
  <h2>三、信息共享</h2>
  <p>除为完成配送而向物流服务商提供必要信息外，我们不会向第三方出售您的个人信息。</p>
  <h2>四、您的权利</h2>
  <p>您可以随时登录账号查看、更正或删除您的个人信息，也可以注销账号。</p>
  <h2>五、联系我们</h2>
  <p>如对本政策有疑问，请通过"联系我们"页面与我们取得联系。</p>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权所有 © 2026 星辰优选　保留一切权利</p>
</footer>
"""),

    ("12_terms_of_service.html", "服务条款 - 星辰优选", """
<header><a href="/">星辰优选</a> <a href="/register">注册</a></header>
<main>
  <h1>用户协议与 terms of service</h1>
  <h2>1. 协议范围</h2>
  <p>本协议是您与星辰优选之间就使用本站服务所订立的约定。注册即表示您已阅读并同意本协议。</p>
  <h2>2. 账号与安全</h2>
  <p>您应妥善保管账号与密码。因您保管不善导致的损失由您自行承担。
     如发现账号被盗用，请立即联系我们。</p>
  <h2>3. 订单与支付</h2>
  <p>订单在您完成支付后成立。我们保留在库存不足等情况下取消订单的权利，届时将全额退款。</p>
  <h2>4. 退换货</h2>
  <p>自签收之日起 7 日内，商品保持完好可申请无理由退货。定制类商品除外。</p>
  <h2>5. 知识产权</h2>
  <p>本站的文字、图片、标识等内容的版权归星辰优选或相应权利人所有。</p>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权 © 2026 星辰优选</p>
</footer>
"""),

    ("13_help_center.html", "帮助中心 - 星辰优选", """
<header><a href="/">星辰优选</a> <a href="/login">登录</a></header>
<main>
  <h1>帮助中心</h1>
  <section>
    <h2>订单相关</h2>
    <ul>
      <li><a href="/h1">如何查询订单状态？</a></li>
      <li><a href="/h2">可以修改收货地址吗？</a></li>
      <li><a href="/h3">如何取消订单？</a></li>
    </ul>
  </section>
  <section>
    <h2>配送与售后</h2>
    <ul>
      <li><a href="/h4">配送需要多长时间？</a></li>
      <li><a href="/h5">如何申请退换货？</a></li>
      <li><a href="/h6">退款多久到账？</a></li>
    </ul>
  </section>
  <section>
    <h2>账号问题</h2>
    <ul>
      <li><a href="/h7">忘记密码怎么办？</a></li>
      <li><a href="/h8">如何修改绑定手机号？</a></li>
    </ul>
  </section>
  <p>没找到答案？<a href="/contact">联系我们</a></p>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权所有 © 2026 星辰优选</p>
</footer>
"""),

    ("14_error_404.html", "页面不存在 - 星辰优选", """
<header><a href="/">星辰优选</a> <a href="/login">登录</a></header>
<main class="error">
  <h1>404</h1>
  <p>抱歉，您访问的页面不存在或已被移除。</p>
  <p>可能的原因：链接已过期、地址输入有误，或商品已下架。</p>
  <p><a href="/">返回首页</a>　<a href="/help">去帮助中心</a></p>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权 © 2026 星辰优选</p>
</footer>
"""),

    ("15_about_us.html", "关于我们 - 星辰优选", """
<header><a href="/">星辰优选</a> <a href="/login">登录</a></header>
<main>
  <h1>关于我们</h1>
  <p>星辰优选成立于 2018 年，是一家专注于消费电子的线上零售商。
     我们与超过 300 个品牌建立合作，服务用户逾 500 万。</p>
  <h2>我们的理念</h2>
  <p>只卖我们自己愿意用的产品。每一件上架商品都经过团队实测。</p>
  <h2>联系我们</h2>
  <p>商务合作：bd@example.com</p>
  <p>客服邮箱：support@example.com</p>
  <p>公司地址：某市某区某路 100 号</p>
  <h2>加入我们</h2>
  <p>我们长期招聘产品、研发与客服岗位，欢迎投递简历。</p>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权所有 © 2026 星辰优选</p>
</footer>
"""),

    ("16_bank_dashboard.html", "个人网银 - 账户总览", """
<header class="bank">
  <span class="brand">某银行个人网银</span>
  <a href="/logout">退出登录</a>
</header>
<main>
  <h1>账户总览</h1>
  <p>尊敬的客户，欢迎回来。上次登录时间：2026-09-30 21:14</p>
  <section class="accounts">
    <h2>我的账户</h2>
    <ul>
      <li>活期储蓄账户 ****3821　余额 ¥28,450.12</li>
      <li>定期存款账户 ****7764　余额 ¥100,000.00</li>
    </ul>
  </section>
  <section class="safety">
    <h2>安全提示</h2>
    <p>为保障您的资金安全，我行建立了完善的风险控制机制。
       如发现账户操作异常，系统将自动发送短信提醒。</p>
    <p>请勿向任何人透露您的密码、短信验证码或 U 盾密码。
       银行工作人员不会以任何理由向您索取上述信息。</p>
    <p>建议定期修改登录密码，并开通账户变动短信通知。</p>
  </section>
  <section class="links">
    <a href="/transfer">转账汇款</a> <a href="/bill">账单查询</a>
    <a href="/settings">安全设置</a>
  </section>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <a href="/security">安全中心</a>
  <p>版权所有 © 2026 某银行　请认准官方网站，谨防钓鱼</p>
</footer>
"""),

    ("17_corp_home.html", "启明科技 - 首页", """
<header>
  <a class="logo" href="/">启明科技</a>
  <nav><a href="/product">产品</a> <a href="/solution">解决方案</a> <a href="/case">客户案例</a> <a href="/news">新闻</a></nav>
  <a href="/login">登录</a> <a href="/register">注册</a>
</header>
<main>
  <h1>让数据流动起来</h1>
  <p>启明科技为企业提供数据集成、治理与分析平台，帮助客户把分散的数据
     变成可用的决策依据。</p>
  <h2>核心产品</h2>
  <ul>
    <li><strong>启明数据集成</strong> —— 支持 60+ 数据源的实时同步</li>
    <li><strong>启明数据治理</strong> —— 元数据管理、血缘追踪与质量监控</li>
    <li><strong>启明分析云</strong> —— 拖拽式报表与自助分析</li>
  </ul>
  <h2>客户案例</h2>
  <p>某大型零售企业通过我们的平台，将报表产出周期从 5 天缩短到 2 小时。</p>
  <p>某制造企业实现了 12 个工厂数据的统一接入与实时监控。</p>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <a href="/jobs">加入我们</a>
  <p>版权所有 © 2026 启明科技</p>
</footer>
"""),

    ("18_blog_post.html", "如何用 Python 处理 CSV 大文件 - 技术博客", """
<header><a href="/">技术博客</a> <a href="/login">登录</a></header>
<main>
  <h1>如何用 Python 处理 CSV 大文件</h1>
  <p class="meta">作者：张伟　2026-09-28　阅读 12480</p>
  <article>
    <p>当 CSV 文件超过内存容量时，直接 <code>pd.read_csv</code> 会撑爆内存。
       本文介绍三种实用的流式处理方式。</p>
    <h2>方法一：分块读取</h2>
    <p>pandas 的 <code>chunksize</code> 参数可以把文件切成若干块，
       每块处理完就释放，内存占用可控。</p>
    <h2>方法二：csv 模块逐行处理</h2>
    <p>标准库的 csv 模块最省内存，适合只需要做简单聚合的场景。</p>
    <h2>方法三：先转成 Parquet</h2>
    <p>如果同一份数据要反复查询，先转成列式存储再处理，速度会有明显提升。</p>
    <h2>小结</h2>
    <p>没有万能方案。文件小于 2GB 用分块足够；需要复杂计算建议先转 Parquet。</p>
  </article>
  <section class="comments">
    <h2>评论（3）</h2>
    <p>写得清楚，方法三我一直在用。</p>
    <p>补充一点：注意编码问题，有些 CSV 是 GBK。</p>
  </section>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权所有 © 2026 技术博客</p>
</footer>
"""),

    ("19_jobs_page.html", "加入我们 - 启明科技", """
<header><a href="/">启明科技</a> <a href="/login">登录</a></header>
<main>
  <h1>加入我们</h1>
  <p>我们是一支 80 人的团队，相信好的工具能让人把时间花在真正重要的事情上。</p>
  <h2>在招职位</h2>
  <section class="job">
    <h3>后端工程师（Python）</h3>
    <p>负责数据同步服务的开发与维护。要求熟悉 Python、熟悉常见数据库，
       有高并发经验者优先。</p>
  </section>
  <section class="job">
    <h3>前端工程师</h3>
    <p>负责控制台产品的前端开发。要求熟悉 TypeScript 与现代前端框架。</p>
  </section>
  <section class="job">
    <h3>技术支持工程师</h3>
    <p>负责客户问题排查与方案讲解。要求沟通能力强，能读懂日志与 SQL。</p>
  </section>
  <h2>投递方式</h2>
  <p>请将简历发送至 jobs@example.com，邮件标题格式：应聘职位-姓名。</p>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权所有 © 2026 启明科技</p>
</footer>
"""),

    ("20_docs_page.html", "快速开始 - 启明文档", """
<header><a href="/">启明文档</a> <a href="/login">登录</a></header>
<main>
  <h1>快速开始</h1>
  <p>本文用 5 分钟带你把第一条数据同步跑起来。</p>
  <h2>第一步：安装客户端</h2>
  <pre>pip install qiming-client</pre>
  <h2>第二步：配置连接</h2>
  <p>在控制台创建数据源后，会得到一串连接串。把它填进配置文件：</p>
  <pre>endpoint = "https://api.example.com"
token = "your-token"</pre>
  <h2>第三步：启动同步</h2>
  <pre>qiming sync --config ./config.toml</pre>
  <p>首次同步会做全量拉取，之后自动切换为增量。</p>
  <h2>常见问题</h2>
  <p><strong>同步卡在"正在初始化"？</strong> 检查网络是否能访问 endpoint，
     以及 token 是否过期。</p>
  <p><strong>数据对不上？</strong> 检查源表是否有主键变更，或时区设置是否一致。</p>
  <h2>下一步</h2>
  <p>阅读<a href="/advanced">进阶配置</a>了解分片、重试与监控。</p>
</main>
<footer>
  <a href="/terms">服务条款</a> <a href="/privacy">隐私政策</a>
  <a href="/about">关于我们</a> <a href="/contact">联系我们</a>
  <p>版权所有 © 2026 启明科技</p>
</footer>
"""),
]


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, title, body in PAGES:
        html = (
            "<!DOCTYPE html>\n"
            "<html lang=\"zh-CN\">\n"
            "<head>\n"
            "  <meta charset=\"utf-8\">\n"
            "  <title>%s</title>\n"
            "</head>\n"
            "<body>\n"
            "%s"
            "</body>\n"
            "</html>\n"
        ) % (title, body)
        path = os.path.join(OUT, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(html)
    print("生成 %d 个样本 → %s" % (len(PAGES), OUT))


if __name__ == "__main__":
    main()
