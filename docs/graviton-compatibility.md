# Graviton（arm64）兼容性清单与实测记录（FR-4）

核实日期 2026-10-20。「官方依据」只列官方发布说明/文档/镜像清单；「实测」只写本次真正跑过的结果（详见 `VERIFICATION.md`、`DEPLOYMENT-LOG.md`），没跑的写「未运行」。

| # | 依赖 / 组件 | 本原型使用 | arm64 支持（官方依据） | 实测 |
|---|---|---|---|---|
| G-01 | 计算：ECS on Fargate | `runtimePlatform.cpuArchitecture=ARM64` | Fargate Linux 容器支持 ARM64（F-05） | 已运行：Web 服务与一次性迁移/缓存任务均在 ARM64 Fargate 上运行，冒烟响应 `arch=aarch64` |
| G-02 | PHP 8.3-fpm 运行时镜像 | `public.ecr.aws/docker/library/php:8.3-fpm-bookworm` | Docker 官方镜像提供 arm64v8；ECR Public 镜像清单含 arm64（F-23）；Graviton 指南：PHP 8.2+ 表现良好（F-03） | 已运行：在 arm64 CodeBuild 上构建，运行时报告 PHP 8.3.35 |
| G-03 | PHP 扩展（bcmath、gd、intl、pdo_mysql、soap、sockets、sodium、xsl、zip、opcache、ftp） | `docker-php-ext-install` 在 arm64 构建机上从源码编译 | 官方镜像提供 `docker-php-ext-*` 构建工具，与架构无关；无预编译 x86 二进制依赖 | 已运行：全部扩展在 aarch64 上编译通过，`setup:install` 的扩展检查通过 |
| G-04 | Composer 2 | `public.ecr.aws/docker/library/composer:2`（只取 `/usr/bin/composer`，PHP 脚本） | 官方镜像提供 arm64v8（F-23） | 已运行：`composer install --no-dev` 成功 |
| G-05 | nginx | Debian bookworm `nginx-light` 包 | Debian arm64 官方仓库包；基础镜像为 arm64 | 已运行：ALB 健康检查与首页 200 |
| G-06 | Magento Open Source 2.4.8 代码与 Composer 依赖 | 从官方 Git 仓库构建 | 纯 PHP；Adobe 系统要求页未声明 CPU 架构（F-02）。若加入含原生扩展的第三方模块，需逐个确认 | 已运行：2.4.8-p5 在 arm64 上完成 `setup:di:compile`、`setup:static-content:deploy`、`setup:install`，首页 200 |
| G-07 | 数据库：RDS for MySQL 8.4 | Graviton 实例族（交付配置留空必填；沙箱值只在 `config/sandbox.json`） | RDS 支持 Graviton 实例（F-04）；MySQL 8.4 可选 Graviton 实例族（F-15）；Magento 2.4.8 要求 MySQL 8.4（F-02） | 已运行：Graviton 实例族上 `setup:install`/`setup:upgrade` 成功（DEPLOYMENT-LOG） |
| G-08 | 缓存/会话：ElastiCache for Valkey 8.1 | Graviton 节点类型、TLS + AUTH | Valkey 可选 Graviton 节点族（F-15） | 已运行：Graviton 节点上 `tls://` + AUTH 的会话/缓存/整页缓存可用，`cache:flush` 成功 |
| G-09 | 搜索：Amazon OpenSearch Service 3.1 | `.search` Graviton 实例族 | 服务支持 Graviton 实例（F-04）；3.x 可选 Graviton 实例族（F-15）；2.4.8 要求 OpenSearch 3（F-02） | 已运行：Graviton 实例 + OpenSearch 3.1 + FGAC，`setup:install` 的搜索引擎校验通过 |
| G-10 | 构建：CodeBuild | `ARM_CONTAINER` + `aws/codebuild/amazonlinux-aarch64-standard:3.0`，特权模式 docker build | F-07、F-08 | 已运行：构建日志 `arch=aarch64`；VPC 内部署项目同样为 ARM |
| G-11 | 函数：Lambda（MCF 桥接） | `python3.14`、`arm64`，仅用标准库 + 运行时自带 boto3 | 所有受支持运行时都支持 arm64（F-06） | 已运行：三个函数端到端（VERIFICATION §4） |
| G-12 | 镜像仓库：ECR | 单架构 arm64 镜像 | ECR 支持多架构清单（F-04）；本原型只推 arm64 | 已运行：推送/拉取正常 |
| G-13 | 共享文件：EFS | `pub/media`、`var/` | 与客户端 CPU 架构无关（NFS） | 已运行：访问点 + IAM 授权 + 传输加密挂载成功 |
| G-14 | CDK 自带的自定义资源 Lambda（S3 自动清空、桶通知、日志保留、OpenSearch/EventBridge 日志资源策略等） | CDK 生成 | 模板里这些函数没有 `Architectures` 属性，即 Lambda 默认的 **x86_64**；CDK 未提供统一切换为 arm64 的开关，本原型不改写 CDK 内置处理函数 | 已运行（x86_64）；只在部署/删除栈时执行，不在业务路径上。记入 SECURITY-NOTES「已知例外」 |

## 调优点（只列做法，不给数字）

- OPcache：生产模式开启、关闭时间戳校验（`opcache.validate_timestamps=0`），因为镜像不可变。数值在 `app/magento/docker/php.ini`，按压测结果调整。
- php-fpm 进程数随任务 CPU/内存调整（`php-fpm-pool.conf`）。
- 构建：`setup:static-content:deploy --jobs` 随 CodeBuild 计算类型调整；Magento 变体构建时间较长，构建超时设为参数。
- Graviton 世代选择、实例大小、价格以官方页面为准：
  - EC2/Graviton：https://aws.amazon.com/ec2/graviton/
  - RDS 实例类：https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/Concepts.DBInstanceClass.html
  - ElastiCache 节点类型：https://docs.aws.amazon.com/AmazonElastiCache/latest/dg/CacheNodes.SupportedTypes.html
  - OpenSearch 实例类型：https://docs.aws.amazon.com/opensearch-service/latest/developerguide/supported-instance-types.html
  - Fargate 任务 CPU/内存组合：https://docs.aws.amazon.com/AmazonECS/latest/developerguide/fargate-tasks-services.html
