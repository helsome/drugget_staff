# 药品价格专员文件树

标记说明：

- `[现用]`：当前业务链路直接使用。
- `[待接通]`：功能骨架已有，但尚未进入当前正式闭环。
- `[兼容]`：现有代码或测试仍依赖，后续应迁移。
- `[历史]`：只用于证据、回归或问题复盘，不是运行入口。
- `[废弃]`：已被新实现替代，不得继续作为正式入口。
- `[生成]`：运行、安装或测试产生，不是源代码。
- `[原始]`：业务事实源，原则上只读。

> **成熟度注意：** `[现用]` 只表示文件被当前代码调用，不等于该功能已经通过真实业务闭环验收。当前只有淘宝/天猫和药师帮的最小采集闭环经过受限在线验证；价格换算、控价判断、通知、公司系统和定时运行最多只有骨架或单元测试，没有端到端验收。

```text
数字员工drugget/
├── README.md                                      # [现用] 项目目标、当前进度、完整工作路线与启动方法
├── FILE_TREE.md                                   # [现用] 本文件，说明每个文件的用途和状态
├── AGENTS.md                                      # [现用] 多 Agent 协作边界：核心、入口、OpenCLI 适配器不得互相复制
├── pyproject.toml                                 # [现用] Python 版本、依赖、pytest 和 `price-specialist` 命令行入口
├── uv.lock                                        # [现用] Python 依赖锁定
├── .env.example                                   # [现用/待修] 本地配置模板；平台列表目前漏了 yaoshibang
├── .gitignore                                     # [现用] 排除环境、运行库、证据、输出和缓存
├── docker-compose.yml                              # [现用] 本地 PostgreSQL 容器
├── alembic.ini                                    # [现用] Alembic 数据库迁移配置
│
├── build_knowledge_base.py                        # [现用] 从历史 Excel/控价文本生成全量业务知识库
├── build_test_knowledge_base.py                   # [现用/阻塞] 抽取小测试库；仍硬依赖当前缺失的 price_observations_clean.csv
├── price_specialist.db                            # [生成] 当前本地运行数据库，不是源数据
│
├── src/
│   ├── price_specialist/
│   │   ├── __init__.py                             # [现用] Python 包入口
│   │   ├── config.py                               # [现用] 数据库、证据、输出、OpenCLI、平台白名单和重试配置
│   │   ├── database.py                             # [现用] SQLite/PostgreSQL 引擎、会话和建表
│   │   ├── models.py                               # [现用] 药品、包装、店铺、任务、候选、价格、事件和通知数据表
│   │   ├── schemas.py                              # [现用] 采集任务、结果、候选、证据和 API 的 Pydantic 契约
│   │   ├── enums.py                                # [现用] 采集、计算、价格、候选、任务和人工事件状态
│   │   ├── errors.py                               # [现用] 业务异常及平台访问异常
│   │   ├── catalog.py                              # [现用] 品牌/通用名/规格/包装归一化和控价规则解析
│   │   ├── collector.py                            # [现用] 统一 OpenCLI 采集器，处理登录、搜索、店铺和详情页
│   │   ├── search.py                               # [现用] 链接规范化、候选去重、药品/规格/店铺分类
│   │   ├── services.py                             # [现用] 任务队列、结果落库、候选保存和固定监控价格评估
│   │   ├── orchestrator.py                         # [现用] 双路线编排、候选升级、平台限速、异常暂停和跨平台隔离
│   │   ├── evidence.py                             # [现用] 脱敏后证据、截图、哈希与原始字段保存
│   │   ├── incidents.py                            # [现用] 验证/登录/限流人工事件状态机和恢复检查
│   │   ├── pricing.py                              # [待接通] 价格解析、包装换算、控价匹配和破价判断
│   │   ├── routing.py                              # [待接通] 破价责任人路由和通知唯一键
│   │   ├── alerts.py                               # [待接通] 通知预览与破价事件 dry-run，不发钉钉
│   │   ├── api.py                                  # [现用/待扩展] 本地 API 与人工验证工作台，尚缺价格历史页
│   │   ├── scheduler.py                            # [待接通] 默认禁用的 APScheduler 每周调度定义
│   │   ├── cli.py                                  # [现用/兼容] Typer 命令行；既有当前批次/API 命令，也有 7.14 旧数据命令
│   │   ├── bootstrap.py                            # [兼容] 将旧烟测计划与原始资料加载到运行库
│   │   ├── data_quality.py                         # [现用/兼容] 原始 Excel 质量检查，仍引用 7.14 店铺快照
│   │   ├── smoke_plan.py                           # [兼容] 生成旧的 smoke_plan；其店铺名归一化函数仍被候选分类复用
│   │   ├── offline_search.py                       # [兼容] 对 7.14 搜索结果做离线候选分类
│   │   ├── replay.py                               # [历史] 回放并评估 7.14 旧烟测结果
│   │   └── logging_config.py                       # [现用] JSON 日志格式
│   └── price_specialist.egg-info/                  # [生成] `pip install -e` 生成的包元数据，可重建
│
├── 采集器/
│   ├── run_fixture_live_smoke.py                 # [现用] 测试库运行入口；药师帮单种子可跑完整闭环
│   ├── run_taobao_store_closed_loop.py           # [现用] 淘宝已验证店铺的单候选受限闭环
│   ├── export_fixture_run_csv.py                  # [现用] 把运行库和测试库导出为中文列名 CSV 审计包
│   ├── run_yaoshibang_closed_loop.py              # [废弃/兼容] 旧药师帮入口，现仅转发到 run_fixture_live_smoke.py
│   ├── yaoshibang-closed-loop-e20448a8/           # [历史基线] 药师帮葛泰首条 verified 价格与供应商闭环证据
│   ├── taobao-store-closed-loop-ec4969aa-.../     # [历史基线] 淘宝阿里健康首条 matched + verified 正式价格
│   ├── taobao-store-closed-loop-87fdfe2d/         # [历史回归] NOT_FOUND 正确落库的负向样本
│   ├── fixture-live-store-W00017-b8df9543/       # [废弃批次] W00017 已从测试样本排除
│   ├── fixture-live-smoke-ab474f72-.../          # [历史] 早期淘宝/药师帮综合批次，仍有未确认候选和人工事件
│   ├── taobao-store-closed-loop-6382d151-.../     # [历史] 早期 verified 但责任店未匹配的中间批次
│   └── 其他 taobao-store-closed-loop-*/          # [废弃批次] 调试期的 NOT_FOUND/异常输出，不得当作当前成功基线
│
├── opencli-adapters/
│   ├── taobao/
│   │   ├── README.md                              # [现用] 淘宝店内搜索规则、强制校验与验收方法
│   │   ├── install.sh                             # [现用] 安装淘宝 OpenCLI 适配器
│   │   ├── package.json                          # [现用] Node 包元数据
│   │   └── clis/taobao/
│   │       ├── shop-search.js                    # [现用] 实际输入/点击店内搜索、搜索词和候选校验
│   │       └── commands.test.js                  # [现用] 淘宝适配器测试
│   └── yaoshibang/
│       ├── cli-manifest.json                       # [现用] auth/search/detail/shop/resolve-provider 命令声明
│       ├── install.sh                              # [现用] 安装药师帮 OpenCLI 适配器
│       ├── package.json                           # [现用] Node 包元数据
│       └── clis/yaoshibang/
│           ├── auth.js                            # [现用] 登录状态检查与人工登录入口
│           ├── search.js                          # [现用] 全站药品搜索
│           ├── detail.js                          # [现用] 详情、价格、规格、店铺和起购数量提取
│           ├── shop.js                            # [现用] 按 provider_id 取供应商档案
│           ├── resolve-provider.js                # [现用] 按店名解析 provider_id
│           └── commands.test.js                   # [现用] 药师帮适配器测试
│
├── migrations/
│   ├── env.py                                     # [现用] Alembic 运行环境
│   ├── script.py.mako                             # [现用] 新迁移脚本模板
│   └── versions/
│       ├── 0001_initial.py                        # [现用] 创建初始业务表
│       └── 0002_store_home_url.py                 # [现用] 增加已验证店铺主页字段
│
├── tests/
│   ├── test_api.py                                # [现用] API、人工事件列表和状态转换
│   ├── test_collector.py                          # [现用] 详情解析、访问异常、药师帮/ 淘宝店内前置校验
│   ├── test_orchestrator.py                       # [现用] 批次、候选升级、限速、平台暂停和任务状态
│   ├── test_search.py                             # [现用] 链接去重、候选分类和周搜索分组
│   ├── test_fixture_runner.py                     # [现用] 药师帮通用种子入口、详情放行和 provider_id 复用
│   ├── test_pricing.py                            # [现用] 价格解析、包装换算、控价命中与异常规则
│   ├── test_evidence.py                           # [现用] 证据保存、脱敏和哈希
│   ├── test_routing.py                            # [现用] 责任路由与通知去重键
│   ├── test_data_quality.py                       # [兼容] 旧店铺数据读取和基础质量检查
│   ├── test_data_quality_integration.py           # [兼容] 7.14 样本、smoke/search 兼容输出和大表数量验证
│   └── test_replay.py                             # [历史] 7.14 旧结果回放
│
├── 过往抓取数据/
│   ├── 网络店铺档案明细表_2026.xlsx              # [原始] 店铺、经营主体、区域和责任人事实源
│   ├── 趣维1-3月总数据.xlsx                       # [原始] 1–3 月历史价格与商品线索
│   ├── 安托监控数据2026年4-6月.xlsx                # [原始] 4–6 月历史价格与商品线索
│   └── 价格标准表.md                           # [原始] 控价规则事实源
│
├── 业务知识库/
│   ├── README.md                                  # [现用] 知识域、质量口径与使用边界
│   ├── manifest.json                              # [现用] 生成时间、源文件哈希、行数和质量摘要
│   ├── store_master.csv                           # [现用] 标准店铺主档
│   ├── responsibility_relations.csv               # [现用] 店铺到责任单位/责任人映射
│   ├── drug_master.csv                            # [现用] 品牌、通用名和历史覆盖
│   ├── drug_package_master.csv                    # [现用] 规格、包装数和最小单位
│   ├── control_price_rules.csv                    # [现用] 控价版本与最小单位控价
│   ├── monitor_task_master.csv                    # [现用线索] 历史商品任务；只应降级为线索，不直接驱动长期采集
│   ├── data_quality_issues.csv                    # [现用] 未识别、未匹配、重复和公式异常明细
│   ├── data_quality_report.md                     # [现用] 质量问题摘要与建议
│   └── price_observations_clean.csv               # [缺失/阻塞] README/manifest 声明应存在，当前工作区实际缺失
│
├── 测试数据/
│   ├── README.md                                  # [现用/待更新] 测试数据边界；其数量描述落后于当前 summary.json
│   ├── 脚本复用评估.md                          # [历史] 旧脚本的复用/废弃判断
│   └── 业务知识库测试集/
│       ├── README.md                              # [现用/待更新] 店铺目标与种子说明
│       ├── summary.json                           # [现用] 当前 5 店、7 目标、19 种子等实际数量
│       └── price_specialist_test.sqlite3          # [现用/请勿删] 当前小规模任务源，在重建问题解决前必须保留
│
├── evidence/
│   ├── ec4969aa-18f2-4761-8239-c63be1b60566/    # [历史基线] 淘宝成功闭环截图与原始字段
│   ├── e20448a8-be8f-4c98-92c2-fee054ec3161/    # [历史基线] 药师帮成功闭环截图与原始字段
│   └── 其他 <run_id>/                            # [生成/历史] 调试、NOT_FOUND、异常和中间批次证据
│
├── outputs/
│   ├── current-stage/                            # [废弃] 旧 CSV 驱动原型与历史输出，不再是新调度器输入
│   │   ├── run_taobao.py                          # [废弃] 旧淘宝双路线单文件脚本
│   │   ├── run_jd.py                              # [废弃] 旧京东脚本，已受限流且异常暂停不完整
│   │   ├── get_shop_home.py                       # [废弃] 旧店铺主页验证原型，能力已进入 collector/淘宝适配器
│   │   ├── generate_summary.py                    # [废弃] 写死店铺数的旧汇总脚本
│   │   ├── verify_3stores.py                      # [废弃] 一次性 3 店验证脚本
│   │   ├── verify_remaining.py                    # [废弃] 一次性剩余店铺验证脚本
│   │   └── *.csv / *.md                          # [历史] 旧任务、候选、失败结果和限流证据
│   ├── smoke/smoke_plan.json                     # [兼容] 旧 bootstrap 和集成测试仍引用
│   ├── search/offline_candidates.json            # [兼容] 旧离线搜索分类输出，集成测试仍引用
│   └── fixture-live-smoke/                       # [废弃] 当前为空目录
│
├── 7.14抓取结果/                                # [兼容/待迁移] 旧店铺快照、烟测、目标和匹配 JSON
├── 首轮京东淘宝系重点药房候选清单.md              # [历史] 早期候选和测试范围参考，不是当前调度输入
├── docs/                                            # [废弃] 空目录
├── .venv/                                           # [生成] 本地 Python 虚拟环境
├── .pytest_cache/                                   # [生成] pytest 缓存，可删除后重建
├── __pycache__/                                     # [生成] Python 字节码缓存，可删除后重建
└── .DS_Store                                        # [生成/无关] macOS Finder 元数据，可删除
```

## 废弃内容的处理原则

1. `[废弃]` 代码不得再被新入口引用。
2. 废弃运行批次如果还有截图、限流或 NOT_FOUND 证据，可保留作问题复盘，但不得作为验收成功样本。
3. `[兼容]` 文件不能直接删除；应先将当前代码和测试迁移到新测试样本。
4. `[生成]` 文件不应提交为业务源文件；运行库和证据仅按复盘或合规需要保留。
