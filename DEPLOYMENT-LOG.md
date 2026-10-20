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
| 13:2x | `cdk deploy proto-ecom-dev-platform -c imageTag=<smoke tag>`（新增应用库用户 Secret） | UPDATE_COMPLETE；服务保持 1/1 运行 |

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
| V-5 | `scripts/promote_image.sh dev <magento tag>` ×4 | 前 3 次在迁移步骤失败（参数语法、库权限、env.php 先于安装），**服务均保持原修订**；第 4 次（`9049d40`，13:41:53–13:50:32）**成功**：`setup:install` 完成 → `web:8` COMPLETED → cache-flush → 冒烟 `/health_check.php` 200、`/`（Magento 首页）200 |
| V-6 | 再次晋级 Magento（`dc7ef97`，14:01:11–14:07:45；第一次尝试因 MySQL 8.4 definer 检查失败，服务未动，修复后重试） | **成功**：「All modules are up to date」→ 只导入配置、不进维护模式 → `web:10` COMPLETED → cache-flush → 冒烟 200/200 |
| V-7 | 流水线阶段回滚：`aws codepipeline rollback-stage --stage-name Deploy-dev --target-pipeline-execution-id dbfb1ebb…`（14:08:51） | **成功**（executionType=ROLLBACK）：用目标执行的产物重新执行 Deploy-dev，服务从 Magento `dc7ef97` 回到 smoke `d1c4f71`（`web:11`），冒烟 200/200 |

## 4. 销毁

见下方 §5、§6（销毁完成后填写）。
