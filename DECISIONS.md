# DECISIONS

每条：选择 / 备选 / 理由（取更保守、更安全的一侧）。

| # | 决策 | 备选 | 理由 |
|---|---|---|---|
| D-01 | 继续开发，范围限定在 prior-art 中成立的差异点（Magento 4 关键点 + 晋级流水线、Graviton 清单、MCF 粘合层） | 停止；或做完整电商功能 | `docs/prior-art.md`：已有资产未覆盖这些组合；完整电商功能（购物车、支付等）是 Magento 自带能力，不重复 |
| D-02 | Magento Open Source 2.4.8-p5 + PHP 8.3 | 2.4.9（PHP 8.5）；2.4.7（PHP 8.2） | 2.4.8 最新补丁线同时支持 PHP 8.4/8.3，属于成熟补丁线；2.4.9 刚发布，依赖（PHP 8.5、Valkey 9）更新；2.4.7 已不支持 MySQL。需求输入中的 PHP 8.2 记入 Q-01 |
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
| D-14 | 部署阶段配置 CodePipeline 阶段回滚（OnFailure=ROLLBACK） | 仅手工回滚 | 回到该阶段最后一次成功执行的产物（上一镜像 tag），同样先经过部署前扫描复查与迁移检查。数据库迁移不可逆：沙箱中回滚到旧 Magento 镜像被扫描复查拦下，旧代码遇到新 schema 时迁移检查的行为未实测（VERIFICATION §2.2）；schema 不兼容时应从迁移前快照恢复 |
| D-15 | uat/prod 在迁移前自动打 RDS 快照；dev/test 不打 | 全部打 | 沙箱验证后要清理干净，快照会成为残留；dev/test 数据可重建 |
| D-16 | `var/` 与 `pub/media` 放 EFS（访问点 + IAM 授权 + 传输加密） | 只放 `pub/media` | 维护标志 `var/.maintenance.flag` 必须对所有 Web 任务可见，迁移时的维护模式才生效 |
| D-17 | ECS 服务在该环境没有镜像 tag 时 desiredCount=0；tag 按环境传入（`-c imageTags=dev=<TAG>,...`；`-c imageTag` 只在只合成一个环境时接受）；部署脚本成功后把当前 tag 写入 SSM `<SSM_PREFIX>/<ENV>/current_image_tag` | 用公共占位镜像先跑起来；一个 `imageTag` 作用于所有环境 | 避免首次部署拉取不存在的镜像而卡住 CFN；避免把一个环境的 tag 误用到其他环境。重跑 `cdk deploy` 前从 SSM 读出各环境当前 tag |
| D-18 | MCF：EventBridge 自定义总线 → SQS（DLQ，maxReceiveCount=重试次数）→ Lambda arm64；DynamoDB 单表存订单状态、SKU 映射与 mock 状态 | Step Functions 编排 | 需求是「失败重试 3 次后标记异常并告警」，SQS 重投 + DLQ 正好表达；状态机简单，不需要 Step Functions |
| D-19 | SP-API 默认 mock；live 模式要求显式配置且 Secret 不能是占位值，否则拒绝启动 | 默认 live | 不接真实卖家账户；凭证只以类型占位放在 Secrets Manager |
| D-20 | `sellerFulfillmentOrderId` 由独立站订单号确定性生成（≤40 字符），作为 MCF 侧幂等键；4xx 时先 `getFulfillmentOrder` 确认是否已存在 | 随机 ID | 防止重投导致重复发货 |
| D-21 | 库存同步只发布 `Inventory Updated` 事件，不直接写 Magento 数据库 | Lambda 直接改 Magento 库存表 | 不绕过 Magento 的库存/预留逻辑（防超卖由 Magento 下单时的预留负责）；独立站侧消费者属于店铺实现，见 Q-05 |
| D-22 | 构建产物扫描门禁：沙箱阻断 CRITICAL，客户配置阻断 CRITICAL+HIGH | 都阻断 HIGH | 基础镜像常含 HIGH 级别 OS CVE，沙箱若一律阻断会无法验证流程；交付配置按需求输入「高危阻断」 |
| D-23 | 删除策略做成参数：sandbox=destroy（含自动清空桶/ECR），customer=retain + 删除保护 | 统一 destroy | 通用要求第 5 节 |
| D-24 | 账号 ID 不进模板：CDK 以 env-agnostic 账号合成（区域固定） | 读取 CDK_DEFAULT_ACCOUNT | cdk-nag 输出、cdk.out 与确认文件中不出现账号 ID |
| D-25 | cdk-nag v3（`Validations.of().acknowledge`），逐条确认并写理由 | 降级 v2 | 使用当前版本；v3 不支持前缀批量抑制，确认项更细 |
| D-26 | 实例型号只出现在 `config/sandbox.json`（沙箱可部署值，不是规格建议）；交付配置 `config/customer.json` 中数据库/缓存/搜索实例类留空，加载时强制要求填写（报错指向官方实例类型页）；文档只写「Graviton 实例族」 | 交付配置给默认型号；文档给推荐规格 | 项目约定：不写实例规格；型号由部署方按官方页面与容量评估选择。沙箱值避开早期草稿里出现过的型号，只用于短时验证 |
| D-27 | 本地无 Docker：完整镜像只在 CodeBuild arm64 构建；沙箱流水线默认构建 smoke 镜像，Magento 镜像另行触发一次构建验证 | 本地构建 | 本机 Docker daemon 未启动；smoke 镜像用于验证流水线/迁移顺序/回滚，Magento 构建结果单独记录 |
| D-28 | 扫描门禁兼容账号级「增强扫描（Amazon Inspector）」：轮询 `DescribeImageScanFindings` 至 COMPLETE/ACTIVE；超时视为「扫描结果不可用」。交付配置 `scan_unavailable_action=fail`（拒绝晋级），沙箱配置 `warn`（记录告警后继续） | 一律 fail | 沙箱账号的注册表扫描类型由账号所有者设为增强扫描，覆盖仓库级 scan-on-push，首次扫描可能长时间 PENDING；不修改账号级配置。只要拿到结果，有阻断级别的发现一律失败 |
| D-29 | 构建角色可只读源桶，以便运维人员在流水线外按需构建某个变体（例如 magento） | 不授予，只能经流水线构建 | 便于验证 Magento 变体而不改流水线；权限只读且限于源桶 |
| D-30 | Magento 使用单独的应用库用户（Secrets Manager 生成，迁移任务用主用户创建并授予本 schema 全部权限）；Web 任务拿不到主用户凭证 | 直接用 RDS 主用户 | 实测 RDS for MySQL 8.4 主用户的权限经角色授予，Magento 的安装前权限检查不通过；同时更符合最小权限 |
| D-31 | 提供 `scripts/promote_image.sh`：用环境的部署项目把已存在的镜像 tag 晋级到单个环境（同一脚本、同一顺序） | 只允许经流水线 | 沙箱验证 Magento 变体需要；生产中作为应急手段，需要权限控制与审计 |
| D-32 | uat/prod（`base_url_scheme=https`）必须提供 `certificate_arn` 与 `site_hostname`，否则加载配置即失败；内部 ALB 建 HTTPS:443 监听（ELB 推荐的后量子 TLS 1.3/1.2 策略），80 只做重定向；冒烟测试按 scheme 用证书主机名访问并校验证书（`curl --connect-to` 指向内部 ALB） | 未提供证书时自动降级为 http | 降级会让交付配置在不知情的情况下以明文运行，并与 base_url 不一致；保守做法是显式失败。沙箱 dev 用 http |
| D-33 | TLS 在 ALB 终止，ALB → 任务为 VPC 内 HTTP:8080 | 端到端 TLS（任务内证书） | 原型范围内的取舍：流量只在 VPC 内、安全组限定来源；端到端 TLS 列入加固清单（SECURITY-NOTES「传输加密例外」） |
| D-34 | MCF 桥接用事务性 outbox：状态变化与待发事件在同一次条件写入（乐观锁 `version`）里落库，发布成功后才从 outbox 移除；轮询器与重投补发；事件带稳定 `eventId`，投递语义为至少一次 | 先发事件再推进状态；EventBridge Pipes / DynamoDB Streams | 「先发后推进」在发布成功、写库失败时同样会丢状态；outbox 只依赖现有的表与轮询函数，不增加资源。消费者按 `eventId` 去重 |
| D-35 | EXCEPTION 订单的恢复通道：消息带 `detail.retry=true` 时把订单重置为 RECEIVED 并清零尝试次数（`scripts/mcf_redrive.py` 从 DLQ 重驱时自动加；店铺也可带该标志重新发布）；不带标志的重放仍按重复忽略 | 任意重放都重提 | 防止误重放导致重复下单；恢复必须是显式动作 |
| D-36 | 每次投递都失败（例如 Lambda 超时）留在 RECEIVED 的订单，由轮询器在超过「可见性超时 ×（最大次数 + 1）」后标记 EXCEPTION 并提示从 DLQ 重驱；不消费 DLQ | DLQ 上挂消费者补标 EXCEPTION | 消费 DLQ 会删除消息，失去重驱能力；按时间扫描不需要额外资源 |
| D-37 | intake 默认 batch_size=1；函数超时 = 批大小 ×（4 × 单次 HTTP 超时 + 余量）；队列可见性超时 = 6 × 函数超时（Lambda 官方建议） | 固定 30 秒超时、批大小 5 | 最坏情况下（LWA + create + 两次确认）一条消息就可能超过 30 秒；批量失败会连带同批其他订单重投 |
| D-38 | 放弃前先用 `getFulfillmentOrder` 确认 MCF 是否已建单；429/5xx 不再视为「不存在」；确认不了时标 EXCEPTION 且 `mcf_check=true`，轮询器继续确认，确认存在则回到 SUBMITTED 继续跟踪 | 把查询失败当不存在 | 避免「MCF 已发货但店铺侧显示异常、运单不回传」 |
| D-39 | 市场配置带 SP-API 区域（na/eu/fe），每个区域一个客户端与 refresh token（Secret 键 `lwa_refresh_token_<REGION>`） | 全局一个区域 | 美/欧/日站点分属不同端点（F-19）；一个卖家授权按区域签发 refresh token 的细节以官方为准（Q-03） |
| D-40 | 取消：店铺发 `Order Cancelled` → 发货前调用 `cancelFulfillmentOrder` 并置 CANCELLED；已发货/已完成则拒绝并发 `Fulfillment Exception`（按退货处理）；MCF 侧 Cancelled 映射为 CANCELLED（不再当异常）；支付前取消先记 CANCELLED，迟到的 `Order Paid` 被忽略 | 只支持 MCF 侧取消 | 对齐店铺订单状态流转里的「已取消」与手动取消 |
| D-41 | 迁移串行化在两层：部署脚本拒绝并发 ops 任务 + 迁移脚本持有 MySQL `GET_LOCK`（会话级，进程或连接断开即释放） | DynamoDB/SSM 锁 | 数据库锁与被保护的资源同生命周期，不会留下陈旧锁；不增加资源 |
| D-42 | 有 schema 变更时维护模式在迁移后保持开启，直到新任务上线、cache-flush 完成后才关闭；部署在迁移开始后失败（含超时被停止）时，按环境参数 `maintenance_on_failure` 处理：dev/test=`disable`（恢复访问，可能是旧代码 + 新 schema），uat/prod=`keep`（保持维护，由人恢复迁移前快照或完成发布） | 迁移结束就关闭维护模式 | 迁移结束即关闭会让旧代码在滚动/蓝绿切换期间服务新 schema；生产侧宁可停机也不让旧代码写入已升级的库 |
| D-43 | 任务定义开启 `initProcessEnabled`，ops 任务 `stopTimeout` 取 Fargate 上限；迁移脚本对 INT/TERM/EXIT 做清理，长命令放后台 `wait` 以便立刻响应信号 | 依赖 `sh -c` 作为 PID 1 | PID 1 默认忽略 SIGTERM，陷阱不会执行（F-25、F-26） |
| D-44 | 扫描门禁：构建阶段阻断的镜像被隔离（删除可晋级的 tag，摘要保留为 `quarantine-<TAG>`）；每次部署前用同一门禁参数重新检查 tag 与扫描结果 | 只在构建阶段检查 | 先推后扫意味着被阻断的镜像曾短暂存在于 ECR；部署前复查也覆盖 `promote_image.sh` 这类流水线外入口 |
| D-45 | `promote_image.sh` 对流水线中有审批的环境（uat/prod）默认拒绝，`--break-glass "<理由>"` 才放行，理由写入部署构建的环境变量与日志 | 不限制 | 应急通道可审计，不绕过审批成为常态 |
| D-46 | uat/prod 迁移前快照打标签并保留最近 N 个（`predeploy_snapshots_keep`），部署脚本删除更早的 | 永久保留 | 快照会无限累积；清理指南与残留核对脚本按前缀覆盖剩余快照 |
| D-47 | cdk-nag 确认项挂在具体构造路径上（路径不存在即报错，按环境条件启用），不再整栈确认 | 整栈 `Validations.of(stack)` | 整栈确认会把 `Resource::*` 等发现对栈内所有资源放行，理由也容易与实际策略不符 |
| D-48 | `app/etc/config.php`（模块、scopes、themes、i18n）提交到 Git，构建直接使用并校验与代码中的模块一致；模块增删后用 Dockerfile 的 `config-export` 目标（或 `scripts/export_magento_config.sh`）重新生成 | 构建时 `module:enable --all` | 与「config.php 进 Git」一致，模块启用状态可审查、可追溯 |
| D-49 | 仓库带一个演示模块 `Proto_DeployDemo`（只声明一张表），用来在沙箱里真实走一遍「setup:upgrade + 维护模式」分支 | 不带演示模块，只靠单元测试 | 该分支此前从未在 AWS 上触发；真实项目删除该目录与 config.php 中的一行即可 |
| D-50 | LICENSE 为 MIT-0，版权行 `Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.`；CODE_OF_CONDUCT 指向亚马逊开源行为准则；SECURITY 走 AWS 漏洞上报页面/aws-security@amazon.com | 保留原版权行与 Contributor Covenant | 规范对齐（aws-samples 惯例）。git 作者仍为 `shaojiez <shaojiez2026@gmail.com>` |
| D-51 | 占位符：账号 ID 统一 `111122223333`，其余 `<UPPER_SNAKE>`，主机名 `example.com`；CODEOWNERS 用 `@<MAINTAINER_GITHUB_HANDLE>`（Q-19） | 保留 `<ACCOUNT_ID>`、小写占位符 | 规范对齐 |
| D-52 | 配置以模板入库（`config/*.example.json`），真实文件 gitignore，`make init` 在缺失时复制；加载时做格式与联动校验，synth 前失败 | 继续提交真实配置 | 规范对齐；避免把证书 ARN（含账号 ID）等真实值提交进仓库 |
| D-53 | `make preflight` 只读：账号只显示末 4 位并要求输入确认；`cdk diff` 发现删除/替换默认拒绝（`ALLOW_DESTRUCTIVE=1`），uat/prod 的数据存储（RDS、OpenSearch、ElastiCache、EFS、DynamoDB、S3、Secrets）再要求 `ALLOW_DATA_REPLACEMENT=1` | 只靠 CloudFormation 删除策略 | 删除保护只覆盖部分资源；替换（例如改实例标识符）同样会丢数据。没有会额外计费的「真实模型评测」类命令，因此不设 `ALLOW_PAID` |
| D-54 | 信任策略条件只加在官方文档给出条件键用法的服务上（CodeBuild、ECS 任务角色、VPC 流日志、EventBridge 规则目标）；其余列在 SECURITY-NOTES 并附官方链接 | 所有服务角色一律加 | 对文档未说明的服务加条件可能导致服务无法扮演角色；不猜测。ARN 用确定的名字拼接，避免循环依赖 |
| D-55 | Secret 资源策略：拒绝除消费方执行角色（和需要解析动态引用的 CDK CloudFormation 执行角色）以外的所有读取 | 不加资源策略 | 规范「敏感资源只允许预期调用方，其余显式拒绝」；管理员也读不到凭证，填写 SP-API 凭证仍可用 `PutSecretValue` |
| D-56 | CI（lint、测试、两套配置 synth+cdk-nag、gitleaks 全历史 + 运行时生成的假凭证 canary）与部署验证 workflow（只能手动触发、OIDC、需审批的 environment、角色 ARN 占位）入库，但**未在 GitHub 上运行**；本地等效执行并记入 VERIFICATION | 不提供 workflow | 规范对齐；仓库不推送远端。Actions 按 `git ls-remote` 查到的 commit SHA 固定 |
| D-57 | CDK bootstrap 桶清理：先按 `cdk.out/*.assets.json` 与在旧提交上重新 synth 得到的资产哈希识别；更早部署是从未提交的工作区合成的、哈希无法复现，对这些对象再做内容识别（模板必须**同时**含本项目 `Project` 标签值与栈名前缀，zip 必须含本项目 Lambda 包），两条都不满足的不删 | 只按哈希；或按上传时间删除 | 规范第九节允许「按资产哈希或内容识别」；只按哈希会留下确属本项目的旧模板。CDK 通用 provider 包与 CDKToolkit 栈不动 |
| D-58 | 历史重写只做一次：在规范对齐轮改动开始前，用 git-filter-repo 改写提交主题为 Conventional Commits、替换旧前缀与实例型号、统一作者；之后不再重写 | 每轮各重写一次 | 保证验证记录中的提交哈希长期有效 |
| D-59 | 最终验证复用本轮已部署的沙箱环境：在记下的提交上 `make deploy`（更新）→ 流水线 → Magento 升级分支 → `make verify` → `make destroy` → `make verify-cleanup` | 先销毁再全新部署一次 | 维护者要求避免重复部署 Magento 环境；更新部署同样经过 preflight 的破坏性变更保护，也验证了「已有环境升级到新提交」这一常见路径。代价：没有验证「从零创建」在最终提交上的耗时与顺序（此前提交已全新创建过一次，见 DEPLOYMENT-LOG） |
| D-60 | 本机没有 Docker：ASH 及依赖容器的扫描器不运行，写「未运行」交 CI；依赖漏洞扫描用 `uvx pip-audit` 读 `uv.lock` 导出的依赖（osv-scanner 未安装） | 安装 Docker 与全套扫描器 | 维护者约束：不依赖本地 Docker，不为凑齐扫描器安装大量本地工具 |
| D-61 | AGENTS.md 是给编码助手的仓库说明（角色、命令、git 流程、Always/Ask first/Never），CLAUDE.md 只写 `@AGENTS.md` | — | 规范对齐；本项目没有把 AGENTS.md 用作产品知识库的需求 |
