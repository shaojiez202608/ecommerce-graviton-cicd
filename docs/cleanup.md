# 清理指南

## 沙箱（`removal_policy=destroy`）

```bash
npx -y aws-cdk@2 destroy --all -c config=config/sandbox.json --force
scripts/check_residuals.sh proto-ecom us-east-1     # 只读核对，按名称前缀与标签
```

`destroy` 会删除：三类栈及其资源；S3 桶（CDK 先清空对象与版本）；ECR 仓库（含镜像）；RDS 实例（不留最终快照、删除自动备份）；ElastiCache（不留最终快照）；OpenSearch 域；EFS；日志组（栈内创建的）；Secrets（进入计划删除，见下）。

需要额外核对/处理的：

| 资源 | 原因 | 处理 |
|---|---|---|
| 流水线注册的 ECS 任务定义修订 | 由部署脚本注册，不归 CloudFormation 管 | `aws ecs list-task-definitions --family-prefix proto-ecom-` 后逐个 `deregister-task-definition` 再 `delete-task-definitions` |
| CodeBuild/Lambda 自动创建的日志组 | 若日志组不是栈创建的（如 CDK 自定义资源） | `aws logs describe-log-groups` 按前缀/名字核对后删除 |
| Secrets Manager | 删除后默认进入计划删除窗口 | 沙箱可用 `aws secretsmanager delete-secret --force-delete-without-recovery --secret-id <name>`（只针对 `proto-ecom-` 前缀） |
| RDS 迁移前快照 | 只有开启 `snapshot_before_migrate` 的环境会创建（沙箱 dev 未开启） | `aws rds delete-db-snapshot` |
| VPC 内 Lambda/CodeBuild 网卡 | 删除 VPC 时偶尔需要等待 ENI 释放 | 重试 destroy |

## 交付环境（`removal_policy=retain`）

数据库、缓存、搜索、EFS、桶、ECR、Secrets 在删除栈后**保留**，且 RDS 开启删除保护。确认数据已备份且不再需要后，先关闭删除保护，再逐个手工删除。
