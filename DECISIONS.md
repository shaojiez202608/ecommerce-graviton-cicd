# DECISIONS

每条：选择 / 备选 / 理由（取更保守、更安全的一侧）。

| # | 决策 | 备选 | 理由 |
|---|---|---|---|
| D-01 | 继续开发，范围限定在 prior-art 中成立的差异点（Magento 4 关键点 + 晋级流水线、Graviton 清单、MCF 粘合层） | 停止；或做完整电商功能 | `docs/prior-art.md`：已有资产未覆盖这些组合；完整电商功能（购物车、支付等）是 Magento 自带能力，不重复 |
| D-02 | Magento Open Source 2.4.8-p5 + PHP 8.3 | 2.4.9（PHP 8.5）；2.4.7（PHP 8.2） | 2.4.8 最新补丁线同时支持 PHP 8.4/8.3，属于成熟补丁线；2.4.9 刚发布，依赖（PHP 8.5、Valkey 9）更新；2.4.7 已不支持 MySQL。补充材料写的 PHP 8.2 记入 Q-01 |
| D-03 | 不分发 Magento 源码：构建时从官方 Git 仓库按 tag 拉取 | 把 Magento 代码放进本仓库 | 本仓库保持 MIT-0；OSL-3.0 义务由构建/分发镜像的一方自行评估 |
| D-04 | 从官方 Git 仓库根目录 `composer install`，不用 `repo.magento.com` 元包 | 用 Marketplace 访问密钥走 composer 元包 | 原型不需要、也不应持有任何账户密钥；Git 方式无需认证。客户项目通常用元包 + 自有模块，见 handover |
| D-05 | 容器运行在 ECS on Fargate，`cpuArchitecture=ARM64` | ECS on EC2 Graviton 实例 | 不需要管理实例与 AMI；Graviton 由 Fargate ARM64 提供。EC2 方案作为演进项 |
| D-06 | 数据库用 RDS for MySQL 8.4（Graviton 实例族），不用 Aurora | Aurora MySQL | Magento 2.4.8 要求 MySQL 8.4；本次未能核实 Aurora MySQL 的 8.4 兼容版本，取已核实的 RDS for MySQL 8.4 |
| D-07 | 缓存/会话用 ElastiCache for Valkey 8.1，开启传输加密与 AUTH | Redis OSS | Magento 2.4.8 系统要求列 Valkey 8.x；Redis 在 2.4.8 表中已不列 |
| D-08 | OpenSearch 3.1（VPC 域 + 细粒度访问控制） | 2.19 | 2.4.8 要求 OpenSearch 3；3.1 是 Amazon OpenSearch Service 支持的 3.x 中最早的一个，版本做成参数 |
| D-09 | ALB 只建 internal，SG 入站仅 VPC CIDR + 配置的内网 CIDR，禁止 /0（配置加载时校验） | 公网 ALB + WAF | 测试环境默认不暴露公网；需要公网时由客户加 CloudFront/WAF/认证，写入威胁模型 |
| D-10 | 流水线源用 S3（上传 `git archive` + REVISION），EventBridge 触发 | CodeConnections 连接 GitHub/GitLab | 原型不推送远端、不创建外部连接；S3 方式无需第三方授权。分支触发的 CodeConnections 写在 handover |
| D-11 | 两条流水线：develop→dev；release→test→审批①→uat→审批②→prod。每条流水线内一次构建，所有环境用同一 SHA 镜像 | 一条流水线串四环境 | 对齐「dev 由 develop 自动、test 由 release 自动」的分支规则，同时保持「一次构建逐环境晋级」 |
| D-12 | 部署动作用 VPC 内的 CodeBuild 执行脚本，而不是 CodePipeline 的 ECS 部署动作 | ECS deploy action / CodeDeploy | 需要严格顺序：注册新修订 → 单个迁移任务 → 更新服务 → cache:flush → 冒烟。ECS 部署动作不能先跑一次性任务 |
| D-13 | dev/test 滚动更新 + 断路器自动回滚；uat/prod 用 ECS 原生蓝绿（bake time） | CodeDeploy 蓝绿 | 原生蓝绿不需要 CodeDeploy 应用与部署组；CDK L2 已支持（`DeploymentStrategy.BLUE_GREEN`） |
| D-14 | 部署阶段配置 CodePipeline 阶段回滚（OnFailure=ROLLBACK） | 仅手工回滚 | 回到该阶段最后一次成功执行的产物（上一镜像 tag）；数据库迁移不可逆，跨 schema 变更的回滚会在迁移步骤失败并停下（fail-safe），由人恢复快照 |
| D-15 | uat/prod 在迁移前自动打 RDS 快照；dev/test 不打 | 全部打 | 沙箱验证后要清理干净，快照会成为残留；dev/test 数据可重建 |
| D-16 | `var/` 与 `pub/media` 放 EFS（访问点 + IAM 授权 + 传输加密） | 只放 `pub/media` | 维护标志 `var/.maintenance.flag` 必须对所有 Web 任务可见，迁移时的维护模式才生效 |
| D-17 | ECS 服务在没有 `-c imageTag` 时 desiredCount=0 | 用公共占位镜像先跑起来 | 避免首次部署拉取不存在的镜像而卡住 CFN；镜像由流水线晋级。重跑 `cdk deploy` 时需带当前 tag，避免把服务改回 0（handover 有说明） |
| D-18 | MCF：EventBridge 自定义总线 → SQS（DLQ，maxReceiveCount=重试次数）→ Lambda arm64；DynamoDB 单表存订单状态、SKU 映射与 mock 状态 | Step Functions 编排 | 需求是「失败重试 3 次后标记异常并告警」，SQS 重投 + DLQ 正好表达；状态机简单，不需要 Step Functions |
| D-19 | SP-API 默认 mock；live 模式要求显式配置且 Secret 不能是占位值，否则拒绝启动 | 默认 live | 不接真实卖家账户；凭证只以类型占位放在 Secrets Manager |
| D-20 | `sellerFulfillmentOrderId` 由独立站订单号确定性生成（≤40 字符），作为 MCF 侧幂等键；4xx 时先 `getFulfillmentOrder` 确认是否已存在 | 随机 ID | 防止重投导致重复发货 |
| D-21 | 库存同步只发布 `Inventory Updated` 事件，不直接写 Magento 数据库 | Lambda 直接改 Magento 库存表 | 不绕过 Magento 的库存/预留逻辑（防超卖由 Magento 下单时的预留负责）；独立站侧消费者属于店铺实现，见 Q-05 |
| D-22 | 构建产物扫描门禁：沙箱阻断 CRITICAL，客户配置阻断 CRITICAL+HIGH | 都阻断 HIGH | 基础镜像常含 HIGH 级别 OS CVE，沙箱若一律阻断会无法验证流程；客户默认值按补充材料「高危阻断」 |
| D-23 | 删除策略做成参数：sandbox=destroy（含自动清空桶/ECR），customer=retain + 删除保护 | 统一 destroy | 通用要求第 5 节 |
| D-24 | 账号 ID 不进模板：CDK 以 env-agnostic 账号合成（区域固定） | 读取 CDK_DEFAULT_ACCOUNT | cdk-nag 输出、cdk.out 与确认文件中不出现账号 ID |
| D-25 | cdk-nag v3（`Validations.of().acknowledge`），逐条确认并写理由 | 降级 v2 | 使用当前版本；v3 不支持前缀批量抑制，确认项更细 |
| D-26 | 实例规格只出现在 `config/*.json` 作为可替换的占位值，文档不写规格与数字 | 文档给推荐规格 | 负责人规则：不写实例规格数字；实例族以官方页为准 |
| D-27 | 本地无 Docker：完整镜像只在 CodeBuild arm64 构建；沙箱流水线默认构建 smoke 镜像，Magento 镜像另行触发一次构建验证 | 本地构建 | 本机 Docker daemon 未启动；smoke 镜像用于验证流水线/迁移顺序/回滚，Magento 构建结果单独记录 |
| D-28 | 扫描门禁兼容账号级「增强扫描（Amazon Inspector）」：轮询 `DescribeImageScanFindings` 至 COMPLETE/ACTIVE；超时视为「扫描结果不可用」。交付配置 `scan_unavailable_action=fail`（拒绝晋级），沙箱配置 `warn`（记录告警后继续） | 一律 fail | 沙箱账号的注册表扫描类型由账号所有者设为增强扫描，覆盖仓库级 scan-on-push，首次扫描可能长时间 PENDING；不修改账号级配置。只要拿到结果，有阻断级别的发现一律失败 |
| D-29 | 构建角色可只读源桶，以便运维人员在流水线外按需构建某个变体（例如 magento） | 不授予，只能经流水线构建 | 便于验证 Magento 变体而不改流水线；权限只读且限于源桶 |
| D-30 | Magento 使用单独的应用库用户（Secrets Manager 生成，迁移任务用主用户创建并授予本 schema 全部权限）；Web 任务拿不到主用户凭证 | 直接用 RDS 主用户 | 实测 RDS for MySQL 8.4 主用户的权限经角色授予，Magento 的安装前权限检查不通过；同时更符合最小权限 |
| D-31 | 提供 `scripts/promote_image.sh`：用环境的部署项目把已存在的镜像 tag 晋级到单个环境（同一脚本、同一顺序） | 只允许经流水线 | 沙箱验证 Magento 变体需要；生产中作为应急手段，需要权限控制与审计 |
