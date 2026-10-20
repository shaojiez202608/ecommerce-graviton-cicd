# SECURITY-NOTES

## 权限最小化做法

- 每个 CodeBuild 项目、Lambda、ECS 任务有独立角色；授权范围到具体资源（ECR 仓库、表、总线、服务、任务定义族、SSM 路径）。
- 部署项目 `iam:PassRole` 只针对本环境任务角色/执行角色，且附 `iam:PassedToService=ecs-tasks.amazonaws.com` 条件；`ecs:RunTask` 附 `ecs:cluster` 条件。
- MCF Lambda 只有在 `spapi_mode=live` 时才被授予读取 SP-API Secret 的权限。订单条目带版本号，所有写入都是条件写入。
- 数据库：Web 任务只使用应用库用户（仅 `magento` schema 权限）；RDS 主用户凭证只注入一次性迁移任务。
- 不创建 IAM 用户与长期访问密钥。

## 网络

- 没有公网入站：ALB 为 internal，安全组入站只允许 VPC CIDR 与配置的内网 CIDR（http 环境 80；https 环境 80 重定向 + 443）；配置加载时拒绝 `/0`。
- 数据层（RDS、Valkey、OpenSearch、EFS）在隔离子网，只接受 Web/一次性任务安全组。
- 公有子网只放 NAT 网关（不自动分配公网 IP）。出站 443 用于访问 AWS API 与（构建时不在 VPC 内的）公网依赖。
- VPC 流日志写入 CloudWatch Logs。

## 数据保护

- 静态加密：RDS、Valkey、OpenSearch、EFS、S3（SSE-S3）、DynamoDB、SQS、ECR（AES-256）、Secrets Manager。
- 传输加密：RDS `require_secure_transport=ON` 且应用使用 RDS CA 包；Valkey `transit_encryption_mode=required` + AUTH；OpenSearch 强制 HTTPS（TLS 1.2 策略）+ 细粒度访问控制；EFS 挂载传输加密 + IAM 授权；S3/SNS/SQS 拒绝非 TLS 请求。
- 机密：数据库、OpenSearch、Valkey、Magento crypt key、管理员口令均由 Secrets Manager 生成并通过 ECS `secrets` 注入；不出现在模板、镜像或环境变量明文里。SP-API Secret 仅含占位值。
- 日志保留：沙箱一周、交付三个月。

## cdk-nag（AwsSolutions）确认项汇总

完整理由与构造路径见 `infra/ecom_infra/nag.py`。每条确认都挂在具体构造上（路径匹配不到即报错），不做整栈确认；随环境参数变化的确认项带条件（例如只有开启迁移前快照的环境才有快照前缀的 IAM5 确认）。

| 规则 | 构造（栈内路径） | 理由摘要 |
|---|---|---|
| IAM4（AWSLambdaBasicExecutionRole） | 三个桥接函数角色；CDK 处理函数（桶通知、日志保留、AwsCustomResource 单例） | 只授予写 CloudWatch Logs |
| IAM4（AmazonECSInfrastructureRolePolicyForLoadBalancers） | `WebService/AlternateTargetRole`（仅蓝绿环境） | ECS 原生蓝绿切换监听规则所需的 AWS 托管策略 |
| IAM5 `Resource::*` | `ImageBuild` 角色策略 | `ecr:GetAuthorizationToken`、`ecr-public:GetAuthorizationToken`、`sts:GetServiceBearerToken`、`inspector2:ListCoverage/ListFindings` 不支持资源级权限 |
| IAM5 `Resource::*` | `DeployProject` 角色策略 | `ecs:RegisterTaskDefinition/DescribeTaskDefinition`、`inspector2:List*` 不支持资源级权限；`ecs:ListTasks` 附 `ecs:cluster` 条件 |
| IAM5 `Resource::*` | `DeployProject/PolicyDocument`（CDK 生成） | VPC 内 CodeBuild 的 ec2 Describe* 与 ENI 创建/删除 |
| IAM5 `Resource::*` | Web/Ops 任务执行角色 | `ecr:GetAuthorizationToken` |
| IAM5 `Resource::*` | 日志保留处理函数；OpenSearch / EventBridge 日志资源策略自定义资源 | `logs:PutRetentionPolicy`、`logs:PutResourcePolicy` 等作用于栈外创建的日志组或不支持资源级权限 |
| IAM5（S3 动作通配、`bucket/*`） | `ImageBuild`、各流水线角色与源动作角色 | CDK 授权辅助方法生成，范围限定在源桶/产物桶 |
| IAM5（日志组/报告组/ENI/任务/任务定义修订/SSM 路径/产物桶对象/快照前缀） | `ImageBuild`、`DeployProject` 角色策略 | ID 运行时生成，已按项目、集群、环境前缀限定 |
| IAM5（表索引 `/index/*`） | 三个桥接函数角色 | 表自身的 gsi1/gsi2 |
| S1 | 源桶、产物桶、ALB 日志桶 | 基线为「访问日志按需」；桶私有、加密、仅 TLS |
| SMG4 | 平台栈 6 个 Secret、SP-API Secret | 原型不配置自动轮换；列入加固清单 |
| CB4 | `ImageBuild`、`DeployProject` | 产物使用私有桶 SSE-S3；客户管理 KMS 列入加固清单 |
| ECS2 | Web/Ops 任务定义 | 环境变量只含非敏感端点/名称，凭证走 secrets |
| RDS3 / AEC4 | 单 AZ 的环境 | Multi-AZ 按环境参数开启 |
| OS7 | 单数据节点的环境 | 多节点时自动开启可用区感知 |
| OS3 / OS4 / OS5 / OS9 | 所有环境的 OpenSearch 域 | VPC 域 + 细粒度访问控制；专用主节点与慢日志按需 |
| RDS10 | 沙箱 | 删除保护跟随删除策略 |
| RDS11 | 所有环境 | 保留默认端口，访问已由安全组与隔离子网限制 |
| `@aws-cdk/aws-ecs:shouldUseCircuitBreaker` | 蓝绿环境的 `WebService` | 蓝绿不使用滚动断路器，失败时流量保持在蓝组 |

## 服务角色信任策略（confused deputy）

`infra/ecom_infra/trust.py` 给下列服务角色的 AssumeRole 语句同时加 `aws:SourceAccount`（StringEquals）与 `aws:SourceArn`（ArnLike），格式按各服务官方文档（核实日期 2026-10-21）：

| 服务主体 | 角色 | SourceArn | 依据 |
|---|---|---|---|
| `codebuild.amazonaws.com` | 镜像构建、各环境部署项目 | `arn:aws:codebuild:<REGION>:<ACCOUNT>:project/<PROJECT>-*` | https://docs.aws.amazon.com/codebuild/latest/userguide/cross-service-confused-deputy-prevention.html |
| `ecs-tasks.amazonaws.com` | Web/Ops 任务角色 | `arn:aws:ecs:<REGION>:<ACCOUNT>:*`（文档说明不支持指定集群） | https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task-iam-roles.html |
| `vpc-flow-logs.amazonaws.com` | VPC 流日志角色 | `arn:aws:ec2:<REGION>:<ACCOUNT>:vpc-flow-log/*` | https://docs.aws.amazon.com/vpc/latest/userguide/flow-logs-iam-role.html |
| `events.amazonaws.com` | 源桶事件启动流水线的规则目标角色 | `arn:aws:events:<REGION>:<ACCOUNT>:rule/*` | https://docs.aws.amazon.com/eventbridge/latest/userguide/cross-service-confused-deputy-prevention.html |

未加条件（官方文档没有给出这类信任策略的条件键用法，不猜测）：CodePipeline 服务角色（https://docs.aws.amazon.com/codepipeline/latest/userguide/pipelines-create-service-role-cli.html ）、ECS 任务执行角色（https://docs.aws.amazon.com/AmazonECS/latest/developerguide/task_execution_IAM_role.html ）、ECS 蓝绿基础设施角色（https://docs.aws.amazon.com/AmazonECS/latest/developerguide/AmazonECSInfrastructureRolePolicyForLoadBalancers.html ）、Lambda 执行角色（Lambda 文档说明 `aws:SourceArn` 用于以函数为目标的资源策略：https://docs.aws.amazon.com/lambda/latest/dg/permissions-source-function-arn.html ）、CodePipeline 动作角色（信任的是流水线角色本身，不是服务主体）。

## 敏感资源的资源策略

- 每个 Secrets Manager Secret 都有资源策略：拒绝除消费它的任务执行角色以外的所有主体 `GetSecretValue`（管理员也不例外）；CloudFormation 以动态引用解析的 Secret（RDS 主用户、OpenSearch 主用户、Valkey AUTH）另外放行 CDK 的 CloudFormation 执行角色（`cdk-*-cfn-exec-role-*`）。SP-API Secret 只允许三个桥接函数读取（`platform_stack.py` `_lock_secrets`、`mcf_stack.py`）。
- S3 桶：Block Public Access、拒绝非 TLS；SQS 队列与 SNS 主题拒绝非 TLS；SQS 只接受 EventBridge 规则（带 `aws:SourceArn`）发送的事件（CDK 生成）。
- 未加资源策略：DynamoDB 表（只有本账号内被授权的角色能访问；表资源策略列入加固清单）、EventBridge 自定义总线（无资源策略即只允许本账号）。

## 部署相关的权限

- 部署项目：`ecs:RunTask` 只对本环境 ops 任务族且附 `ecs:cluster` 条件；`ecs:DescribeTasks/ListTasks/StopTask` 只对本集群的任务；`iam:PassRole` 只针对本环境任务角色并附 `iam:PassedToService`；`ssm:PutParameter` 只对 `<SSM_PREFIX>/<ENV>/current_image_tag`；`rds:CreateDBSnapshot/DeleteDBSnapshot` 只对本环境 `predeploy-*` 前缀（只在开启快照的环境授予）；`ecr:DescribeImages/DescribeImageScanFindings` 只对本项目仓库。
- 构建项目：`ecr:BatchDeleteImage` 只对本项目仓库，用于隔离被扫描门禁阻断的镜像。

## 已知风险与例外

- CDK 生成的自定义资源 Lambda（S3 自动清空、桶通知、日志保留、日志资源策略等）没有设置架构，即 x86_64（`docs/graviton-compatibility.md` G-14）。
- **传输加密例外**：HTTPS 环境的 TLS 在内部 ALB 终止，ALB → 任务为 VPC 内 HTTP:8080（只允许 ALB 安全组访问）；http 环境（沙箱 dev，交付配置 dev/test）在 VPC 内全程 HTTP。跨 VPC/公网访问前必须启用 HTTPS 并评估端到端 TLS（handover 加固清单第 1 条）。
- Web 任务出站 443 指向 `0.0.0.0/0`（经 NAT），用于访问 AWS API；可改为 VPC 接口端点以收紧。
- OpenSearch 主用户密码由 CloudFormation 动态引用解析；变更 Secret 后需重新部署域配置。
- 部署脚本使用 `ecs:RegisterTaskDefinition`（不支持资源级权限），理论上可注册任意族的任务定义，但不能运行（RunTask 被限定在本环境 ops 族与集群）。
- `promote_image.sh --break-glass` 能绕过流水线审批晋级到 uat/prod：只依赖调用者的 IAM 权限与可审计的理由，生产中应再加 IAM 条件或审批。
- MCF 桥接事件是至少一次投递（outbox 补发、并发更新时可能重复），消费者必须按 `eventId` 去重。
- 本原型不处理支付卡数据；不对公网开放。
