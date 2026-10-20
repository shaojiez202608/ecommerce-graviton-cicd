# SECURITY-NOTES

## 权限最小化做法

- 每个 CodeBuild 项目、Lambda、ECS 任务有独立角色；授权范围到具体资源（ECR 仓库、表、总线、服务、任务定义族、SSM 路径）。
- 部署项目 `iam:PassRole` 只针对本环境任务角色/执行角色，且附 `iam:PassedToService=ecs-tasks.amazonaws.com` 条件；`ecs:RunTask` 附 `ecs:cluster` 条件。
- MCF Lambda 只有在 `spapi_mode=live` 时才被授予读取 SP-API Secret 的权限。
- 数据库：Web 任务只使用应用库用户（仅 `magento` schema 权限）；RDS 主用户凭证只注入一次性迁移任务。
- 不创建 IAM 用户与长期访问密钥。

## 网络

- 没有公网入站：ALB 为 internal，安全组入站只允许 VPC CIDR 与配置的内网 CIDR；配置加载时拒绝 `/0`。
- 数据层（RDS、Valkey、OpenSearch、EFS）在隔离子网，只接受 Web/一次性任务安全组。
- 公有子网只放 NAT 网关（不自动分配公网 IP）。出站 443 用于访问 AWS API 与（构建时不在 VPC 内的）公网依赖。
- VPC 流日志写入 CloudWatch Logs。

## 数据保护

- 静态加密：RDS、Valkey、OpenSearch、EFS、S3（SSE-S3）、DynamoDB、SQS、ECR（AES-256）、Secrets Manager。
- 传输加密：RDS `require_secure_transport=ON` 且应用使用 RDS CA 包；Valkey `transit_encryption_mode=required` + AUTH；OpenSearch 强制 HTTPS（TLS 1.2 策略）+ 细粒度访问控制；EFS 挂载传输加密 + IAM 授权；S3/SNS/SQS 拒绝非 TLS 请求。
- 机密：数据库、OpenSearch、Valkey、Magento crypt key、管理员口令均由 Secrets Manager 生成并通过 ECS `secrets` 注入；不出现在模板、镜像或环境变量明文里。SP-API Secret 仅含占位值。
- 日志保留：沙箱一周、交付三个月。

## cdk-nag（AwsSolutions）确认项汇总

完整理由见 `infra/ecom_infra/nag.py`。

| 规则 | 范围 | 理由摘要 |
|---|---|---|
| IAM4（AWSLambdaBasicExecutionRole） | 所有栈的 Lambda | 只授予写 CloudWatch Logs |
| IAM4（AmazonECSInfrastructureRolePolicyForLoadBalancers） | 蓝绿环境 | ECS 原生蓝绿切换监听规则所需的 AWS 托管策略 |
| IAM5（Resource::*） | 各栈 | ECR/ECR Public/STS 令牌、ECS RegisterTaskDefinition/ListServiceDeployments、ELB/EC2 Describe 等不支持资源级权限；CDK 自定义资源 |
| IAM5（S3 动作通配、`bucket/*`） | cicd、platform | CDK 授权辅助方法生成，范围限定在单个桶 |
| IAM5（日志组/报告组/ENI/任务/任务定义修订/SSM 路径/快照前缀） | cicd、platform | ID 运行时生成，已按项目、集群、环境前缀限定 |
| IAM5（表索引 `/index/*`） | mcf | 表自身的 GSI |
| S1 | 源桶、产物桶、ALB 日志桶 | 基线为「访问日志按需」；桶私有、加密、仅 TLS |
| SMG4 | 所有 Secret | 原型不配置自动轮换（crypt key 不能随意轮换；其余需协调重启）；列入加固清单 |
| CB4 | CodeBuild 项目 | 产物使用私有桶 SSE-S3；客户管理 KMS 列入加固清单 |
| ECS2 | 任务定义 | 环境变量只含非敏感端点/名称，凭证走 secrets |
| RDS3 / AEC4 / OS7 / OS4 | dev/test | Multi-AZ、可用区感知、专用主节点按环境参数开启，测试环境关闭 |
| RDS10 | 沙箱 | 删除保护跟随删除策略，沙箱关闭 |
| RDS11 | 所有环境 | 保留默认端口，访问已由安全组与隔离子网限制 |
| OS3 / OS5 | 所有环境 | VPC 域 + 细粒度访问控制；开放资源策略是 FGAC 文档化做法 |
| OS9（慢日志） | 所有环境 | 已开应用日志，慢日志按需开启 |
| `@aws-cdk/aws-ecs:shouldUseCircuitBreaker` | 蓝绿环境 | 蓝绿不使用滚动断路器，失败时流量保持在蓝组 |

## 已知风险与例外

- CDK 生成的自定义资源 Lambda（S3 自动清空、通知配置、日志策略等）架构由 CDK 决定，未强制 arm64。
- 内部 ALB 使用 HTTP:80（仅 VPC 内）；跨 VPC/公网访问前必须加 TLS（见 handover）。
- Web 任务出站 443 指向 `0.0.0.0/0`（经 NAT），用于访问 AWS API；可改为 VPC 接口端点以收紧。
- OpenSearch 主用户密码由 CloudFormation 动态引用解析；变更 Secret 后需重新部署域配置。
- 部署脚本使用 `ecs:RegisterTaskDefinition`（不支持资源级权限），理论上可注册任意族的任务定义，但不能运行（RunTask 被限定在本环境 ops 族与集群）。
- 本原型不处理支付卡数据；不对公网开放。
