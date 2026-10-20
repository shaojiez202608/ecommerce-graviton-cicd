# 演示脚本

全程只用合成数据。时长取决于构建与部署等待时间，不在这里给数字。部署/清理命令见 README。

## A. 不需要 AWS（本地）

1. `uv sync && uv run pytest` —— 单元测试，包括 CDK assertions、四个 Magento 关键点与部署脚本的守护测试、MCF 可靠性测试（outbox、重驱、取消、多区域）。
2. `uv run python -m local.run_mcf_demo` —— 用 mock SP-API 跑完整履约桥接，讲解输出：
   - 库存同步按市场（US/na、DE/eu）分别调用，发现 1 个 MSKU 不存在（INVALID）、1 个可售为 0；
   - 101 下单时 EventBridge 不可用：状态照常到 SUBMITTED，事件留在 outbox，下一轮轮询补发；
   - 102 两次 503 后成功；103 三次 503 后 EXCEPTION；104 SKU 未映射直接 EXCEPTION；105 走 DE 市场（EU 端点）；106 首次创建的响应丢失，第二次以「已存在」确认；107 三次失败后由运维带 `retry=true` 重驱成功；108 发货前被店铺取消；
   - 发给独立站的事件都带 `eventId`；首轮就已签收的订单先补发 `Fulfillment Shipped` 再发 `Fulfillment Delivered`。
3. `npx -y aws-cdk@2 synth -c config=config/customer.json -c synthCheck=true` —— 四环境模板与 cdk-nag 通过（占位值，不可部署）；看 `release` 流水线的阶段顺序、uat/prod 的 443 HTTPS 监听。

## B. 沙箱账号

1. 部署（README「部署」）。
2. 控制台展示：VPC 只有内部 ALB；ECS 任务定义 `ARM64` 且带 init 进程；RDS/ElastiCache/OpenSearch 的 Graviton 实例族；所有资源带 `Project/Purpose/Teardown` 标签。
3. 流水线：`scripts/package_source.sh develop` → Source → Build（arm64 构建机日志 `arch=aarch64`、扫描门禁结果）→ Deploy-dev（部署日志依次出现扫描复查 → `migrate task` → `waiting for service` → `cache-flush task` → `[smoke] /health_check.php -> 200`）。
4. Schema 变更：晋级一个带新模块的 Magento 镜像，部署日志出现 `upgrade required -> maintenance window`、服务切换后 `maintenance window ... is closed`。
5. 回滚：在 Deploy-dev 阶段选择「Roll back」，或执行 `scripts/rollback.sh dev`。
6. 履约：`uv run python scripts/mcf_e2e.py --env dev --outbox-drill`；在 CloudWatch Logs `/aws/events/<project>-dev-mcf-bridge-events` 中查看发给独立站的事件；在 SQS 看 DLQ；在 CloudWatch 告警中看 `OrderExceptions`/`DlqNotEmpty`/`EventPublishFailures`。
7. 清理：见 README「清理」。
