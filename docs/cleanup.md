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
| 非栈创建的日志组 | RDS 日志导出（`/aws/rds/instance/proto-ecom-<env>-mysql/*`，由 CDK LogRetention 设置保留期后默认保留）、CDK 自定义资源 Lambda（`/aws/lambda/proto-ecom-*-Custom*` 等） | `aws logs describe-log-groups` 按名字核对后逐个删除 |
| Secrets Manager | 删除后默认进入计划删除窗口 | 沙箱可用 `aws secretsmanager delete-secret --force-delete-without-recovery --secret-id <name>`（只针对 `proto-ecom-` 前缀） |
| RDS 迁移前快照 | 只有开启 `snapshot_before_migrate` 的环境会创建（沙箱 dev 未开启） | `aws rds delete-db-snapshot` |
| VPC 内 Lambda/CodeBuild 网卡 | 删除 VPC 时偶尔需要等待 ENI 释放 | 重试 destroy |
| CDK bootstrap 资产桶中的模板与代码包 | 属于 CDKToolkit，按内容哈希命名，可能与其他项目共用 | 不随本项目删除；如需清理由账号管理员统一处理 |
| 标签索引中的已删除资源 | `resourcegroupstaggingapi` 有延迟，ECS 集群/服务删除后显示 INACTIVE | 逐个确认状态为 deleted/INACTIVE/DELETE_IN_PROGRESS 即可 |

## 交付环境（`removal_policy=retain`）

数据库、缓存、搜索、EFS、桶、ECR、Secrets 在删除栈后**保留**，且 RDS 开启删除保护。确认数据已备份且不再需要后，先关闭删除保护，再逐个手工删除。
