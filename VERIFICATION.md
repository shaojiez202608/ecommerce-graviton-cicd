# VERIFICATION

日期 2026-10-20，区域 us-east-1。只记录实际运行过的内容；没运行的明确写「未运行」。

## 1. 本地测试（不需要 AWS）

| 命令 | 结果 |
|---|---|
| `uv run pytest` | **55 passed, 0 failed** |
| `uv run ruff check .` | All checks passed |
| `uv run python -m local.run_mcf_demo` | 跑通：库存同步 `{'skus': 5, 'invalid': 1, 'out_of_stock': 1}`；订单 101 COMPLETE、102 三次尝试后 SUBMITTED→SHIPPED→COMPLETE、103 三次 503 后 EXCEPTION、104 未映射 SKU 直接 EXCEPTION |
| `npx -y aws-cdk@2 synth -c config=config/sandbox.json` | 成功，cdk-nag（AwsSolutions，v3）无未确认的发现 |
| `npx -y aws-cdk@2 synth -c config=config/customer.json` | 成功（4 环境 + 2 条流水线），cdk-nag 无未确认的发现 |

测试覆盖（`tests/`）：
- `test_mcf_service.py`（17）：请求字段与官方模型一致、幂等、瞬时错误重试后成功、达到最大次数后 EXCEPTION + 告警、4xx 不重试、确认丢失时通过 `getFulfillmentOrder` 恢复、未映射/INVALID SKU、非法事件、发货→签收、MCF 侧取消、库存同步 INVALID/缺货、按 50 分批与分页。
- `test_spapi_live_client.py`（7）：LWA 令牌交换与缓存、`x-amz-access-token` 头、路径与查询参数、HTTP 错误 → 可重试判断（假传输层，无网络）。
- `test_mcf_aws_handlers.py`（3）：moto 模拟 DynamoDB/EventBridge/Secrets Manager 下的 SQS 批处理部分失败、状态落库、mock 状态跨调用持久化、live 模式拒绝占位 Secret。
- `test_infra.py`（16）：CDK assertions——栈名前缀、cdk-nag 通过、无 0.0.0.0/0 入站、ALB internal、ECS/Lambda/CodeBuild 全部 arm64 且镜像 ID 正确、RDS/Valkey/OpenSearch 为 Graviton 实例族且加密、dev/test 滚动+断路器回滚与 uat/prod 原生蓝绿、desiredCount 规则、release 流水线阶段顺序与两道审批、Deploy 阶段 `OnFailure=ROLLBACK`、所有部署阶段只消费同一构建产物、ECR scan-on-push + 不可变 tag、S3 私有 + 仅 TLS、删除策略随配置、标签、MCF 队列/DLQ/告警、凭证经 secrets 注入。
- `test_config_and_keypoints.py`（12）：配置校验（/0、未知环境、流水线引用缺失环境、非法模式）、Magento 4 个关键点的守护测试、扫描门禁对 basic/enhanced 两种结果的计数。

## 2. AWS 部署验证（沙箱，见 DEPLOYMENT-LOG.md）

| 项 | 结果 |
|---|---|
| 一条命令部署 3 个栈 | 成功（`proto-ecom-cicd`、`proto-ecom-dev-platform`、`proto-ecom-dev-mcf`） |
| 流水线 `develop`（smoke 镜像） | 执行 `dbfb1ebb…` **成功**：Source → Build（CodeBuild `ARM_CONTAINER`，日志 `arch=aarch64`）→ Deploy-dev（迁移任务 exit 0 → 服务滚动到新修订 COMPLETED → cache-flush 任务 exit 0 → 冒烟 `/health_check.php` 200、`/` 200，响应 `"arch":"aarch64"`、PHP 8.3） |
| Magento 2.4.8-p5 镜像在 arm64 CodeBuild 上构建 | **成功**（第 7 次尝试；前 6 次的失败原因与修复见 §3 #1–#6；之后为修正迁移脚本又成功构建 4 次）。composer install、`setup:di:compile`、`setup:static-content:deploy`（frontend/Luma、adminhtml）全部在构建阶段完成，未连接数据库 |
| Magento 镜像晋级到 dev（`scripts/promote_image.sh`，与流水线同一部署脚本） | **成功**：迁移任务创建最小权限应用库用户 → `setup:install`（RDS for MySQL 8.4 TLS、OpenSearch 3.1 细粒度访问控制、Valkey 8.1 TLS+AUTH）→ `app:config:import` → Web 服务切到新修订 COMPLETED → `cache:flush` → 冒烟 `/health_check.php` 200、`/`（Magento 首页 HTML）200 |
| 迁移失败时不动服务（关键点 3 的 fail-safe） | 实际触发 4 次（参数语法、库权限、安装前 env.php、MySQL 8.4 definer 检查），每次部署脚本都在迁移步骤停下，服务保持原修订继续对外 |
| 重新 `cdk deploy` 平台栈并带 `-c imageTag` | 服务保持运行（desired=1/running=1），未被改回 0 |
| MCF 桥接端到端（mock） | 见 §4 |
| 扫描门禁 | 账号级注册表扫描类型为 ENHANCED（Amazon Inspector），镜像长时间 `PENDING_INITIAL_SCAN`；沙箱配置 `scan_unavailable_action=warn` 下记录 WARNING 后继续。**未拿到任何镜像的漏洞结果**，阻断分支只由单元测试覆盖 |
| 部署回滚（CodePipeline 阶段回滚） | **成功**：`rollback-stage` 以上一次成功执行为目标，服务从 Magento 镜像回到上一个 smoke 镜像，冒烟 200（DEPLOYMENT-LOG V-7） |

## 3. Magento 构建/安装中发现并修复的问题（实测）

| # | 现象 | 修复 |
|---|---|---|
| 1 | 构建角色无法读取源桶对象（流水线外单独构建时） | 构建角色只读源桶（D-29） |
| 2–3 | `setup:static-content:deploy` 试图连接数据库 | 构建时 `env.php` 存根分两种：编译阶段带占位 db + 安装日期；静态资源阶段两者都不带，Magento 才会从 `config.php` 读取 scopes/themes（`InitialConfigSource` 的判断逻辑） |
| 4 | `deploy:mode:set` 需要定义 db 连接 | 同上（编译阶段的存根保留占位 db） |
| 5–6 | 「The default website isn't defined」 | `config.php` 增加 locale（i18n）配置，静态资源阶段存根去掉安装日期 |
| 7 | `--db-ssl-verify` 不接受值；`--cleanup-database` 是开关 | 修正 `setup:install` 参数 |
| 8 | 「Database user does not have enough privileges」：RDS for MySQL 8.4 主用户权限经角色授予，Magento 的权限检查不识别 | 新增最小权限应用库用户（Secrets Manager 生成），迁移任务用主用户创建并授予 `magento.*` 权限；Web 任务只拿应用用户 |
| 9 | `setup:install` 前已渲染的 `env.php` 带安装日期，导致找不到默认网站 | 全新安装分支先删除 `env.php`，安装后重新渲染 |
| 10 | 第二次部署时 `CREATE USER IF NOT EXISTS` 被 MySQL 8.4 拒绝（用户已是触发器 definer） | 先查询 `mysql.user`，存在则 `ALTER USER` |

## 4. MCF 桥接端到端（已部署，mock SP-API）

`uv run python scripts/mcf_e2e.py --env dev`：
- 库存同步：5 个映射，1 个 INVALID（MSKU 不存在），1 个缺货。
- 4 个 `Order Paid` 事件 → 101 COMPLETE（1 次尝试，1 个包裹）；102 前两次 503、第三次成功 → 定时轮询 SHIPPED → COMPLETE；103 三次 503 → EXCEPTION，消息进入 DLQ（1 条）；104 未映射 SKU → EXCEPTION。
- 告警：`DlqNotEmpty`、`OrderExceptions`、`InvalidSkuMappings` 进入 ALARM；`IntakeErrors`、`TrackingErrors` 保持 OK。
- 发给独立站的事件（日志组替身）：`Inventory Updated`、`Fulfillment Order Submitted`×2、`Fulfillment Exception`×2、`Fulfillment Shipped`、`Fulfillment Delivered`×2；规则调用失败数 0。

## 5. 扫描

| 工具 | 结果 |
|---|---|
| detect-secrets 1.5.0（`uvx detect-secrets scan`，每次提交前对暂存文件） | 0 个发现 |
| 账号 ID 检查（每次提交前对 diff 做字符串匹配，不打印） | 0 处 |
| ASH v3.1.2（`--mode local`，源为 `git archive HEAD` 的干净副本，输出 `_scan_out/ecommerce_cicd/`，无 zip） | bandit PASSED（121 个 LOW：113 个 B101 测试断言，B105/B106 为占位字符串 `REPLACE_ME` 与测试用假值，B404/B603/B607 为 `scan_gate.py` 调用 AWS CLI）；cdk-nag PASSED；detect-secrets PASSED；npm-audit PASSED；checkov SKIPPED；cfn-nag、grype、opengrep、semgrep、syft 在 local 模式下 MISSING（本机未安装，**未运行**） |
| Holmes | **未运行**：本机没有 Holmes |
| cdk-nag AwsSolutions | 通过；全部确认项及理由见 `infra/ecom_infra/nag.py` 与 `SECURITY-NOTES.md` |

## 6. 未运行 / 未验证

- live SP-API 客户端未对任何真实卖家账户运行（只做了假传输层单元测试）；SigV4 是否必要未确认（Q-13）。
- test/uat/prod 环境、`release` 流水线、两道审批与 ECS 原生蓝绿只通过 CDK assertions 验证，**未部署**。
- ECS 断路器自动回滚未触发过（部署脚本在迁移失败时就停下，从未推送坏镜像到服务）。
- Magento 店铺侧事件模块、cron、消息队列消费者未实现/未运行。
- 性能、容量与成本未测。
- 本地 Docker 未使用（daemon 未启动）；镜像只在 CodeBuild 构建。
