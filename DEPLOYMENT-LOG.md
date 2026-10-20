# DEPLOYMENT-LOG

- 账号：员工沙箱账号（`<ACCOUNT_ID>`，不记录真实 ID）
- 区域：us-east-1
- 配置：`config/sandbox.json`（dev 环境 + `develop` 流水线 + MCF 桥接 mock；`removal_policy=destroy`）
- 标签：`Project=ecommerce-graviton-cicd`、`Purpose=prototype`、`Teardown=after-test`
- 时间均为 UTC，2026-10-20

## 1. 部署

| 时间 | 动作 | 结果 |
|---|---|---|
| 11:18:51 | `npx -y aws-cdk@2 deploy --all -c config=config/sandbox.json --require-approval never --concurrency 3` | 三个栈 CREATE_COMPLETE，总耗时约 19 分钟（平台栈最长） |
| 11:4x–12:4x | 修正后多次 `cdk deploy proto-ecom-cicd`（扫描门禁支持增强扫描、构建角色只读源桶、扫描超时参数、产物桶确定性命名） | UPDATE_COMPLETE |
| 12:2x | `cdk deploy proto-ecom-cicd proto-ecom-dev-platform`（部署项目读取流水线产物的授权） | UPDATE_COMPLETE |
| 13:2x | `cdk deploy proto-ecom-dev-platform -c imageTag=<SMOKE_TAG>`（新增应用库用户 Secret） | UPDATE_COMPLETE；服务保持 1/1 运行 |

## 2. 资源清单（销毁前，按 CloudFormation 资源类型计数）

- `proto-ecom-cicd`：CodeBuild 项目×1、CodePipeline×1、ECR 仓库×1、EventBridge 规则×1、IAM 角色×8 / 策略×7、Lambda×2（CDK 自定义资源）、日志组×1、S3 桶×2（源、产物）、SNS 主题×1。
- `proto-ecom-dev-platform`：VPC×1、子网×6、NAT 网关×1 + EIP×1、IGW×1、流日志×1、安全组×7、内部 ALB×1（监听器、规则、目标组各 1）、ECS 集群×1 / 服务×1 / 任务定义×2、EFS×1（访问点×2、挂载目标×2）、RDS for MySQL×1（参数组、子网组）、ElastiCache Valkey 复制组×1（子网组）、OpenSearch 域×1、Secrets×6、SSM 参数×3、CloudWatch 告警×8、EventBridge 规则×1、SNS 主题×1、CodeBuild 部署项目×1、S3 桶×1（ALB 日志）、日志组×4、IAM 角色×10、Lambda×4（CDK 自定义资源）。
- `proto-ecom-dev-mcf`：DynamoDB 表×1、EventBridge 总线×1 / 规则×5、SQS×2（队列 + DLQ）、Lambda×4（3 个 arm64 业务函数 + 1 个 CDK 自定义资源）、事件源映射×1、日志组×4、Secret×1（占位）、SNS 主题×1、告警×5。
- 栈外由部署脚本注册：ECS 任务定义修订 `proto-ecom-dev-web:2…11`、`proto-ecom-dev-ops:2…11`；ECR 镜像（smoke ×5、magento ×5）。

## 3. 验证步骤与结果

| # | 步骤 | 结果 |
|---|---|---|
| V-1 | `scripts/mcf_e2e.py --env dev`（11:2x–11:4x） | 通过，详见 VERIFICATION §4 |
| V-2 | 流水线 `develop`，smoke 镜像，执行 `5a8fc68` / `c86665f` / `e714512` / `fb2fc95` | 依次失败并修复：扫描等待器需要 `inspector2:ListCoverage`（账号级增强扫描）→ 扫描结果长时间 PENDING → 部署项目无权读取流水线产物 → `services-stable` 后 rolloutState 尚未稳定 |
| V-3 | 流水线执行 `dbfb1ebb`（commit `d1c4f71`，smoke） | **成功**：Build（`arch=aarch64`）→ Deploy-dev：迁移任务 exit 0 → `web:3` COMPLETED → cache-flush exit 0 → 冒烟 200/200 |
| V-4 | Magento 镜像构建（流水线外单独构建，变体 magento） | 第 7 次成功（构建期问题见 VERIFICATION §3 #1–#6） |
| V-5 | `scripts/promote_image.sh dev <MAGENTO_TAG>` ×4 | 前 3 次在迁移步骤失败（参数语法、库权限、env.php 先于安装），**服务均保持原修订**；第 4 次（`9049d40`，13:41:53–13:50:32）**成功**：`setup:install` 完成 → `web:8` COMPLETED → cache-flush → 冒烟 `/health_check.php` 200、`/`（Magento 首页）200 |
| V-6 | 再次晋级 Magento（`dc7ef97`，14:01:11–14:07:45；第一次尝试因 MySQL 8.4 definer 检查失败，服务未动，修复后重试） | **成功**：「All modules are up to date」→ 只导入配置、不进维护模式 → `web:10` COMPLETED → cache-flush → 冒烟 200/200 |
| V-7 | 流水线阶段回滚：`aws codepipeline rollback-stage --stage-name Deploy-dev --target-pipeline-execution-id dbfb1ebb…`（14:08:51） | **成功**（executionType=ROLLBACK）：用目标执行的产物重新执行 Deploy-dev，服务从 Magento `dc7ef97` 回到 smoke `d1c4f71`（`web:11`），冒烟 200/200 |

## 4. 销毁

| 时间 | 命令 | 结果 |
|---|---|---|
| 14:15:37 → 14:40:27 | `npx -y aws-cdk@2 destroy --all -c config=config/sandbox.json --force` | `proto-ecom-cicd`、`proto-ecom-dev-platform`、`proto-ecom-dev-mcf` 全部 destroyed，无 DELETE_FAILED |
| 14:3x | 手工清理栈外资源：ECS 任务定义修订 `proto-ecom-dev-web:2–5,7–11`、`proto-ecom-dev-ops:2–5,7–11` 先 `deregister-task-definition` 再 `delete-task-definitions`（共 22 个，0 失败） | 状态 DELETE_IN_PROGRESS（ECS 的删除终态） |
| 14:4x | 手工删除栈外日志组 10 个：`/aws/rds/instance/proto-ecom-dev-mysql/{error,slowquery}`（RDS 日志导出）、`/aws/ecs/containerinsights/proto-ecom-dev-cluster/performance`、7 个 `/aws/lambda/proto-ecom-*`（CDK 自定义资源处理函数） | 全部删除 |

## 5. 残留核对（`scripts/check_residuals.sh proto-ecom us-east-1`，14:56:45，按名称前缀与 `Project` 标签）

| 类别 | 结果 |
|---|---|
| CloudFormation 栈 | 无 |
| S3 桶（含对象与版本） | 无（源桶、产物桶、ALB 日志桶由 CDK 清空后随栈删除；桶不存在即无残留版本） |
| 日志组 | 无（§4 手工删除后） |
| KMS 密钥 | 未创建客户管理密钥（全部使用 AWS 托管加密）；按标签查询无结果，无计划删除状态的密钥 |
| ECR 仓库与镜像 | 无（`empty_on_delete` 清空后删除） |
| IAM 角色 / 本地策略 | 无 |
| Lambda | 无 |
| API Gateway | 无（本项目未创建） |
| RDS 实例 / 手工快照 / 自动快照 / 参数组 / 子网组 | 无（沙箱 dev 未开启迁移前快照；删除时不留最终快照、删除自动备份） |
| OpenSearch 域 | 无 |
| ElastiCache 复制组 / 快照 / 子网组 | 无 |
| EFS | 无 |
| ECS 集群 / 服务 / 任务定义 | 集群与服务为 INACTIVE（ECS 保留已删除对象的只读记录）；任务定义全部 DELETE_IN_PROGRESS |
| VPC / NAT / EIP / ALB / 目标组 | 无（NAT 状态 deleted） |
| Secrets Manager（含计划删除） | 无 |
| SSM 参数、SQS、SNS、EventBridge 总线、DynamoDB、CodeBuild、CodePipeline、告警 | 无 |
| AgentCore 相关资源 | 不适用（本项目未使用） |
| 标签索引 | `resourcegroupstaggingapi` 仍列出若干已删除资源的 ARN（任务定义 DELETE_IN_PROGRESS、NAT=deleted、流日志不存在、集群/服务 INACTIVE），均已逐个确认处于删除终态，属标签索引延迟 |

不归本项目独占、因此**未删除**的：CDK bootstrap 资产桶中由本次部署上传的 7 个按内容哈希命名的对象（模板与 Lambda 代码包；CDK 内置处理函数的代码包可能与其他项目共用同一键，且负责人要求不得修改 CDKToolkit）。

## 6. 结论

仍有残留：CDK bootstrap 资产桶中由本次部署上传的 7 个资产对象（模板与 Lambda 代码包）。原因：该桶属于 CDKToolkit，负责人要求不得修改；对象按内容哈希命名，CDK 内置处理函数的代码包可能与账号内其他项目共用。除此之外，本项目创建的全部资源已删除（§5 逐类核对）。
