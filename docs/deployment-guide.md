# 部署指南

> Prototype，只用于非生产环境。一条命令部署的命令写在 [README](../README.md#部署)；本页解释前置条件、参数与部署后的操作。

## 前置条件

| 项 | 说明 |
|---|---|
| AWS 账号与区域 | 非生产账号；目标区域需提供 Fargate ARM64、RDS for MySQL 8.4、ElastiCache Valkey、OpenSearch 3.x、CodeBuild ARM。默认 `us-east-1`（配置文件的 `region`） |
| CDK bootstrap | 目标账号/区域已执行过 `cdk bootstrap`（本项目不会修改 CDKToolkit） |
| 本地工具 | Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js（用 `npx -y aws-cdk@2`，不需要全局安装）、AWS CLI v2、git、zip |
| 本地 Docker | 不需要。镜像只在 CodeBuild arm64 构建机上构建 |
| 服务关联角色 | VPC 内的 OpenSearch 域需要 `AWSServiceRoleForAmazonOpenSearchService`；ECS、ElastiCache 的服务关联角色由服务自动创建 |
| HTTPS 环境 | 部署区域内的 ACM 证书 ARN 与它覆盖的主机名（`certificate_arn`、`site_hostname`）；指向内部 ALB 的私有 DNS 记录由你创建 |
| 凭证 | 使用你自己的临时凭证（环境变量或 profile），不要写进任何文件 |

## 配置文件

- `config/sandbox.json`：一个环境（dev）+ `develop` 流水线 + MCF 桥接（mock，US/na 与 DE/eu 两个市场），删除策略 destroy。实例类为沙箱可部署值，只用于短时验证，不是规格建议。
- `config/customer.json`：dev/test/uat/prod 四个环境 + `develop`、`release` 两条流水线，删除策略 retain、删除保护开启。**数据库/缓存/搜索实例类留空**，uat/prod 为 https 且**证书与主机名留空**——填好之前加载就会失败（错误信息给出官方实例类型页）。只想检查模板时用 `-c synthCheck=true`（用占位值合成，不可部署）。只部署部分环境：`-c envs=dev,test`（cicd 栈总会合成；流水线按名字引用部署项目，未部署环境的阶段会失败）。

## 参数（全部在一个配置文件里）

| 键 | 含义 |
|---|---|
| `project` / `ssm_prefix` | 资源名前缀 / SSM 参数前缀（运维脚本通过 `ECOM_CONFIG` 读取，不写死） |
| `removal_policy` | `destroy`（沙箱，删除栈时清空桶/ECR、删库不留快照）或 `retain`（交付默认） |
| `tags` | 统一打在所有资源上 |
| `build.image_variant` | `magento`（完整 Magento 镜像）或 `smoke`（轻量替身，用于验证流水线/顺序/回滚） |
| `build.magento_ref` / `build.php_image` / `build.codebuild_image` | Magento Git tag、PHP 基础镜像、CodeBuild arm64 镜像 ID |
| `build.block_on_severities` / `scan_timeout_seconds` / `scan_unavailable_action` | 扫描门禁：阻断级别、等待时间、拿不到结果时 `fail`（交付默认）或 `warn`（沙箱）。构建和每次部署都用这组参数 |
| `build.alarm_email` | 可选；为告警/审批主题添加邮件订阅（需确认订阅） |
| `pipelines[]` | 每条流水线的名称、源对象键、环境顺序、`approvals_before`（在哪个环境前加审批及说明；这些环境也受 `promote_image.sh` 保护） |
| `environments.<ENV>` 网络与计算 | `vpc_cidr`、`max_azs`、`nat_gateways`、`deployment`（`rolling`/`blue_green`）、`bake_time_minutes`、`desired_count`、`task_cpu`、`task_memory_mib`、`allowed_ingress_cidrs`（禁止 /0） |
| `environments.<ENV>` 数据层 | `db_instance_class`、`db_engine_version`、`db_multi_az`、`db_allocated_storage_gib`、`cache_node_type`、`cache_engine_version`、`search_instance_type`、`search_engine_version`、`search_data_nodes`、`search_volume_gib` |
| `environments.<ENV>` 发布 | `snapshot_before_migrate`、`predeploy_snapshots_keep`、`maintenance_on_failure`（`disable`/`keep`，见 architecture §3） |
| `environments.<ENV>` 入口 | `base_url_scheme`（`http`/`https`）、`certificate_arn`、`site_hostname` |
| `environments.<ENV>.alarms` | CPU/内存百分比、RDS 剩余存储、ALB 5xx 阈值 |
| `environments.<ENV>.mcf` | `spapi_mode`（mock/live）、`marketplaces`（`{"US": {"id": "<marketplaceId>", "region": "na"}}`）、默认履约策略、最大尝试次数、轮询/同步间隔、mock 发货延迟、Lambda 内存、intake 批大小、单次 HTTP 超时 |

实例类与大小以官方页面为准（链接见 `docs/graviton-compatibility.md` 末尾）。费用按各服务计费维度产生（Fargate vCPU/内存时长、RDS 实例时长与存储、ElastiCache 节点时长、OpenSearch 实例时长与存储、NAT 网关时长与流量、CodeBuild 构建分钟等），以各服务官方定价页为准。

## 首次部署之后

ECS 服务的 desiredCount 为 0（还没有镜像）。提交代码后用 `scripts/package_source.sh develop` 上传源码触发流水线（镜像 tag = commit SHA）。

之后若要再次 `cdk deploy` 平台栈，按环境带上当前运行的镜像 tag，避免 CDK 把服务改回初始状态：

```bash
TAG=$(aws ssm get-parameter --name /proto-ecom/dev/current_image_tag --query Parameter.Value --output text)
# 再执行 README「部署」一节的命令，追加 -c imageTags=dev=$TAG
```

`-c imageTag=<TAG>` 只在只合成一个环境（`-c envs=<ENV>`）时接受。

## 手工晋级已有镜像（沙箱验证 / 应急）

`scripts/promote_image.sh <ENV> <IMAGE_TAG>` 用该环境的部署项目执行同一部署脚本（同样先复查扫描结果）。对流水线中有审批的环境默认拒绝，应急时加 `--break-glass "<理由>"`，理由记录在部署构建的环境变量与日志中。

## 修改 Magento 模块后重新生成 config.php

`app/magento/config/config.php` 必须列出代码中的全部模块，否则构建失败。增删模块（`app/magento/modules/` 或 Composer 依赖）后：

```bash
scripts/export_magento_config.sh        # 在 arm64 构建项目上运行 Dockerfile 的 config-export 目标
git diff app/magento/config/config.php  # 检查后提交
```

有本地 Docker 时也可以 `docker build --target config-export -o type=local,dest=out app/magento`。

## 切换到 Magento 完整镜像

把 `build.image_variant` 改为 `magento`，重新部署 cicd 栈，再上传一次源码。Magento 构建会从 GitHub 拉取官方仓库并执行 composer，需要构建机可访问公网。

## 启用真实 SP-API（不在原型验证范围）

1. 在 Secrets Manager 的 `<PROJECT>-<ENV>-spapi-credentials` 中填入 `lwa_client_id`、`lwa_client_secret` 与每个用到的区域的 `lwa_refresh_token_<na|eu|fe>`。
2. 配置 `mcf.spapi_mode=live` 与 `mcf.marketplaces`，重新部署 mcf 栈。
3. 先用 SP-API 沙箱验证（handover §4 第 9 条）。

## DLQ 重驱

原因修复后：`uv run python scripts/mcf_redrive.py --env dev [--order-id <ID>] [--dry-run]`。
