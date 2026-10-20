# 规范对齐 review 表

依据：《AWS 原型项目通用规范与经验》附录 review 清单 A–F（2026-10-21）。「初评」是本轮改动开始前（提交 `875f306`）的状态，「最终」是本轮改动之后的状态；证据给出文件（与行号或符号）。本项目不是 AgentCore 项目，AgentCore 附加清单不适用。

| # | 检查项 | 初评 | 证据（最终） | 处理结果 / 最终 |
|---|---|---|---|---|
| A1 | README 第一屏免责声明（非生产、使用方负责、找安全和法务评估）并链接加固清单 | 需修改（只有「Prototype — not for production」一句） | `README.md:3-8`、`README.zh-CN.md:3-6` | 已改：三要点 + 责任共担模型链接 + 加固清单链接。**满足** |
| A2 | grep 客户名、人名、域名、12 位数字、个人 profile 名、`[INSERT` 残留为 0 | 满足（12 位数字 0；无 profile 名） | `git grep -nE '\b[0-9]{12}\b'` 除占位值 `111122223333` 与脱敏测试的输入样例（`tests/test_ops_scripts.py`，AWS 文档示例号 `123456789012`）外为 0；`git grep -n '\[INSERT'` 为空；历史扫描见 VERIFICATION §5 | **满足** |
| A3 | 占位符统一 `<UPPER_SNAKE>`、`example.com`、`111122223333`；截图有来源记录；数据合成 | 需修改（`<ACCOUNT_ID>`、小写 `<env>` 等） | 全仓替换；`sample-data/` 合成数据；仓库无截图 | 已改。截图：不适用（无截图）。**满足** |
| B1 | LICENSE（含版权行）、SECURITY（完整写法）、CoC、CONTRIBUTING（测试、提交规范、不能提交账号 ID） | 需修改（版权行、CoC、SECURITY 写法不符） | `LICENSE:3`、`SECURITY.md`、`CODE_OF_CONDUCT.md`、`CONTRIBUTING.md` | 已改（DECISIONS D-50）。**满足** |
| B2 | THIRD-PARTY-LICENSES 分「随仓库分发」「下载」两部分；CHANGELOG（Keep a Changelog）+ VERSION | 需修改（缺） | `THIRD-PARTY-LICENSES`、`CHANGELOG.md`、`VERSION` | 已新增；许可来自包元数据/上游许可文件。tag `v0.1.0` 在最终提交上打。**满足** |
| B3 | Issue/PR 模板、AGENTS.md（+ CLAUDE.md）、Makefile、`.gitignore`/`.dockerignore`（`.example` 例外） | 需修改（缺） | `.github/ISSUE_TEMPLATE/*`、`.github/pull_request_template.md`、`AGENTS.md`、`CLAUDE.md`、`Makefile`、`.gitignore`（`config/*.json` + `!config/*.example.json`）、`app/magento/.dockerignore`、`app/smoke/.dockerignore` | 已新增。**满足** |
| B4 | DECISIONS / OPEN-QUESTIONS / SECURITY-NOTES / THREAT_MODEL（DRAFT）/ VERIFICATION / DEPLOYMENT-LOG 齐全 | 需修改（THREAT_MODEL 未标 DRAFT） | `THREAT_MODEL.md:3` | 已改。**满足** |
| C1 | 配置以模板提供，有输入校验，删除策略是参数，标签统一 | 需修改（真实配置入库、校验只覆盖少数字段） | `config/*.example.json`；`infra/ecom_infra/config.py` `resolve_profile`/`_validate_env`/`_validate_app`；`removal_policy`；`tags` + `Teardown=after-test` 校验 | 已改（D-52）。**满足** |
| C2 | 预检（账号、区域、工具链、配额）、破坏性变更保护、冒烟或端到端验证 | 需修改（无预检、无破坏性变更保护） | `scripts/preflight.sh`、`scripts/check_destructive.py`（uat/prod 数据存储需双重确认）、`scripts/deploy/smoke_test.sh`、`scripts/verify.sh`、`scripts/mcf_e2e.py` | 已新增（D-53）。**满足** |
| C3 | 验证记录含日期、区域、配置、提交哈希、失败项和局限 | 需修改（缺提交哈希与「不能证明什么」） | `VERIFICATION.md`、`DEPLOYMENT-LOG.md` | 已按新格式重写。**满足** |
| D1 | 静态检查零未处理发现，抑制有理由；信任策略带 SourceAccount 与 SourceArn；敏感资源有资源策略 | 需修改（整栈确认、无信任条件、Secret 无资源策略） | `infra/ecom_infra/nag.py`（按构造确认）、`infra/ecom_infra/trust.py`、`platform_stack.py` `_lock_secrets`、`mcf_stack.py`（`DenyReadExceptBridge`） | 已改（D-47、D-54、D-55）。CodePipeline 服务角色、ECS 执行角色、ECS 蓝绿基础设施角色、Lambda 执行角色无官方条件键说明，列在 SECURITY-NOTES。**满足（含已记录例外）** |
| D2 | CI 含 lint、测试、synth、密钥扫描；Actions 按 SHA 固定、权限最小；Dependabot 分组；gitleaks 豁免有理由 | 需修改（无 CI） | `.github/workflows/ci.yml`、`.github/workflows/deploy-verify.yml`、`.github/dependabot.yml`、`.gitleaks.toml`（无豁免） | 已新增（D-56）；`uvx zizmor --persona auditor` 无发现。**未在 GitHub 运行，仅本地等效执行** |
| D3 | 没有修改账号级设置 | 满足 | 注册表扫描类型（ENHANCED）未改动；CDKToolkit 未改动 | **满足** |
| E1 | 清理脚本（带确认）和残留核对脚本；README 列出不会被自动删除的资源 | 需修改（只有只读核对脚本，不返回非零） | `scripts/destroy.sh`、`scripts/check_residuals.py`（有残留退出 1）、`scripts/cleanup_bootstrap_assets.py`、`README.md` Cleanup | 已新增。**满足** |
| E2 | 所有用过的区域都核对过，有明确结论 | 满足（只用 us-east-1） | `DEPLOYMENT-LOG.md` 残留核对 | 结论见 DEPLOYMENT-LOG。**满足** |
| F1 | 事实核对表每条有官方链接和日期；成本只写维度；README 章节顺序符合第八节 | 需修改（README 顺序不符） | `docs/architecture.md` §7；README「Prerequisites and cost」只列计费维度 | 已改。**满足** |
| F2 | 提交信息符合 Conventional Commits，正文写验证结果；没有推送远端 | 需修改（旧提交主题不符） | `git log`；历史重写一次（D-58）；无 remote | 已改。**满足** |

## 汇总

- 满足（初评即满足）：3 项（A2、D3、E2）。
- 需修改并已修改：14 项（A1、A3、B1–B4、C1–C3、D1、D2、E1、F1、F2）。
- 不适用：截图来源记录（仓库没有截图）；`ALLOW_PAID` 开关（没有会额外计费的评测类命令，D-53）；AgentCore 附加清单（不是 AgentCore 项目）。
- 带例外的满足：D1（四类服务角色无官方条件键说明，未加条件）；D2（workflow 未在 GitHub 上运行）。
