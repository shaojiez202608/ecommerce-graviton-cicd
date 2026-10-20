# VERIFICATION

只记录实际运行过的内容；没运行的明确写「未运行」。每条记录：日期、区域、配置、提交、结果、失败项、**这个结果不能证明什么**。

- 日期：2026-10-20（UTC，AWS 部分 16:20–19:xx）/ 2026-10-21（本地检查）
- 区域：us-east-1（只用了这一个区域）
- 配置：`config/sandbox.json`（与 `config/sandbox.example.json` 相同：dev 环境 + `develop` 流水线 + MCF mock，US/na 与 DE/eu 两个市场，`removal_policy=destroy`）
- **最终验证提交：`4754d6d`**（`4754d6dbc6c0a18c4231835dfcd7134fa77de442`）。它与 `5f53935` 只差销毁/残留核对脚本中枚举任务定义族的修复；之后的提交只改文档（本文件、DEPLOYMENT-LOG、review 表等），tag `v0.1.0` 打在最后一个文档提交上。
- 账号：员工沙箱账号（`111122223333` 为占位，不记录真实 ID）

## 1. 本地检查（最终提交 `4754d6d`，不需要 AWS）

| 命令 | 结果 | 不能证明什么 |
|---|---|---|
| `make test`（`uv run pytest`） | **130 passed, 0 failed** | 单元测试与 CDK assertions 只检查模板与逻辑，不证明 AWS 上的行为 |
| `make lint`（ruff + `bash -n`/`sh -n` 全部脚本） | All checks passed | — |
| `make synth`（`config/sandbox.example.json`；`config/customer.example.json -c synthCheck=true`） | 两套均成功，cdk-nag AwsSolutions 无未确认发现 | customer 用占位实例类与证书合成，不可部署；https/uat/prod/蓝绿未部署过 |
| `make scan`（gitleaks 8.30.1 全历史 + 运行时生成的假 AWS key canary） | 全历史 0 leaks；canary 被 `aws-access-token` 规则检出（证明扫描器能报错） | canary 只证明 AWS key 规则生效 |
| `uvx zizmor --persona auditor .github/workflows` | 无发现 | workflow **未在 GitHub 上运行**，仅本地静态检查 |
| `uvx pip-audit -r <uv export>`（107 个依赖） | No known vulnerabilities found | osv-scanner 未安装，未运行 |
| ASH 3.7.1 `--mode local`（`c4ca7db` 与文档提交 `91385ca` 各跑一次，源为 `git archive HEAD` 干净副本，输出 `_scan_out/ecommerce_cicd/`，无 zip） | bandit PASSED（280 LOW：测试断言、`REPLACE_ME` 占位串、`subprocess` 调 AWS CLI）；checkov PASSED（2 条带理由的行内跳过）；detect-secrets PASSED；semgrep PASSED；npm-audit PASSED；**cdk-nag、cfn-nag、grype、opengrep、syft：MISSING（未运行：本机无 Docker / 未安装）** | 按规范，扫描器没跑完即视为**未通过**；缺的扫描器交给 CI 补跑。首轮 ASH 有 6 条可处理发现，已在 `922cc6f` 修复（uv 依赖冷却期、Docker 健康检查/基础镜像说明、workflow 输入、误报标注） |
| Holmes | **未运行**：本机没有 Holmes | — |
| `uv run python -m local.run_mcf_demo` | 跑通：outbox（101 在 EventBridge 不可用时仍推进到 SUBMITTED，事件下一轮补发）、102 两次 503 后成功、103 三次后 EXCEPTION、104 未映射 SKU、105 走 EU 客户端、106 确认丢失后以「已存在」恢复、107 带 `retry=true` 重驱成功、108 发货前取消 | mock 行为由合成收件人名驱动，不代表真实 SP-API 的返回 |

测试覆盖（`tests/`，130 个）：MCF 业务（outbox、乐观锁、重驱、确认丢失、429 不当作「不存在」、放弃前确认与轮询器确认、卡在 RECEIVED 的清扫、多区域路由、取消四种情形、首轮即签收补发 Shipped、mock fnSku 确定性）；moto 下的 Lambda 处理函数（SQS 批处理、取消事件、每区域 refresh token、DLQ 重驱）；live 客户端（LWA、请求头含 `x-amz-date`、取消路径、超时）；CDK assertions（https→443 HTTPS 监听 + 证书 + 推荐 TLS 策略、http 只有 80、前缀参数化后快照名与 IAM 前缀一致、扫描门禁参数下发到部署项目、`ecr:BatchDeleteImage`、快照清理权限只在开启快照的环境、imageTag 按环境、init 进程与 stopTimeout、信任策略 SourceAccount/SourceArn、Secret 资源策略、蓝绿两组 UnhealthyHost 告警、intake 超时/批大小/可见性超时、outbox 索引与取消路由、cdk-nag 按构造确认）；配置校验（30+ 拒绝用例）；部署脚本守护（迁移锁、信号陷阱、维护窗口在 cache-flush 后关闭、失败与超时路径、部署前复查扫描、快照清理）；扫描门禁（隔离、只校验模式不改 tag、分页、ACTIVE 时等待结果稳定）；运维脚本（破坏性变更保护的解析与双重开关、bootstrap 资产只选本项目、脱敏）。

## 2. AWS 部署验证

### 2.1 一次环境，两段：开发迭代 + 最终提交

为避免重复创建 Magento 环境（DECISIONS D-59），同一套沙箱栈先用于开发迭代（找出并修复问题），再更新到最终提交做验证，最后销毁。

| 时间（UTC） | 提交 | 步骤 | 结果 | 失败项 / 发现 | 不能证明什么 |
|---|---|---|---|---|---|
| 16:20–16:38 | 历史改写前的 `e6c1834` 对应代码 | `cdk deploy --all`（3 栈 CREATE） | 成功，约 18 分钟 | — | — |
| 16:41–16:49 | 工作区 | `scripts/export_magento_config.sh`（arm64 CodeBuild 上运行 config-export 目标） | 成功，生成 221 个模块的 `config.php`（提交于 `3111335`） | — | — |
| 16:36–17:05 | 同上 | `scripts/mcf_e2e.py --outbox-drill`（mock） | 演练 PASS；8 张订单全部落定（见 §2.3） | 无 | 见 §2.3 |
| 16:45–16:58 | `600c6ee` | 流水线 develop（smoke 镜像） | 成功：Build（`arch=aarch64`）→ Deploy-dev：部署前扫描复查（ENHANCED 无结果 → `warn` 继续）→ 迁移 → 滚动 COMPLETED → cache-flush → 冒烟 200/200 | — | smoke 镜像不是 Magento |
| 16:59–17:02 | `3111335` | Magento 镜像构建 | **失败**：`check-modules` 以静态方式调用实例方法 | 修复于 `5f02870` | — |
| 17:11–17:20 | `5f02870` | Magento 镜像构建 | 成功：`config.php lists all 222 modules`，di:compile 与静态资源在 arm64 上完成 | — | — |
| 17:21–17:31 | `5f02870`（脚本同工作区） | `promote_image.sh dev`：首次安装 | 成功：`migration lock acquired`（MySQL `GET_LOCK`）→ `setup:install`（含演示模块 v1 表）→ `web:3` COMPLETED → cache-flush → 冒烟 `/health_check.php` 200、`/`（Magento 首页）200 | — | 首页 200 只说明能渲染，不是功能测试 |
| 16:5x | 控制项代码已提交、尚未部署 | `verify_controls.py` | 3 项 FAIL（Secret 可读、信任策略无条件），3 项 PASS（部署角色范围） | 预期的失败：证明检查脚本能报出缺失的控制 | — |
| 17:35–17:46 | `c71dbf7` → `bb1382d` → `015490a` | `make preflight` | 首次 FAIL（配额检查把已存在的 VPC 算作新增；EIP 实际用量高于 Service Quotas 返回值）→ 修复；第二次 `make deploy` 时 preflight 因另一个 CDK 进程占用 `cdk.out` 失败 → 修复为独立输出目录；之后 PASS（无删除/替换） | 2 个 preflight 缺陷，已修复 | — |
| 17:44–17:48 | `015490a` | `make deploy`（更新：信任策略条件、Secret 资源策略、Lambda 代码） | 成功 | — | — |
| 17:48 | `015490a` | `verify_controls.py` | 6/6 PASS：运维身份读 SP-API Secret 与 DB 主用户 Secret 均 `AccessDeniedException`；部署角色可对本环境 ops 族 RunTask、不能对其他族 RunTask、不能 PassRole 无关角色（IAM 策略模拟）；部署角色信任策略含 SourceAccount + SourceArn | — | IAM 策略模拟不等于真实调用；没有尝试用其他账号扮演角色 |
| 17:48–17:50 | `015490a` | 流水线 develop | **Build 失败（预期的控制生效）**：增强扫描首次给出结果，门禁发现 CRITICAL，镜像被隔离（tag 删除，摘要保留为 `quarantine-<TAG>`） | 发现基础镜像 OS 包（`linux-libc-dev` 等）有可修复的 CRITICAL；门禁只读了第一页结果 | — |
| 17:5x | `f92be13` | 修复：门禁读取全部分页；两个 Dockerfile 加 `apt-get upgrade` | 流水线（smoke）成功：门禁通过（无 CRITICAL）→ Deploy-dev 成功，冒烟 200/200 | — | — |
| 18:06 | `f92be13` | 晋级 Magento 镜像 | **部署前复查拒绝**：构建时门禁看到的是尚未填满的结果，复查时出现 2 个 CRITICAL（`mtdowling/jmespath.php`、vendor 内附带的 lock 文件） | 构建时门禁对 ACTIVE 状态过早放行 | — |
| 18:0x–18:16 | `d1f0364` / `c4ca7db` | 修复：更新 jmespath.php、删除 vendor 内 lock 文件；门禁在 ACTIVE 时要求两次轮询结果一致 | Magento 镜像（`d1f0364`）通过构建门禁与之后的复查；流水线（`c4ca7db`，smoke）成功并显示「waiting for findings to settle」 | — | 新发现可能在门禁之后才出现，复查只在部署时生效 |
| 18:2x | `5f53935` | 修复：门禁错误信息中的账号 ID 脱敏 | — | — | — |
| 18:4x | `4754d6d` | 修复：销毁与残留核对脚本按族名枚举任务定义（`--family-prefix` 需要完整族名） | 残留核对对运行中的环境列出 ACTIVE 修订并退出 1（证明能报出残留） | 之前的版本会漏掉任务定义修订 | — |

### 2.2 最终提交上的验证（`5f53935` → `4754d6d`，两者的 CDK 模板、镜像内容与部署脚本相同）

| 时间（UTC） | 提交 | 步骤 | 结果 | 不能证明什么 |
|---|---|---|---|---|
| 18:24–18:32 | 部署脚本来自 `5f53935` 工作区；镜像 `d1f0364`（`app/` 与最终提交相同） | **schema 升级 + 维护模式分支**（`promote_image.sh dev d1f0364`）：数据库已有演示表 v1，镜像带 v2 列 | 成功：扫描复查通过 → `migration lock acquired` → `Declarative Schema is not up to date` → `upgrade required -> maintenance window`、`Enabled maintenance mode`（18:27:02）→ `setup:upgrade` 完成，维护模式**保持开启** → 服务滚动到 `web:6` COMPLETED（18:31:08）→ cache-flush 后 `Disabled maintenance mode`（18:31:42）→ 冒烟 `/health_check.php` 200、`/` 200 | 维护期间对外返回 503 没有从客户端角度实测（ALB 仅内网）；迁移失败、超时被停止两条失败路径只有守护测试 |
| 18:34 | `5f53935` | **回滚到真实旧 Magento 镜像**（`promote_image.sh dev 5f02870`，schema v1 的旧镜像） | **被部署前扫描复查拒绝**（该旧镜像有 56 个 CRITICAL），没有任何变更、未进入迁移 | 旧镜像的迁移检查遇到新 schema 的行为**未实测** |
| 18:34–18:36 | `5f53935` | `make deploy` | preflight PASS，3 个栈 no changes | 没有在最终提交上「从零创建」（从零创建在开发迭代开始时做过一次） |
| 18:36–18:38 | `5f53935` | 服务级回滚（`scripts/rollback.sh dev web:3`，不经过迁移与扫描门禁）：旧代码（无 v2 列）运行在 v2 schema 上 | 服务 COMPLETED，目标组 healthy | 只证明健康检查通过；`rollback.sh` 绕过扫描门禁（SECURITY-NOTES 已知风险） |
| 18:40–18:54 | `5f53935` | 流水线 develop（smoke） | 成功（门禁等待结果稳定后通过；部署前复查通过；冒烟 200/200） | — |
| 18:5x | `4754d6d` | `make deploy` | preflight PASS，3 个栈 no changes | 同上 |
| 18:56–19:05 | `4754d6d` | 流水线 develop（smoke） | 成功，冒烟 200/200 | — |
| 19:05–19:12 | `4754d6d` | 流水线阶段回滚：`rollback-stage --stage-name Deploy-dev`，目标为 `5f53935` 的成功执行 | 成功（executionType=ROLLBACK）：扫描复查通过 → 迁移任务 → 服务回到 `5f53935-smoke` → cache-flush → 冒烟 200/200 | smoke 镜像不碰 schema，回滚不涉及迁移兼容性 |
| 19:13–19:52 | `4754d6d` | `make verify` | **PASS**：流水线最近一次执行 Succeeded；拒绝路径 6/6 PASS；`promote_image.sh prod` 在交付配置下被拒绝；MCF e2e + outbox 演练 PASS（§2.3） | IAM 策略模拟不等于真实调用；`prod` 拒绝只在本地脚本层面验证（没有 prod 环境） |
| 19:5x–20:08 | `4754d6d` | `make destroy`、bootstrap 资产清理、`make verify-cleanup` | 3 个栈 destroyed；栈外遗留（任务定义修订、10 个日志组）由脚本清除；bootstrap 桶中本项目对象共删除 20 个版本（按哈希 11、按内容 9）；残留核对退出 0：**已清理，无残留**（DEPLOYMENT-LOG §4–§6） | 标签索引对已删除的 ECS/流日志 ARN 仍有滞后，已逐个确认处于终态 |

### 2.3 MCF 桥接端到端（mock SP-API）

开发迭代（16:36–17:05）与最终提交（`make verify`，19:13 起）结果相同：
- outbox 演练：给三个桥接函数角色附加拒绝 `events:PutEvents` 的内联策略后发送订单 → 状态推进到 SUBMITTED，`Fulfillment Order Submitted` 留在 outbox；移除拒绝后轮询器 `republished=1`、outbox 清空 → **PASS**。
- 8 张订单：101/102/105/106 COMPLETE（消费者依次收到 Submitted → Shipped → Delivered）；102 两次 503 后第三次成功；105 由 EU 客户端处理（mock 记录 `region=eu`）；106 首次创建响应丢失、第二次以「已存在」确认（attempts=2）；103/107 三次失败后 EXCEPTION 并进入 DLQ，`mcf_redrive.py` 带 `retry=true` 重驱：107 COMPLETE，103 再次 EXCEPTION（FAIL_ALWAYS）；104 未映射 SKU → EXCEPTION；108 SUBMITTED 后店铺发 `Order Cancelled` → CANCELLED（消费者收到 Submitted → Cancelled）。所有订单 outbox 为空。最终一轮的 DLQ 中还有开发迭代留下的一条 103 消息，被同一次重驱一并送回，按预期再次 EXCEPTION（测试脚本按 DLQ 深度触发重驱，没有按运行批次过滤）。
- 不能证明什么：mock 不代表真实 SP-API 的错误码与时序；没有测试真实的 SP-API 限流；首轮轮询即签收（补发 Shipped）只由单元测试覆盖；卡在 RECEIVED 的清扫与 `mcf_check` 确认只由单元测试覆盖。

## 3. 本轮发现并修复的问题（实测）

| # | 现象 | 修复提交 |
|---|---|---|
| 1 | Magento 构建在 `check-modules` 失败：`ComponentRegistrar::getPaths()` 不能静态调用 | `5f02870` |
| 2 | preflight 配额检查把已存在环境的 VPC 算作新增；Service Quotas 返回的 EIP 配额低于实际用量 | `bb1382d` |
| 3 | preflight 的 `cdk diff` 被并发 CDK 进程锁住；当前 CLI 不接受 `diff --all` | `015490a` |
| 4 | 扫描门禁只读第一页（增强扫描的 `findingSeverityCounts` 也只覆盖当页） | `f92be13` |
| 5 | 基础镜像 OS 包有可修复的 CRITICAL（门禁正确阻断并隔离了镜像） | `f92be13`（`apt-get upgrade`） |
| 6 | Magento 依赖 `mtdowling/jmespath.php` CRITICAL；vendor 内附带的 lock 文件被扫描器当作已安装依赖 | `d1f0364` |
| 7 | 增强扫描状态 ACTIVE 时结果尚未填满，构建门禁过早放行（部署前复查拦住了） | `c4ca7db` |
| 8 | 门禁错误信息引用 AWS CLI 输出时带账号 ID | `5f53935` |

## 4. 未运行 / 未验证

- **旧 Magento 镜像回滚时的迁移检查**：被部署前扫描复查拦下，未进入迁移（§2.2）；只验证了不经迁移的服务级回滚。
- 迁移失败、迁移超时被停止、滚动失败后按 `maintenance_on_failure` 处理维护模式：只有守护测试，未在 AWS 上触发。
- test/uat/prod、`release` 流水线、两道审批、ECS 原生蓝绿、https 监听（证书、私有 DNS）：只由 CDK assertions 验证，**未部署**。
- live SP-API 客户端未对真实卖家账户运行（假传输层单元测试）。
- 构建期 `module:enable --all` 生成的 `config.php` 是在沙箱 CodeBuild 上导出的；本机没有 Docker，`config-export` 目标没有在本地运行。
- GitHub Actions（CI、部署验证 workflow）**未在 GitHub 上运行**，仅本地等效执行（lint、test、synth+nag、gitleaks+canary、zizmor）。
- ASH 中 cdk-nag、cfn-nag、grype、opengrep、syft 未运行（本机无 Docker / 未安装）；Holmes、osv-scanner 未运行。
- Magento 店铺侧事件模块、cron、消息队列消费者未实现/未运行；性能、容量与成本未测。
