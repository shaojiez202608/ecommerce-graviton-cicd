# 部署指南

> Prototype，只用于非生产环境。

## 前置条件

| 项 | 说明 |
|---|---|
| AWS 账号与区域 | 非生产账号；目标区域需提供 Fargate ARM64、RDS for MySQL 8.4、ElastiCache Valkey、OpenSearch 3.x、CodeBuild ARM。默认 `us-east-1`（`config/*.json` 的 `region`） |
| CDK bootstrap | 目标账号/区域已执行过 `cdk bootstrap`（本项目不会修改 CDKToolkit） |
| 本地工具 | Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js（用 `npx -y aws-cdk@2`，不需要全局安装）、AWS CLI v2、git、zip |
| 本地 Docker | 不需要。镜像只在 CodeBuild arm64 构建机上构建 |
| 服务关联角色 | VPC 内的 OpenSearch 域需要 `AWSServiceRoleForAmazonOpenSearchService`；ECS、ElastiCache 的服务关联角色由服务自动创建 |
| 凭证 | 使用你自己的临时凭证（环境变量或 profile），不要写进任何文件 |

## 一条命令部署

```bash
uv sync
npx -y aws-cdk@2 deploy --all -c config=config/sandbox.json --require-approval never
```

- `config/sandbox.json`：一个环境（dev）+ `develop` 流水线 + MCF 桥接（mock），删除策略 destroy。
- `config/customer.json`：dev/test/uat/prod 四个环境 + `develop`、`release` 两条流水线，删除策略 retain、删除保护开启。只部署部分环境：`-c envs=dev,test`（cicd 栈总会合成；流水线引用的部署项目按名字查找，未部署的环境阶段会失败）。

首次部署后，ECS 服务的 desiredCount 为 0（还没有镜像）。触发一次流水线：

```bash
git commit ...                                 # 镜像 tag = commit SHA，必须先提交
scripts/package_source.sh develop              # 上传 source/develop.zip，EventBridge 启动流水线
```

之后若要再次 `cdk deploy` 平台栈，带上当前已晋级的镜像 tag，避免 CDK 把服务改回初始状态：

```bash
npx -y aws-cdk@2 deploy proto-ecom-dev-platform -c config=config/sandbox.json -c imageTag=<SHA>[-smoke]
```

## 参数（全部在一个配置文件里）

| 键 | 含义 |
|---|---|
| `project` / `ssm_prefix` | 资源名前缀 / SSM 参数前缀（对应补充材料的 `/<shop>/<env>/`） |
| `removal_policy` | `destroy`（沙箱，删除栈时清空桶/ECR、删库不留快照）或 `retain`（交付默认） |
| `tags` | 统一打在所有资源上 |
| `build.image_variant` | `magento`（完整 Magento 镜像）或 `smoke`（轻量替身，用于验证流水线/顺序/回滚） |
| `build.magento_ref` / `build.php_image` | Magento Git tag 与 PHP 基础镜像 |
| `build.codebuild_image` | CodeBuild arm64 镜像 ID |
| `build.block_on_severities` | ECR 扫描门禁阻断的严重级别 |
| `build.alarm_email` | 可选；为告警/审批主题添加邮件订阅（需确认订阅） |
| `pipelines[]` | 每条流水线的名称、源对象键、环境顺序、`approvals_before`（在哪个环境前加审批及说明） |
| `environments.<env>.*` | VPC CIDR、AZ 数、NAT 数、部署方式（`rolling`/`blue_green`）、任务数与任务 CPU/内存、数据库/缓存/搜索的实例类与版本、Multi-AZ、允许访问 ALB 的内网 CIDR（禁止 /0）、迁移前是否快照、MCF 参数 |
| `environments.<env>.mcf.*` | `spapi_mode`（mock/live）、SP-API 区域、市场 → marketplaceId、默认履约策略、最大尝试次数、轮询/同步间隔、mock 发货延迟 |

实例类与大小：配置里的值只是沙箱占位值。选择请以官方页面为准（链接见 `docs/graviton-compatibility.md` 末尾）。费用按各服务计费维度产生（Fargate vCPU/内存时长、RDS 实例时长与存储、ElastiCache 节点时长、OpenSearch 实例时长与存储、NAT 网关时长与流量、CodeBuild 构建分钟等），以各服务官方定价页为准。

## 切换到 Magento 完整镜像

把 `build.image_variant` 改为 `magento`，重新部署 cicd 栈，再上传一次源码。Magento 构建会从 GitHub 拉取官方仓库并执行 composer，需要构建机可访问公网。

## 启用真实 SP-API（不在原型验证范围）

1. 在 Secrets Manager 的 `proto-ecom-<env>-spapi-credentials` 中填入 `lwa_client_id`、`lwa_client_secret`、`lwa_refresh_token`（LWA 应用凭证，类型见 Secret 描述）。
2. 配置 `mcf.spapi_mode=live`、`mcf.spapi_region`、`mcf.marketplaces`，重新部署 mcf 栈。
3. 先阅读 `OPEN-QUESTIONS.md` Q-13 并用 SP-API 沙箱验证。
