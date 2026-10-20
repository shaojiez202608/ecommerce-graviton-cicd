# 演示脚本

全程只用合成数据。时长取决于构建与部署等待时间，不在这里给数字。

## A. 不需要 AWS（本地）

1. `uv sync && uv run pytest` —— 单元测试，包括 CDK assertions 与四个 Magento 关键点的守护测试。
2. `uv run python -m local.run_mcf_demo` —— 用 mock SP-API 跑完整履约闭环，讲解输出：
   - 库存同步发现 1 个 MSKU 不存在（标 INVALID）、1 个可售为 0；
   - 订单 101 正常下单 → 发货 → 签收；102 两次 503 后第三次成功；103 三次 503 后标 EXCEPTION 并发出 `Fulfillment Exception`；104 SKU 未映射直接 EXCEPTION；
   - 发给独立站的事件：`Inventory Updated`、`Fulfillment Shipped`（含跟踪链接）、`Fulfillment Delivered`。
3. `npx -y aws-cdk@2 synth -c config=config/customer.json` —— 展示四环境模板与 cdk-nag 通过；打开 `cdk.out/proto-ecom-cicd.template.json` 看 `release` 流水线的阶段顺序与两个审批。

## B. 沙箱账号

1. 部署：`npx -y aws-cdk@2 deploy --all -c config=config/sandbox.json --require-approval never`。
2. 控制台展示：VPC 只有内部 ALB；ECS 任务定义 `ARM64`；RDS/ElastiCache/OpenSearch 的 Graviton 实例族；所有资源带 `Project/Purpose/Teardown` 标签。
3. 流水线：`scripts/package_source.sh develop` → 在 CodePipeline 控制台看 Source → Build（arm64 构建机日志里 `arch=aarch64`、扫描门禁结果）→ Deploy-dev（部署日志依次出现 `migrate task` → `waiting for service` → `cache-flush task` → `[smoke] /health_check.php -> 200`）。
4. 回滚：在 Deploy-dev 阶段选择「Roll back」，或执行 `scripts/rollback.sh dev`；说明 schema 变更时为何会停在迁移步骤。
5. 履约：`uv run python scripts/mcf_e2e.py --env dev`；在 CloudWatch Logs `/aws/events/proto-ecom-dev-mcf-bridge-events` 中查看发给独立站的事件；在 SQS 看 DLQ；在 CloudWatch 告警中看 `OrderExceptions`/`DlqNotEmpty`。
6. 清理：见 `docs/cleanup.md`。
