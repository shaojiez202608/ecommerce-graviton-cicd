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
| 流水线注册的 ECS 任务定义修订 | 由部署脚本注册，不归 CloudFormation 管（`check_residuals.sh` 列出 ACTIVE/INACTIVE 数量） | `aws ecs list-task-definitions --family-prefix proto-ecom-` 后逐个 `deregister-task-definition` 再 `delete-task-definitions` |
| ECR 隔离 tag | 扫描门禁阻断的镜像以 `quarantine-<tag>` 保留 | 沙箱 `empty_on_delete` 随仓库删除；交付配置保留仓库，按需 `aws ecr batch-delete-image` |
| 非栈创建的日志组 | RDS 日志导出（`/aws/rds/instance/proto-ecom-<env>-mysql/*`，由 CDK LogRetention 设置保留期后默认保留）、CDK 自定义资源 Lambda（`/aws/lambda/proto-ecom-*-Custom*` 等） | `aws logs describe-log-groups` 按名字核对后逐个删除 |
| Secrets Manager | 删除后默认进入计划删除窗口 | 沙箱可用 `aws secretsmanager delete-secret --force-delete-without-recovery --secret-id <name>`（只针对 `proto-ecom-` 前缀） |
| RDS 迁移前快照 | 开启 `snapshot_before_migrate` 的环境（交付配置 uat/prod）每次部署前创建 `<project>-<env>-predeploy-<时间戳>`，部署脚本只保留最近 `predeploy_snapshots_keep` 个；删除栈不会删除它们（沙箱 dev 未开启） | `scripts/check_residuals.sh` 的「rds pre-deploy snapshots」一行列出剩余快照；确认不再需要后 `aws rds delete-db-snapshot --db-snapshot-identifier <id>` |
| VPC 内 Lambda/CodeBuild 网卡 | 删除 VPC 时偶尔需要等待 ENI 释放 | 重试 destroy |
| CDK bootstrap 资产桶中的模板与代码包 | 属于 CDKToolkit，按内容哈希命名，可能与其他项目共用 | 不随本项目删除；如需清理由账号管理员统一处理 |
| 标签索引中的已删除资源 | `resourcegroupstaggingapi` 有延迟，ECS 集群/服务删除后显示 INACTIVE | 逐个确认状态为 deleted/INACTIVE/DELETE_IN_PROGRESS 即可 |

## 交付环境（`removal_policy=retain`）

数据库、缓存、搜索、EFS、桶、ECR、Secrets 在删除栈后**保留**，且 RDS 开启删除保护。确认数据已备份且不再需要后，先关闭删除保护，再逐个手工删除。
