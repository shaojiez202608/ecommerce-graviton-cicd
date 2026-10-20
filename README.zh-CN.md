# ecommerce-graviton-cicd

> **Prototype，不用于生产。** 只用于非生产测试环境。上生产前请先完成[加固清单](#上生产前需要加固的清单)。

为自建 **Magento Open Source** 独立站在 AWS 上提供一套 Graviton 优先的测试环境与 CI/CD 参考，并附带一个把独立站订单交给 **Amazon Multi-Channel Fulfillment（MCF）** 履约的小服务。场景（已抽象）：某 IoT 厂商的海外独立站，先建测试环境，订单从 FBA 库存发货。

建议仓库名：`ecommerce-graviton-cicd`。

## 与已有资产的差异

详见 [docs/prior-art.md](docs/prior-art.md)。本原型聚焦：

1. **把 Magento 特有步骤放进「一次构建、逐环境晋级」的流水线**：不可变镜像（构建时完成 composer、`setup:di:compile`、`setup:static-content:deploy`）、运行时从 Secrets Manager/SSM 渲染 `env.php`、每环境单点串行的迁移任务 + 维护模式、服务稳定后 `cache:flush`。两条流水线（`develop` → dev；`release` → test → 审批 → uat 蓝绿 → 审批 → prod 蓝绿），阶段回滚 + ECS 层回滚。
2. **全栈 Graviton**，逐依赖给出 arm64 依据（[docs/graviton-compatibility.md](docs/graviton-compatibility.md)）：ECS Fargate ARM64、CodeBuild ARM、RDS for MySQL 8.4、ElastiCache for Valkey、OpenSearch Service、Lambda arm64。
3. **独立站 ↔ MCF 粘合层**：SKU↔MSKU 映射与校验、库存同步事件、幂等的 `createFulfillmentOrder` + 重试后告警、物流回传事件。SP-API 调用层默认 **mock**，不接真实卖家账户、不含任何凭证。

不重复实现 Magento 自带功能（购物车、结算、支付），也不重复 Amazon 已发布的 SP-API 操作示例。

## 快速开始

```bash
uv sync
uv run pytest                                   # 单元测试 + CDK assertions（不需要 AWS）
uv run python -m local.run_mcf_demo             # 用 mock SP-API 跑完整履约闭环

# 部署到沙箱账号（一个环境 + 流水线 + MCF 桥接）
npx -y aws-cdk@2 deploy --all -c config=config/sandbox.json --require-approval never
scripts/package_source.sh develop               # 用已提交的代码触发流水线
```

参数集中在一个文件（`config/sandbox.json` 或 `config/customer.json`）。文档：[架构](docs/architecture.md) · [部署](docs/deployment-guide.md) · [演示](docs/demo-script.md) · [交接](docs/handover.md) · [清理](docs/cleanup.md)。

## 上生产前需要加固的清单

公网入口（CloudFront + WAF + TLS）；生产独立账号；密钥轮换与客户管理 KMS；数据层 Multi-AZ；Magento cron 与队列消费者；锁定基础镜像与私有依赖源；UAT 脱敏数据；仪表板与值班告警；SP-API 沙箱验证、限流与凭证轮换；PCI 范围评估；逐条复核 cdk-nag 确认项。完整清单见 [docs/handover.md](docs/handover.md)。

## 安全与许可

- [SECURITY-NOTES.md](SECURITY-NOTES.md)、[THREAT_MODEL.md](THREAT_MODEL.md)、漏洞上报 [SECURITY.md](SECURITY.md)。
- 本仓库采用 [MIT-0](LICENSE)。Magento Open Source（OSL-3.0）**不包含**在仓库中，构建时从官方仓库拉取。
- 验证结果：[VERIFICATION.md](VERIFICATION.md)、[DEPLOYMENT-LOG.md](DEPLOYMENT-LOG.md)；待确认问题与决策：[OPEN-QUESTIONS.md](OPEN-QUESTIONS.md)、[DECISIONS.md](DECISIONS.md)。
