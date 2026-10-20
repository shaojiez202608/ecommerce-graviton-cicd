# OPEN-QUESTIONS

不明确的业务事实，未猜测；原型用了下列「保守默认值」继续。

| # | 问题 | 原型当前的处理 |
|---|---|---|
| Q-01 | PHP 版本：补充材料写 PHP 8.2-fpm，但 Magento 2.4.8 只支持 PHP 8.3/8.4（8.2 只在 2.4.7）。客户锁定的 Magento 小版本是什么？ | 2.4.8-p5 + PHP 8.3（`config/*.json` 的 `magento_ref`、`php_image`） |
| Q-02 | 是否使用 Adobe Commerce（商业版）或第三方付费扩展？是否需要 `repo.magento.com` 访问密钥？ | 只用 Open Source，从官方 Git 仓库构建；密钥流程未实现 |
| Q-03 | 站点数与 MCF 市场：美/欧/日分别是独立 Magento website 还是一个站点多市场？各市场 MSKU 是否不同？ | SKU 映射表按 (storefront SKU, marketplace) 建键；示例只有 US |
| Q-04 | 履约策略默认 FillOrKill 还是 FillAll？是否需要部分发货（FillAllAvailable）？ | 默认 FillOrKill，订单事件可覆盖 |
| Q-05 | Magento 侧如何发出「已支付」事件、如何消费「已发货/库存」事件（自研模块、第三方集成平台还是消息中间件）？发货邮件由谁发？ | 定义了事件契约（EventBridge 总线 + `sample-data/orders.json`）；Magento 模块未实现；桥接输出写入日志组作为替身 |
| Q-06 | 库存同步频率（材料写 1–4 小时）与「可售为 0 自动缺货」的阈值、安全库存？ | 默认每 60 分钟；数量为 0 时事件里 `qty=0`，由店铺侧决定上下架 |
| Q-07 | 支付网关、税费、运费估算服务的选型与回调地址 | 不在原型范围（Magento 自带/第三方扩展） |
| Q-08 | 环境与账号：是否最终拆分生产账号（Organizations 跨账号部署）？uat 的脱敏数据副本由谁、如何生成？ | 单账号多 VPC + 命名前缀；跨账号与脱敏流程未实现 |
| Q-09 | 跨区域：是否需要多区域部署、静态资源/媒体跨区域分发？目标区域与数据驻留要求？ | 单区域；媒体放 EFS；未做 CloudFront/S3 媒体分发 |
| Q-10 | Prior-art 补检：本次环境无法使用通用搜索引擎，AWS 博客/Prescriptive Guidance 中是否有更新的 Magento on Graviton 或 Magento CI/CD 文章？ | 需负责人发布前人工检索确认 |
| Q-11 | 对外公开前，客户是否同意以抽象形式公开（spec 第 8 节）？ | 仓库内已全部使用通用说法 |
| Q-12 | OSL-3.0：若对外分发构建好的 Magento 镜像，分发方的合规义务（源码提供等）由谁评估？ | 本仓库不分发 Magento 代码或镜像 |
| Q-13 | SP-API 调用是否仍需 AWS SigV4 签名？本次读到的官方连接文档只描述 LWA 访问令牌与 `x-amz-access-token` 头，未明确说明 | live 客户端只实现 LWA 令牌；接真实账户前需确认 |
| Q-14 | 生产流量、峰值、SLA、RPO/RTO 与合规要求（PCI 范围等） | 未知；规格均为占位值，未做容量规划 |
| Q-15 | 后台访问方式（VPN/Client VPN/堡垒/零信任）与管理员入口路径 | ALB 仅内网；后台路径按环境注入 SSM 参数 |
