# OPEN-QUESTIONS

不明确的业务/技术事实，未猜测；原型用了下列「保守默认值」继续。

| # | 问题 | 原型当前的处理 |
|---|---|---|
| Q-01 | PHP 版本：需求输入写 PHP 8.2-fpm，但 Magento 2.4.8 只支持 PHP 8.3/8.4（8.2 只在 2.4.7）。项目要使用的 Magento 小版本是什么？ | 2.4.8-p5 + PHP 8.3（`config/*.json` 的 `magento_ref`、`php_image`） |
| Q-02 | 是否使用 Adobe Commerce（商业版）或第三方付费扩展？是否需要 `repo.magento.com` 访问密钥？ | 只用 Open Source，从官方 Git 仓库构建；密钥流程未实现 |
| Q-03 | 站点数与 MCF 市场：美/欧/日分别是独立 Magento website 还是一个站点多市场？各市场 MSKU 是否不同？卖家在各 SP-API 区域的授权（refresh token）如何管理？ | SKU 映射表按 (storefront SKU, marketplace) 建键；市场配置带区域，每个区域一个客户端与 `lwa_refresh_token_<REGION>`；沙箱示例有 US（na）与 DE（eu） |
| Q-04 | 履约策略默认 FillOrKill 还是 FillAll？是否需要部分发货（FillAllAvailable）？ | 默认 FillOrKill，订单事件可覆盖 |
| Q-05 | Magento 侧如何发出「已支付」「已取消」事件、如何消费「已发货/已取消/库存/异常」事件（自研模块、第三方集成平台还是消息中间件）？发货邮件由谁发？ | 定义了事件契约（EventBridge 总线 + `sample-data/orders.json`，事件带 `eventId` 供去重）；Magento 模块未实现；桥接输出写入日志组作为替身 |
| Q-06 | 库存同步频率（需求输入写 1–4 小时）与「可售为 0 自动缺货」的阈值、安全库存？下单锁库存由 Magento 还是桥接负责？ | 默认每 60 分钟；数量为 0 时事件里 `qty=0`，由店铺侧决定上下架；锁库存留给 Magento 的预留机制 |
| Q-07 | 支付网关、税费、运费估算服务的选型与回调地址 | 不在原型范围（Magento 自带/第三方扩展） |
| Q-08 | 环境与账号：是否最终拆分生产账号（Organizations 跨账号部署）？uat 的脱敏数据副本由谁、如何生成？ | 单账号多 VPC + 命名前缀；跨账号与脱敏流程未实现 |
| Q-09 | 跨区域：是否需要多区域部署、静态资源/媒体跨区域分发？目标区域与数据驻留要求？ | 单区域；媒体放 EFS；未做 CloudFront/S3 媒体分发 |
| Q-12 | OSL-3.0：若对外分发构建好的 Magento 镜像，分发方的合规义务（源码提供等）由谁评估？ | 本仓库不分发 Magento 代码或镜像 |
| Q-14 | 生产流量、峰值、SLA、RPO/RTO 与合规要求（PCI 范围等） | 未知；交付配置的实例类留空待填，未做容量规划 |
| Q-15 | 后台访问方式（VPN/Client VPN/堡垒/零信任）与管理员入口路径 | ALB 仅内网；后台路径按环境注入 SSM 参数 |
| Q-16 | HTTPS 环境的主机名、证书（ACM，部署区域内）与私有 DNS 记录由谁提供与维护？ | `site_hostname` 与 `certificate_arn` 为必填参数；指向内部 ALB 的私有 DNS 记录不在栈内创建 |
| Q-17 | 部署失败且 schema 已升级时，uat/prod 是保持维护模式等待人工恢复，还是允许旧代码临时服务？ | 交付配置 uat/prod `maintenance_on_failure=keep`，dev/test `disable`（DECISIONS D-42） |
| Q-18 | MCF 取消的业务规则：店铺侧在什么状态允许取消？发货后取消是否自动转退货？ | 发货前调用 `cancelFulfillmentOrder`；发货后拒绝并发异常事件，退货流程未实现 |
| Q-19 | 仓库发布后的维护者 GitHub 账号（`.github/CODEOWNERS` 中的 `@<MAINTAINER_GITHUB_HANDLE>`），以及 CI/部署验证 workflow 所需的 GitHub environment（`sandbox-deploy` 的审批人）与 OIDC 角色 | 占位符；workflow 未在 GitHub 上运行 |

已关闭：
- 原 Q-13（SP-API 是否需要 AWS SigV4 签名）：官方「Connecting to the Selling Partner API」页面的调用步骤只有 LWA 访问令牌、构造 URI、添加请求头（`x-amz-access-token`、`x-amz-date`、`user-agent`），示例写明「no signing information」，未出现 SigV4/IAM 步骤（F-18，2026-10-20 核实）。live 客户端按此只实现 LWA，并发送上述请求头。
