# Prior-art 检索与差异结论

检索日期：2026-10-20。检索方式：GitHub API（`aws-samples`、`aws-solutions-library-samples`、`awslabs`、`aws-quickstart`、`amzn` 组织）+ 直接读取官方文档页面。通用网页搜索引擎在本次环境中不可用（返回验证码页），因此博客类资产覆盖不完整，见文末「检索局限」。

## Step 0-1：开源电商框架的许可与官方系统要求

| 项 | 结论 | 来源 |
|---|---|---|
| 框架 | Magento Open Source 2.4.x | https://github.com/magento/magento2 |
| 许可 | Open Software License (OSL) v3.0（仓库根目录 `LICENSE.txt`）；另有 AFL-3.0 用于部分示例/主题文件（`LICENSE_AFL.txt`） | https://github.com/magento/magento2/blob/2.4-develop/LICENSE.txt |
| 是否适合做公开参考实现 | 适合。本仓库**不包含、不分发** Magento 源码或其衍生作品，只在 CodeBuild 构建时从官方 Git 仓库拉取；本仓库自身代码按 MIT-0 发布。OSL-3.0 的 copyleft 义务落在「分发 Magento 衍生作品」的一方，使用本原型构建并对外分发镜像的人需自行评估（记入 OPEN-QUESTIONS） | 同上 |
| 官方系统要求（2.4.8 最新补丁线） | PHP 8.4 / 8.3；MySQL 8.4 或 MariaDB 11.4；OpenSearch 3；Valkey 8.x；Composer 2.9.3+；RabbitMQ 4.x 可选；Varnish 可选 | https://experienceleague.adobe.com/en/docs/commerce-operations/installation-guide/system-requirements |
| 官方系统要求（2.4.7 最新补丁线） | PHP 8.3 / 8.2；MariaDB 10.11；OpenSearch 2.19 / 3；Valkey 8.x | 同上 |
| arm64 | Adobe 系统要求页未提及 CPU 架构（既未声明支持也未声明不支持） | 同上 |

注：系统要求页的表头写的是 Adobe Commerce on-premises，Magento Open Source 与其共用代码基线，页面没有单列 Open Source 的依赖表。本原型取 2.4.8 + PHP 8.3（两条补丁线的交集），见 DECISIONS D-02。补充材料中的「PHP 8.2」只被 2.4.7 支持，记入 OPEN-QUESTIONS Q-01。

## Step 0-2：已有同类资产

| # | 资产 | 链接 | 覆盖了什么 | 没覆盖什么 |
|---|---|---|---|---|
| A1 | aws-samples/magento-ecs-cdk（MIT） | https://github.com/aws-samples/magento-ecs-cdk | CDK(TypeScript) 部署 Magento 到 ECS（Fargate 或 EC2）+ Aurora MySQL + OpenSearch + ElastiCache + EFS/FSx；需公网 Route 53 域名与证书 | 使用第三方预制 Magento 镜像，在容器启动时安装/初始化（不是「构建时烤进镜像」）；**无 CI/CD 流水线**；无 arm64/Graviton 设定（任务定义未设 `cpuArchitecture`）；无多环境晋级、审批、回滚；无 `setup:upgrade` 单点迁移设计；无 MCF |
| A2 | aws-quickstart/quickstart-magento（已下线，仅存社区 fork） | https://github.com/joaoasrosa/quickstart-magento （fork） | 早期 CloudFormation 版 Magento on EC2 | 已不维护；无容器、无流水线、无 Graviton |
| A3 | aws-samples/ecs-blue-green-deployment（已归档） | https://github.com/aws-samples/ecs-blue-green-deployment | ECS + CodePipeline 蓝绿参考 | 通用容器应用；已归档；不涉及 PHP/Magento 特有步骤；基于 CodeDeploy 蓝绿 |
| A4 | aws-samples/ecs-blue-green-global-deployment-with-multiregion-cmk-codepipeline | https://github.com/aws-samples/ecs-blue-green-global-deployment-with-multiregion-cmk-codepipeline | 多区域多账号 ECS Fargate 蓝绿 + CodePipeline | 通用应用；无数据库迁移串行化、无 Magento 配置分离；不针对 arm64 构建 |
| A5 | aws/aws-graviton-getting-started（官方 Graviton 指南） | https://github.com/aws/aws-graviton-getting-started | PHP 8.2+ 在 Graviton 上表现良好、AL2023/Ubuntu 22.04 开箱可用；托管服务（RDS、Aurora、ElastiCache、OpenSearch、Fargate、Lambda、CodeBuild、ECR）均有 Graviton 支持 | 不提供 Magento 整栈的逐依赖兼容性清单，也不提供 Magento 构建在 arm64 CodeBuild 上的落地脚本 |
| A6 | amzn/selling-partner-api-samples → use-cases/fulfillment-outbound（Amazon 官方，MIT-0） | https://github.com/amzn/selling-partner-api-samples/tree/main/use-cases/fulfillment-outbound | **已有 MCF 对接参考**：CDK(TS) + Step Functions + Lambda(Java/…) 演示 createFulfillmentOrder / getFulfillmentOrder / updateFulfillmentOrder / cancelFulfillmentOrder / getPackageTrackingDetails / getFulfillmentPreview，以及 SP-API 通知订阅 | 是 API 操作演示，不是「独立站订单 → MCF → 运单回传」的业务闭环：没有独立站 SKU↔MSKU 映射表与映射校验、没有 FBA 库存回写独立站、没有基于独立站订单号的幂等与状态机、没有「重试 N 次后标记异常并告警」、没有离线 mock 模式（部署时需要真实 LWA 凭证作为 CFN 参数） |
| A7 | amzn/selling-partner-api-samples → code-recipes/…/multichannel_fulfillment（官方） | https://github.com/amzn/selling-partner-api-samples/tree/main/code-recipes | MCF 创建/取消/跟踪订单的代码片段（Java、Python 等） | 片段级，不含基础设施、状态与告警 |
| A8 | aws-samples/selling-partner-api-bootstrap（已归档） | https://github.com/aws-samples/selling-partner-api-bootstrap | SP-API 限流与鉴权（Java + Redis 令牌桶） | 不涉及 MCF 履约闭环；已归档 |
| A9 | aws-samples/sp-api-throttling-monitor、aws-samples/sp-api-lwa-rotation | https://github.com/aws-samples/sp-api-throttling-monitor ，https://github.com/aws-samples/sp-api-lwa-rotation | 429 限流监控；LWA 凭证轮换 | 不涉及履约业务流程 |
| A10 | Amazon MCF 集成目录 | https://supplychain.amazon.com/integrations | 列出 Shopify 等平台和多家第三方连接器 | 页面未列出 Adobe Commerce / Magento 的 Amazon 自建应用；社区有第三方扩展（例如 GitHub 上 2020 年后未更新的 Magento MCF 扩展），不属于 AWS 资产 |
| A11 | AWS CodeBuild arm64 构建镜像 | https://docs.aws.amazon.com/codebuild/latest/userguide/ec2-compute-images.html | 官方 `aws/codebuild/amazonlinux-aarch64-standard:3.0`（AL2023）等 | 通用能力，不含 Magento 构建步骤 |
| A12 | Amazon ECS 原生蓝绿 / 断路器回滚 | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-type-blue-green.html ，https://docs.aws.amazon.com/AmazonECS/latest/developerguide/deployment-circuit-breaker.html | 蓝绿（含 bake time、生命周期钩子）与滚动更新失败自动回滚 | 通用能力，不处理「先迁移数据库再切流量」的顺序 |

## Step 0-3：差异假设逐条结论

### spec 第 3 节的 3 条假设

| # | 假设 | 结论 | 依据 |
|---|---|---|---|
| H1 | 开源电商栈整体迁到 Graviton 的兼容性结论与调优点 | **成立（窄）** | A5 给出各托管服务与 PHP 的 Graviton 支持，但没有「Magento 2.4.8 的每个依赖 ↔ arm64 支持 ↔ 官方出处」的对照，也没有在 arm64 CodeBuild 里完成 composer/di:compile/static-content:deploy 的落地脚本。A1 没有设定 arm64。差异点很窄：清单 + 构建脚本 + 实测记录，不是新的性能结论。本原型不做性能对比（不写数字） |
| H2 | 多环境流水线分级与回滚 | **成立（仅在与 Magento 特有步骤结合时）** | 通用的多环境 + 审批 + 蓝绿已有 A3/A4 与官方文档，单独做不构成差异。差异在于把 Magento 的 4 个关键点（不可变镜像、env.php 运行时注入、setup:upgrade 单点串行迁移 + 维护模式、部署后 cache:flush）放进「一次构建、逐环境晋级」的流程里，A1/A3/A4 都没有 |
| H3 | 跨区域部署中静态资源与媒体文件的处理 | **不确定 → 本原型不做** | 现有材料没有跨区域、流量或合规要求（spec 第 2 节）。S3 + CloudFront 承载静态资源是通用做法，没有可引用的差异依据。本原型只在单区域内用 EFS 共享 `pub/media`；跨区域记入 OPEN-QUESTIONS |

### 补充材料中的候选差异点

| # | 候选点 | 结论 | 依据 |
|---|---|---|---|
| S-a | 独立站订单经 MCF 履约的闭环（SKU 映射、库存同步、MCF 下单、物流回传） | **成立（部分）** | A6/A7 已覆盖 SP-API 调用本身，本原型**不重复**实现 API 演示，请求/响应字段直接对齐官方模型（`amzn/selling-partner-api-models`）。差异在独立站一侧的业务粘合层：SKU↔MSKU 映射与校验、库存回写事件、以独立站订单号为键的幂等状态机、重试 3 次后标记异常并告警、可离线运行的 SP-API mock、Lambda arm64 |
| S-b | Magento 特有 4 个 CI/CD 关键点 | **成立** | 见 H2；A1 在启动时初始化，与「构建时烤进镜像」相反；没有资产处理迁移串行化 |
| S-c | 四环境一次构建逐环境晋级 + 两道审批 + 回滚 | **成立（与 S-b 合并后）** | 单独看是通用模式（A3/A4、CodePipeline 官方文档）；与 S-b 合并后才有差异 |
| S-d | 全栈 Graviton | **成立（与 H1 合并）** | 见 H1 |

## 结论

存在差异点，继续开发，范围聚焦：

1. Magento 4 个关键点落地为 Dockerfile / buildspec / 部署脚本，接入「一次构建、逐环境晋级、两道审批、回滚」的 CodePipeline（S-b + S-c + H2）。
2. Graviton 逐依赖兼容性清单（FR-4）+ arm64 构建与运行（H1 + S-d）。
3. 独立站 ↔ MCF 履约粘合服务（S-a），SP-API 调用层强制 mock，字段对齐官方模型。

不做：跨区域静态资源（H3）；SP-API 操作本身的演示（已有 A6）；性能/成本对比。

## 检索局限

- 通用网页搜索不可用，AWS 博客与 Prescriptive Guidance 未能全文检索，只检查了 GitHub 组织与已知官方页面。负责人发布前建议再人工检索一次 AWS 博客「Magento」「Adobe Commerce」「Graviton PHP」相关文章（记入 OPEN-QUESTIONS Q-10）。
