# ecommerce-graviton-cicd

> **免责声明：本项目是用于演示和学习的原型（Prototype），不用于生产。**
> - 只部署到非生产账号，最好是可随时丢弃的账号。
> - 使用方对部署、使用及其后果（包括费用）自行负责。
> - 进一步使用前，请与你自己的安全和法务团队评估，并遵循 [AWS 责任共担模型](https://aws.amazon.com/compliance/shared-responsibility-model/)。上生产所需的工作见[加固清单](docs/handover.md#4-上生产前需要加固的清单)。

为自建 **Magento Open Source** 独立站在 AWS 上提供一套 Graviton 优先的测试环境与 CI/CD 参考，并附带一个把独立站订单交给 **Amazon Multi-Channel Fulfillment（MCF）** 履约的桥接服务。场景（已抽象）：某 IoT 厂商的海外独立站，先建测试环境，订单从 FBA 库存发货。

## 架构

组件、信任边界、取舍与事实核对见 [docs/architecture.md](docs/architecture.md)（其中的 mermaid 图是事实来源）。

```
<PROJECT>-cicd            ECR、S3 源/产物桶、arm64 镜像构建、流水线、审批
<PROJECT>-<ENV>-platform  VPC、内部 ALB（http，或带 ACM 证书的 https:443）、ECS Fargate ARM64、
                          RDS MySQL、Valkey、OpenSearch、EFS、告警、VPC 内部署项目
<PROJECT>-<ENV>-mcf       EventBridge 总线、SQS + DLQ、DynamoDB、3 个 arm64 Lambda、告警
（<PROJECT> 来自配置文件，默认 proto-ecom）
```

与已有资产的差异（详见 [docs/prior-art.md](docs/prior-art.md)）：

1. **把 Magento 特有的发布顺序放进「一次构建、逐环境晋级」的流水线**：不可变 arm64 镜像（构建时完成 composer、`setup:di:compile`、`setup:static-content:deploy`，`config.php` 来自 Git）、运行时从 Secrets Manager/SSM 渲染 `env.php`、每环境单个串行迁移任务（MySQL 命名锁）且维护窗口覆盖服务切换、发布后 `cache:flush`、按环境设定的失败处理策略。两条流水线（`develop` → dev；`release` → test → 审批 → uat 蓝绿 → 审批 → prod 蓝绿），阶段回滚 + ECS 层回滚。通用的蓝绿与切流前检查已有 AWS 样例（prior-art A12/A13），差异在于围绕它的 Magento 发布顺序。
2. **全栈 Graviton**，逐依赖给出 arm64 依据（[docs/graviton-compatibility.md](docs/graviton-compatibility.md)）。
3. **独立站 ↔ MCF 履约桥接（闭环的一半）**：SKU↔MSKU 映射、按市场/SP-API 区域的库存同步、幂等下单 + 重试后告警 + 显式 DLQ 重驱、发货前取消、物流回传、事务性 outbox。SP-API 调用层默认 **mock**。**店铺侧未实现**：发布 `Order Paid`/`Order Cancelled`、消费发货/库存事件的 Magento 模块只定义了事件契约。

## 前置条件和成本

- 非生产 AWS 账号与区域，且已完成 CDK bootstrap（本项目不修改 CDKToolkit 栈）；Python 3.12+、[uv](https://docs.astral.sh/uv/)、Node.js（用于 `npx -y aws-cdk@2`）、AWS CLI v2、git、zip。不需要本地 Docker：镜像在 CodeBuild（arm64）上构建。
- 参数与前置条件详见 [docs/deployment-guide.md](docs/deployment-guide.md)。
- **成本**：运行中的栈按各服务计费维度收费——Fargate vCPU/内存时长、RDS 实例时长与存储、ElastiCache 节点时长、OpenSearch 实例时长与存储、NAT 网关时长与处理流量、ALB 时长与 LCU、EFS 存储、CodeBuild 构建分钟、CodePipeline 流水线、Lambda 请求与时长、DynamoDB 按需请求、CloudWatch 日志与告警。以各服务官方定价页为准（入口 https://aws.amazon.com/pricing/），不预设免费额度。建议给沙箱账号设置 [AWS Budgets](https://docs.aws.amazon.com/cost-management/latest/userguide/budgets-create.html) 预算告警，用完即销毁。

## 部署

部署命令只写在 README 中（英文 README 的「Deployment」一节与本节相同）：

```bash
make init                       # uv sync + 从 config/*.example.json 复制出 config/*.json（按需修改）
CONFIRM=<LAST_4_DIGITS> make preflight   # 只读检查，发现破坏性变更时拒绝
CONFIRM=<LAST_4_DIGITS> make deploy      # = preflight + cdk deploy --all（ECOM_CONFIG 默认 config/sandbox.json）
git commit ...                  # 镜像 tag = commit SHA
scripts/package_source.sh develop   # 用已提交的代码触发 develop 流水线
```

`ECOM_CONFIG=config/customer.json` 部署四环境交付配置，前提是填好留空的实例类（uat/prod 还要证书 ARN 与主机名）。之后再次部署平台栈时要带上当前运行的镜像 tag，见 [docs/deployment-guide.md](docs/deployment-guide.md#首次部署之后)。

## 使用和验证

- `make verify`：流水线结果、拒绝路径检查（Secret 资源策略、部署角色权限范围、信任策略条件、受保护环境的晋级）以及带 outbox 演练的 MCF 端到端。
- 演示流程：[docs/demo-script.md](docs/demo-script.md)；日常操作（晋级、回滚、DLQ 重驱、维护模式策略）：[docs/handover.md](docs/handover.md)。
- 实际运行了什么、结果不能证明什么：[VERIFICATION.md](VERIFICATION.md)、[DEPLOYMENT-LOG.md](DEPLOYMENT-LOG.md)。

## 故障排查

| 现象 | 原因 / 处理 |
|---|---|
| `configuration file ... not found; run make init` | 真实配置是 `config/*.example.json` 的本地副本 |
| `... not set; choose a Graviton instance class` | 交付配置有意留空实例类，需要填写 |
| preflight 提示 `refusing: the change deletes or replaces resources` | 查看 `/tmp/preflight-diff.txt`；确认并备份后才用 `ALLOW_DESTRUCTIVE=1`（uat/prod 数据存储另需 `ALLOW_DATA_REPLACEMENT=1`）重跑 |
| 部署阶段一直 `waiting for scan (status PENDING)` | 账号级增强扫描尚无结果；由配置 `scan_unavailable_action` 决定（沙箱为 `warn`） |
| 构建在 `check-modules` 失败 | `config.php` 与模块不一致；运行 `scripts/export_magento_config.sh` 后提交 |
| `upgrade required` 之后部署失败，站点处于维护模式 | 该环境 `maintenance_on_failure=keep`；见 handover §3 |
| `cdk deploy` 后服务回到 desiredCount 0 | 带上 `-c imageTags=<ENV>=<TAG>`（当前 tag 在 SSM `<SSM_PREFIX>/<ENV>/current_image_tag`） |

## 安全与局限

[SECURITY.md](SECURITY.md)（控制项与数据处理）、[SECURITY-NOTES.md](SECURITY-NOTES.md)（最小权限、cdk-nag 确认项、已知例外）、[THREAT_MODEL.md](THREAT_MODEL.md)（草案）。局限见 [docs/architecture.md](docs/architecture.md#6-局限)；决策与待确认问题见 [DECISIONS.md](DECISIONS.md)、[OPEN-QUESTIONS.md](OPEN-QUESTIONS.md)。

## 清理

```bash
CONFIRM_DESTROY=<PROJECT> CONFIRM_DATA=yes make destroy   # 交互运行时会确认两次
make verify-cleanup                                       # 有残留时非零退出
```

`cdk destroy` 不会自动删除的资源（`make destroy` 能处理的会处理，其余由 `make verify-cleanup` 列出，详见 [docs/cleanup.md](docs/cleanup.md)）：部署脚本注册的 ECS 任务定义修订；服务创建的日志组（RDS 日志导出、Container Insights、CDK 处理函数）；处于计划删除窗口的 Secret；迁移前 RDS 快照（uat/prod）；交付配置下保留的数据（`removal_policy=retain`：数据库、缓存、搜索域、文件系统、桶、镜像仓库、Secret）；共享 CDK bootstrap 桶中属于本项目的对象（按资产哈希删除；CDKToolkit 栈与 CDK 通用 provider 包保留）。不要以为命令跑完就删干净了，请查看残留报告。

## 目录结构

| 路径 | 内容 |
|---|---|
| `infra/` | CDK 应用（Python）与各栈；`nag.py` 确认项；`trust.py` 信任策略条件 |
| `config/` | 配置模板（`*.example.json`；本地副本不入库） |
| `app/magento/` | 不可变 Magento 镜像（arm64）、提交到 Git 的 `config.php`、项目模块、运行时脚本 |
| `app/smoke/` | 用于验证流水线的轻量替身镜像 |
| `buildspec/`、`scripts/deploy/`、`scripts/build/` | 构建、扫描门禁与晋级逻辑 |
| `scripts/` | 预检、销毁、残留核对、验证、运维工具 |
| `src/mcf/` | MCF 履约桥接（Lambda），含 mock 与 live SP-API 客户端 |
| `tests/`、`local/`、`sample-data/` | 测试、本地演示、合成数据 |
| `.github/` | CI 与部署验证 workflow（尚未在 GitHub 上运行）、模板、CODEOWNERS |

## 本地测试

```bash
make lint test synth scan       # 不需要 AWS 账号
uv run python -m local.run_mcf_demo   # 用 mock SP-API 跑完整履约桥接
```

## Contributing

See [CONTRIBUTING](CONTRIBUTING.md) for more information.

## Security

See [SECURITY](SECURITY.md) for how to report security issues.

## License

This library is licensed under the MIT-0 License. See the [LICENSE](LICENSE) file.
第三方组件保留各自的许可：[THIRD-PARTY-LICENSES](THIRD-PARTY-LICENSES)。Magento Open Source（OSL-3.0）不包含在本仓库中，构建时从官方仓库拉取。
