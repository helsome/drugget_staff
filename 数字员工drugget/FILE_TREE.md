# 药品价格专员文件树

标记：`[现用]` 当前链路使用；`[待接通]` 有代码或测试但未经过真实业务闭环；`[兼容]` 仅为旧命令或测试保留；`[历史]` 用于回放和复盘；`[生成]` 可重建的运行产物；`[原始]` 只读业务事实源。

> **成熟度声明：** 淘宝/天猫和药师帮的最小价格采集闭环，以及两条详情价标准化和“暂不可比较”判断已受限验证。尚无完整规格且业务确认的真实控价规则，因此破价案件、责任路由、通知、公司系统、定时监控和京东采集均未完成真实端到端验收，不能据此宣称“数字价格专员已完成”。

```text
数字员工drugget/
├── README.md                               # [现用] 项目范围、成熟度与运行方式
├── FILE_TREE.md                            # [现用] 本文件
├── AGENTS.md                               # [现用] 核心、入口、适配器的协作边界
├── pyproject.toml / uv.lock                # [现用] Python 包、依赖及命令入口
├── .env.example                            # [现用] 本地配置模板（含药师帮平台）
├── docker-compose.yml / alembic.ini        # [现用] 本地 PostgreSQL 与迁移配置
│
├── src/price_specialist/                   # [现用] 正式业务核心
│   ├── collector.py / orchestrator.py      # [现用] 采集、编排、限速及异常隔离
│   ├── services.py / search.py / catalog.py# [现用] 队列、候选、药品规格归一化
│   ├── evidence.py / incidents.py          # [现用] 证据与人工事件状态机
│   ├── api.py / cli.py / config.py         # [现用] 本地工作台、命令与配置
│   ├── pricing.py / decisions.py            # [部分接通] 价格、包装换算与严格三态控价判断
│   ├── routing.py / alerts.py               # [部分接通] 幂等 dry-run、路由预览和中央待分配；无钉钉发送
│   ├── scheduler.py                         # [待接通] 默认禁用的调度骨架
│   ├── bootstrap.py / smoke_plan.py         # [兼容] 旧烟测资料导入
│   ├── data_quality.py / offline_search.py  # [兼容] 历史资料质量与离线分类
│   └── replay.py                            # [历史] 7.14 结果回放
│
├── collectors/                              # [现用] 受限在线采集入口；不放业务核心或历史结果
│   ├── run_fixture_live_smoke.py            # [现用] 药师帮单种子受限闭环
│   ├── run_taobao_store_closed_loop.py      # [现用] 淘宝已验证店铺闭环
│   ├── export_fixture_run_csv.py            # [现用] CSV 审计包导出
│   └── run_yaoshibang_closed_loop.py         # [兼容] 旧入口转发
│
├── opencli-adapters/                        # [现用] 已验证平台的 OpenCLI 站点适配器
│   ├── taobao/                              # [现用] 店内搜索、候选校验与测试
│   └── yaoshibang/                          # [现用] 登录、搜索、详情、店铺与供应商解析
│
├── data/                                    # 数据边界
│   ├── raw/                                 # [原始] Excel、控价文本和店铺档案，只读
│   ├── knowledge-base/                      # [现用] 标准店铺/药品/包装/控价/责任关系 CSV
│   │   └── price_observations_clean.csv     # [生成/本地] 可由构建脚本重建，不提交
│   └── fixtures/                            # [现用] 小规模验证数据
│       └── 业务知识库测试集/                # [现用] 当前 SQLite 测试库；重建前请保留
│
├── scripts/                                 # [现用] 数据构建脚本
│   ├── build_knowledge_base.py               # [现用] 原始资料 → 全量知识库
│   ├── build_test_knowledge_base.py          # [现用] 知识库 → 小规模测试库；--rebuild-source 可临时自愈
│   └── normalize_confirmed_prices.py         # [现用] 已确认详情价的标准化；不创建通知
│
├── artifacts/                               # 可复核的运行产物
│   ├── evidence/<run_id>/<task_id>/          # [生成/历史] 截图、原始字段、元数据与哈希
│   └── runs/
│       ├── verified/                         # [历史基线] 淘宝、药师帮各一条在线闭环 CSV
│       ├── history/                          # [历史] 调试、中间与负向批次 CSV
│       ├── retired/                          # [废弃] 已排除测试样本的批次 CSV
│       └── current/                          # [生成] 新运行默认输出，不提交
│
├── evidence → artifacts/evidence             # [兼容] 仅保证旧 CSV 中的证据路径仍可打开
│
├── archive/                                  # 不作为运行入口的历史资料
│   ├── legacy-2026-07-14/                    # [兼容] 店铺快照、烟测、目标与匹配 JSON
│   └── prototypes/current-stage/             # [废弃] 旧淘宝/京东单文件原型及输出
│
├── migrations/                               # [现用] Alembic 数据库迁移
├── tests/                                    # [现用] 单元、集成及兼容回归测试
├── outputs/                                  # [兼容/生成] 旧 smoke/search 产物和本地命令输出
├── price_specialist.db                       # [生成] 本地运行库，不提交
└── .venv/、.pytest_cache/、__pycache__/       # [生成] 可重建环境与缓存
```

## 使用规则

1. 新的业务代码只进入 `src/price_specialist/`；新的人工运行入口只进入 `collectors/`。
2. 新验收结果输出到 `artifacts/runs/current/` 和 `artifacts/evidence/`；验收后再人工归入 `verified/`。
3. `archive/` 内的文件不得恢复为正式入口；旧路径仅由兼容命令和回归测试读取。
4. `data/raw/` 不修改；若测试库缺少 `price_observations_clean.csv`，使用 `scripts/build_test_knowledge_base.py --rebuild-source`，不会改写正式知识库。
