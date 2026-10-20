# DEPLOYMENT-LOG

本次部署验证记录（替换此前的记录）。

- 账号：员工沙箱账号（`111122223333` 为占位，不记录真实 ID；preflight 只显示并核对末 4 位，未记录）
- 区域：us-east-1（只用了这一个区域）
- 配置：`config/sandbox.json`（与 `config/sandbox.example.json` 相同；dev + `develop` 流水线 + MCF mock）
- 标签：`Project=ecommerce-graviton-cicd`、`Purpose=prototype`、`Teardown=after-test`
- 栈：`proto-ecom-cicd`、`proto-ecom-dev-platform`、`proto-ecom-dev-mcf`
- 最终验证提交：`4754d6d`（之前的验证提交与发现的问题见 `VERIFICATION.md` §2、§3）
- 时间均为 UTC，2026-10-20

## 1. 部署

| 时间 | 提交 | 命令 | 结果 |
|---|---|---|---|
| 16:20:00–16:38:24 | 历史改写前、与 `e6c1834` 相同的 CDK 代码 | `npx -y aws-cdk@2 deploy --all -c config=config/sandbox.json --require-approval never --concurrency 3` | 3 个栈 CREATE_COMPLETE（约 18 分钟，平台栈最长） |
| 17:44–17:48 | `015490a` | `CONFIRM=<LAST_4_DIGITS> make deploy`（preflight PASS，无删除/替换） | 3 个栈 UPDATE_COMPLETE：服务角色信任策略条件、7 个 Secret 资源策略、Lambda 代码 |
| 18:34–18:36 | `5f53935` | `make deploy` | preflight PASS；3 个栈 no changes |
| 18:55 | `4754d6d` | `make deploy` | preflight PASS；3 个栈 no changes |

另：破坏性变更保护用真实 `cdk diff` 验证过一次（临时把 dev 的 `vpc_cidr` 改掉的本地配置，只做 diff，不部署）：VPC、子网、安全组、目标组等被识别为 replace，`check_destructive.py` 退出码 3 拒绝；`ALLOW_DESTRUCTIVE=1` 后放行（diff 中没有数据存储）。

## 2. 资源清单（销毁前，按 CloudFormation 资源类型计数）

- `proto-ecom-cicd`：CodeBuild 项目×1、CodePipeline×1、ECR 仓库×1、EventBridge 规则×1、IAM 角色×8 / 策略×7、Lambda×2（CDK 自定义资源）、日志组×1、S3 桶×2（源、产物）、SNS 主题×1。
- `proto-ecom-dev-platform`：VPC×1、子网×6、NAT 网关×1 + EIP×1、IGW×1、流日志×1、安全组×7、内部 ALB×1（监听器、规则、目标组各 1）、ECS 集群×1 / 服务×1 / 任务定义×2、EFS×1（访问点×2、挂载目标×2）、RDS for MySQL×1（参数组、子网组）、ElastiCache Valkey×1（子网组）、OpenSearch 域×1、Secrets×6（资源策略×6）、SSM 参数×4、CloudWatch 告警×8、EventBridge 规则×1、SNS 主题×1、CodeBuild 部署项目×1、S3 桶×1（ALB 日志）、日志组×4、IAM 角色×10 / 策略×10、Lambda×4（CDK 自定义资源）。
- `proto-ecom-dev-mcf`：DynamoDB 表×1（2 个 GSI）、EventBridge 总线×1 / 规则×5、SQS×2（队列 + DLQ）、Lambda×4（3 个 arm64 业务函数 + 1 个 CDK 自定义资源）、事件源映射×1、日志组×4、Secret×1（占位，资源策略×1）、SNS 主题×1、告警×7。
- 栈外：部署脚本注册的任务定义修订 `proto-ecom-dev-web:2…`、`proto-ecom-dev-ops:2…`；ECR 镜像（smoke、magento、被隔离的 `quarantine-<TAG>`）；服务创建的日志组。

## 3. 验证步骤与结果

详见 `VERIFICATION.md` §2。摘要：

| # | 步骤 | 结果 |
|---|---|---|
| V-1 | 流水线 develop（smoke）在 `600c6ee`、`f92be13`、`c4ca7db`、`5f53935`、`4754d6d` | 成功；`015490a` 的执行在 Build 失败——扫描门禁阻断 CRITICAL 并隔离镜像（控制生效） |
| V-2 | Magento 镜像构建（`5f02870`、`f92be13`、`d1f0364`） | 成功（`3111335` 失败，缺陷已修复） |
| V-3 | 首次安装（`5f02870` 镜像） | 成功：迁移锁 → `setup:install` → COMPLETED → cache-flush → 冒烟 200/200 |
| V-4 | schema 升级 + 维护模式（`d1f0364` 镜像，部署脚本来自最终代码） | 成功：维护模式在 `setup:upgrade` 后保持开启，新任务 COMPLETED 之后由 cache-flush 关闭，冒烟 200/200 |
| V-5 | 晋级被拒：`f92be13` Magento 镜像、旧 `5f02870` 镜像 | 部署前扫描复查拒绝，服务未变 |
| V-6 | 服务级回滚 `rollback.sh dev web:3`（旧代码 + 新 schema） | COMPLETED，目标组 healthy |
| V-7 | 流水线阶段回滚（`rollback-stage`，目标为 `5f53935` 的成功执行） | 成功（executionType=ROLLBACK）：复查通过 → 迁移 → 服务回到 `5f53935-smoke` → 冒烟 200/200 |
| V-8 | `make verify`（`4754d6d`，19:13–19:5x） | **PASS**：流水线最近执行 Succeeded；拒绝路径 6/6；受保护晋级被拒；MCF e2e 与 outbox 演练 PASS |
| V-9 | 拒绝路径（`verify_controls.py`） | 控制部署前 3 项 FAIL（预期）；部署后 6/6 PASS |

## 4. 销毁（提交 `4754d6d`）

| 时间 | 命令 | 结果 |
|---|---|---|
| 19:5x–20:02:56 | `CONFIRM_DESTROY=proto-ecom CONFIRM_DATA=yes ASSET_HASH_LISTS=<旧提交重新 synth 得到的键列表> make destroy`（即 `scripts/destroy.sh`） | 1. `cdk destroy --all`：`proto-ecom-cicd`、`proto-ecom-dev-platform`、`proto-ecom-dev-mcf` 全部 destroyed，无 DELETE_FAILED；2. 部署脚本注册的任务定义修订全部注销并删除（状态 DELETE_IN_PROGRESS，ECS 的删除终态）；3. 栈外日志组 10 个已删除（Container Insights 1、CDK 处理函数 7、RDS 日志导出 2）；4. 计划删除中的 Secret：无；5. 迁移前快照：无（沙箱 dev 未开启）；6. bootstrap 桶：按资产哈希删除本项目对象 5 个；7. 残留核对：已清理，无残留 |
| 20:05 | `scripts/cleanup_bootstrap_assets.py --assembly cdk.out --assembly <本轮两次部署与旧部署的 assets.json> --extra-prefix <旧前缀> --hash-list <旧提交键列表> --content-scan --delete`（`CONFIRM_ASSETS=yes`） | 再删除 15 个对象版本：6 个按资产哈希识别（本轮第一次部署的模板与 Lambda 包、旧部署的模板），9 个按内容识别（旧部署从未提交的工作区合成、哈希无法复现的模板；每个都同时含本项目 `Project` 标签值与栈名前缀）。复查：候选键在桶中剩 0 个 |

旧提交重新 synth：在历史改写前的 18 个提交上（每个提交 × 不带 imageTag、带各提交 SHA、带各提交 SHA + `-smoke`）重新合成，得到 184 个候选键；与本轮的 `cdk.out/*.assets.json` 合计 210 个候选键。

## 5. 残留核对（`make verify-cleanup`，20:07:56，按名称前缀与 `Project` 标签；另按旧前缀核对一次）

| 类别 | 结果 |
|---|---|
| CloudFormation 栈 | 无 |
| S3 桶（含对象与版本） | 无（源桶、产物桶、ALB 日志桶由 CDK 清空后随栈删除） |
| 日志组 | 无 |
| KMS 密钥 | 未创建客户管理密钥；按标签查询无结果（含计划删除状态） |
| ECR 仓库与镜像（含 `quarantine-<TAG>`） | 无（`empty_on_delete` 清空后删除） |
| IAM 角色 / 本地策略 | 无（e2e 演练临时加的拒绝内联策略在演练结束时已移除，角色随栈删除） |
| Lambda、API Gateway | 无（本项目未创建 API Gateway） |
| RDS 实例 / 手工快照（含迁移前快照） / 自动快照 / 参数组 / 子网组 | 无 |
| OpenSearch 域、ElastiCache 复制组/快照/子网组、EFS | 无 |
| ECS 集群 / 服务 / 任务定义修订 | 集群与服务为 INACTIVE，任务定义修订为 DELETE_IN_PROGRESS（ECS 的终态，标签索引仍列出这些 ARN，已逐个确认） |
| VPC / NAT / EIP / ALB / 目标组 / 流日志 | 无（NAT 状态 deleted；流日志 ARN 仅在标签索引中滞后） |
| Secrets Manager（含计划删除）、SSM 参数、SQS、SNS、EventBridge 总线、DynamoDB、CodeBuild、CodePipeline、告警 | 无 |
| AgentCore 相关资源 | 不适用（本项目未使用） |
| 其他区域 | 未使用 |
| CDK bootstrap 资产桶 | 本项目可识别的对象已全部删除（§4）；CDK 通用 provider 包（自定义资源处理函数代码）与 CDKToolkit 栈按规定保留、未修改 |

未删除且不属于本项目独占的：账号级服务关联角色（ECS、ElastiCache、OpenSearch 等由服务自动创建，可能早于本项目存在且被其他工作负载使用）；CDK bootstrap 桶中的 CDK 通用 provider 包。

## 6. 结论

已清理，无残留。
