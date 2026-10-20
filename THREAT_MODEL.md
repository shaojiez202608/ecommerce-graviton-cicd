# THREAT_MODEL

> **Status: DRAFT** — not reviewed by the deploying organisation's security team yet.

本原型**不使用 LLM**，没有把不可信输入交给模型处理，因此通用要求中「提示注入」类威胁不适用。下面覆盖实际存在的不可信输入：独立站订单事件、SP-API 响应、构建输入（源码与依赖）。

## 资产

订单与收件地址（个人信息）、SP-API LWA 凭证、数据库/搜索/缓存凭证、Magento crypt key、构建产物（镜像）、部署权限。

## 信任边界

1. 独立站 → EventBridge 总线（`Order Paid`、`Order Cancelled` 事件）。
2. MCF 桥接 → SP-API（外部 HTTPS）。
3. 源码/依赖（GitHub、Packagist、公共镜像）→ CodeBuild → ECR。
4. CodePipeline → 各环境 VPC 内部署项目 → ECS。

## 威胁与对策（STRIDE 摘要）

| 威胁 | 场景 | 对策 | 残余风险 |
|---|---|---|---|
| 伪造（S） | 非店铺来源向总线发送 `Order Paid`，触发真实发货 | 总线为自定义总线，只有被授权的主体能 PutEvents（无资源策略开放）；规则只匹配 `source=storefront.orders`；live 模式需显式开启 | 拥有账号内 PutEvents 权限的主体仍可伪造；生产应给店铺发布方独立角色并在事件中加签名/对账 |
| 篡改（T） | 事件字段被构造为超长/非法值（地址、数量、SKU） | 解析时校验必填字段、数量为正、配送等级与履约策略为白名单；ID 截断到模型上限；SKU 必须在映射表中且为 VALID | 地址内容本身不做校验，依赖 SP-API 拒绝 |
| 抵赖（R） | 无法追溯谁批准了生产发布 | 手工审批记录在 CodePipeline 执行历史；CloudTrail 记录 API 调用 | CloudTrail 组织级配置由账号管理员负责 |
| 信息泄露（I） | 订单地址写入日志；凭证泄露 | 日志只记订单号与状态；凭证在 Secrets Manager，按需授予；Lambda 不在 mock 模式下读取 SP-API Secret | 事件日志组（替身）包含发布给店铺的事件详情（含跟踪号，不含地址）；按需缩短保留期 |
| 拒绝服务（D） | 大量事件压垮 SP-API 配额或 Lambda | SQS 缓冲 + 事件源 `max_concurrency` 限制 + 重投次数上限 + DLQ 告警 | 未实现基于响应头的自适应限流（见 handover） |
| 越权（E） | 部署项目被滥用以运行任意任务 | RunTask 限定到本环境 ops 任务族与集群；PassRole 限定到本环境任务角色 | RegisterTaskDefinition 无法资源级限制 |
| 供应链 | 依赖或基础镜像被投毒 | 基础镜像来自 ECR Public 的 Docker 官方镜像镜像；ECR 扫描门禁；镜像 tag 不可变；同一 SHA 只构建一次 | 未锁定镜像 digest 与 Composer 依赖哈希之外的校验；生产需私有依赖源 |
| 重复发货 | 事件重复投递或确认丢失 | `sellerFulfillmentOrderId` 确定性生成；DynamoDB 条件写入；4xx 时查询是否已存在 | 依赖 MCF 侧对重复 ID 的处理（以官方行为为准） |
| 伪造取消 | 非店铺来源发送 `Order Cancelled`，取消真实订单 | 与 `Order Paid` 同一总线与规则（只匹配 `source=storefront.orders`，无开放资源策略）；发货后拒绝取消 | 同「伪造」行；生产需事件签名或对账 |
| 事件丢失/重复 | EventBridge 暂时不可用、Lambda 中途失败 | 事务性 outbox（状态与待发事件同一次条件写入），轮询补发；事件带 `eventId` | 至少一次投递，消费者必须去重 |
| 绕过审批 | 运维脚本把未审批的镜像推到 uat/prod | `promote_image.sh` 对有审批的环境默认拒绝，`--break-glass` 需理由并记录；部署前复查扫描结果 | 仍依赖调用者的 IAM 权限；生产应限制谁能启动部署项目 |
