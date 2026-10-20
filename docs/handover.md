# 交接说明

> Prototype，不用于生产。本节写给接手的客户团队/实施方。

## 1. 交付了什么

- `infra/`：CDK（Python）应用，三类栈：`proto-ecom-cicd`、`proto-ecom-<env>-platform`、`proto-ecom-<env>-mcf`。参数集中在 `config/*.json`。
- `app/magento/`：不可变 Magento 镜像（arm64）的 Dockerfile 与运行时脚本（env.php 渲染、迁移、缓存刷新）。`app/smoke/`：用于验证流程的替身镜像。
- `buildspec/build.yml`、`scripts/deploy/*.sh`：一次构建、逐环境晋级的构建与部署逻辑。
- `src/mcf/`：独立站 ↔ MCF 履约桥接（Lambda arm64），SP-API 调用层含 mock。
- `tests/`：单元测试与 CDK assertions。`local/`：本地 mock 演示。

## 2. 交付默认值与沙箱默认值的区别

| 项 | `config/sandbox.json` | `config/customer.json`（交付默认） |
|---|---|---|
| 删除策略 | destroy：删栈即删数据，自动清空桶与 ECR | **retain**：数据库/缓存/搜索/EFS/桶/ECR/Secrets 保留；RDS 删除保护开启 |
| 日志保留 | 一周 | 三个月 |
| 备份 | RDS 备份保留期最短、EFS 不开自动备份 | RDS 备份保留期更长、EFS 自动备份、Valkey 快照保留 |
| 扫描门禁 | 阻断 CRITICAL | 阻断 CRITICAL + HIGH |
| 镜像 | smoke 替身 | magento |
| 环境/流水线 | dev / develop | dev/test/uat/prod；develop、release（两道审批） |

修改删除策略只需改 `removal_policy`；切换后重新 `cdk deploy`。

## 3. 日常操作

- 发版：提交代码 → `scripts/package_source.sh develop|release`（或改用 CodeConnections 源，见 §5）。
- 审批：`release` 流水线在 uat、prod 前停在审批动作，SNS 主题 `proto-ecom-approvals` 通知审批人；七天未处理视为失败。
- 回滚：见 `docs/architecture.md` §3。数据库迁移不可逆，uat/prod 每次部署前自动快照。
- 重新 `cdk deploy` 平台栈时带 `-c imageTag=<当前 SHA>`。

## 4. 上生产前需要加固的清单

1. **入口**：增加公网入口时使用 CloudFront + AWS WAF + ACM 证书 + HTTPS 监听；ALB 仍放私有子网；后台路径限制来源或接入身份认证；更新 `THREAT_MODEL.md`。
2. **账号隔离**：生产独立账号（AWS Organizations），流水线跨账号部署；部署角色按账号最小权限。
3. **密钥**：DB/OpenSearch/Valkey 凭证开启轮换并协调任务重启；crypt key 不轮换但纳入备份与访问审计；考虑客户管理 KMS 密钥（RDS、EFS、S3、CodeBuild、SNS、SQS、DynamoDB、日志组）。
4. **高可用**：prod 的 RDS Multi-AZ、Valkey 多节点自动故障转移、OpenSearch 多 AZ + 专用主节点、多个 NAT、Web 任务数与自动扩缩容。
5. **Magento 运行面**：部署 cron（`cron:run`）与消息队列消费者；Varnish/CDN 整页缓存；管理员 2FA；关闭生产开发者模式；按官方建议设置文件权限。
6. **构建供应链**：锁定基础镜像 digest；Composer 依赖用私有镜像源/缓存；对 Magento 扩展做许可与安全审查；启用 ECR 增强扫描（Amazon Inspector）。
7. **数据**：uat 使用脱敏副本（脚本与审批流程）；RDS/EFS 备份策略与恢复演练；日志保留与合规要求。
8. **可观测性**：为业务关键路径（下单、支付回调、MCF 下单失败率）建仪表板与告警；告警主题接入值班系统。
9. **MCF**：接真实账户前在 SP-API 沙箱验证；确认签名要求（Q-13）；按官方限流说明实现令牌桶/退避；LWA 凭证轮换流程；店铺侧消费者（发货邮件、上下架）实现与幂等。
10. **合规**：支付卡数据不进入本系统（使用托管支付页/令牌化）；评估 PCI DSS 范围；OSL-3.0 下分发镜像的义务评估（Q-12）。
11. **IAM**：复核 `SECURITY-NOTES.md` 中每一条 cdk-nag 确认项；部署角色加权限边界。
12. **CDK 管理边界**：把镜像 tag 写回 SSM 并由 CDK 读取，或让 CDK 不再管理任务定义镜像，避免重复部署时漂移。

## 5. 常见替换点

- 源：把 S3 源换成 CodeConnections（GitHub/GitLab/Bitbucket），按分支触发两条流水线。
- 计算：如需 EC2 Graviton 容量提供者，替换 `FargateService` 为 `Ec2Service` + ASG（Graviton 实例族）。
- 数据库：若后续确认 Aurora MySQL 提供与 Magento 版本匹配的兼容版本，可替换 `DatabaseInstance`。
