# 药品价格专员数字员工

这是一个以“店铺是长期监控对象，商品链接是当次发现结果”为核心的药品价格监控系统。

系统从公司历史文档中整理店铺、药品、规格、控价和责任人，再通过两条路线发现当前商品：

```text
路线一：已知店铺/供应商 → 店内搜索 → 候选商品 → 详情页确认
路线二：平台全站搜索   → 发现商品与新店 → 详情页确认
                                              ↓
                               规格/包装换算 → 控价比较
                                              ↓
                               责任归属 → 人工处理/通知
```

历史商品链接只作线索，不再是长期调度入口。搜索列表价不是正式价格；只有详情页完成药品、规格、店铺和价格核对后，才能记为正式价格。

## 重要：当前成熟度声明

> **当前项目真正完成并经过受限在线验证的，只到“采集器闭环”这一步，且目前只覆盖淘宝/天猫和药师帮。**

后续代码的真实状态如下：

| 层级 | 是否有代码 | 是否有自动测试 | 是否经过真实业务闭环验证 |
| --- | --- | --- | --- |
| 淘宝/药师帮搜索、详情确认、正式价格落库 | 有 | 有 | **有，已各完成 1 条在线闭环** |
| 京东采集闭环 | 只有通用骨架 | 有少量通用分支测试 | **没有** |
| 规格、包装、单盒价和最小单位换算 | 有 | 有单元测试 | **没有使用最新在线价格跑通业务闭环** |
| 控价匹配和破价判断 | 有 | 有单元测试 | **没有** |
| 责任人路由和破价事件 | 有 dry-run | 有单元测试 | **没有** |
| 钉钉真实通知 | 没有 | 没有 | **没有** |
| 公司系统中的价格查询、历史回溯和案件闭环 | 没有，只有基础 API/人工事件页 | 只测了基础 API | **没有** |
| 定时常驻运行、日报和规模化监控 | 只有调度骨架 | 没有长时运行测试 | **没有** |

因此，项目目前不得宣称“价格专员数字员工已完成”。准确说法是：

> **淘宝和药师帮的最小价格采集闭环已验证；价格标准化、控价判断、责任通知、公司系统和规模化运行仍待实现与验收。**

## 当前进度（2026-07-20）

| 工作环节 | 状态 | 当前结果 |
| --- | --- | --- |
| 业务资料清洗 | 已完成基础版 | 已形成店铺、药品、包装、控价、责任关系和历史价格数据 |
| 小规模测试库 | 可用 | 当前 SQLite 可用；重建前先运行知识库构建脚本以生成本地 `price_observations_clean.csv` |
| 药师帮双路线闭环 | 已验证 | 葛泰详情价 16.17 元，并反向保存 `provider_id=18650` 供店内复用 |
| 淘宝/天猫店内闭环 | 已验证 | 阿里健康大药房中新托妥 `10mg*48片/盒`，详情价 124 元 |
| 京东闭环 | 待建设 | 核心层保留通用平台接口与限速策略，但当前没有可验收的京东入口和 OpenCLI 适配器 |
| 价格换算与控价判断 | 已对两条在线详情价完成标准化 | 已保存页面价、单盒价、最小单位价与药师帮起购盒数；因无规格精确控价规则，均为“暂不可比较”，未形成破价结论 |
| 异常人工处理 | 基础版已有 | 支持验证码、登录、限流、页面变更转人工和当前任务恢复 |
| 钉钉通知 | 待建设 | 只有通知预览和路由骨架，没有实际发送 |
| 公司系统接入 | 基础 API | 只有健康检查、批次查询和人工事件工作台，尚无业务查询、价格历史和案件闭环页面 |

## 完整工作路线与文件对应

### 1. 原始资料转为业务知识库

| 功能 | 主要文件 | 说明 |
| --- | --- | --- |
| 原始业务文档 | `data/raw/*` | 店铺档案、历史价格和控价标准；只读，不直接改写 |
| 知识库构建 | `scripts/build_knowledge_base.py` | 清洗 Excel/文本，输出业务 CSV、质量问题和源文件哈希 |
| 数据质量检查 | `src/price_specialist/data_quality.py` | 读取原始表格，识别重复、店铺未匹配、药品未识别和公式异常 |
| 业务知识输出 | `data/knowledge-base/*.csv` | 店铺、药品、包装、控价、责任关系、历史线索和质量问题 |

### 2. 业务知识库转为小规模测试库

| 功能 | 主要文件 | 说明 |
| --- | --- | --- |
| 测试样本抽取 | `scripts/build_test_knowledge_base.py` | 选出测试店铺、药品、规格、历史线索和两类任务种子 |
| 测试数据库 | `data/fixtures/业务知识库测试集/price_specialist_test.sqlite3` | 只读的小规模调度输入；当前包含淘宝和药师帮，不包含京东 |
| 任务种子 | 测试库 `task_seeds` | `STORE_SEARCH` 代表店内搜索，`GLOBAL_SEARCH` 代表平台全站搜索 |
| 历史链接 | 测试库 `historical_product_clues` | 只作历史线索，不用于长期任务调度 |

### 3. 任务生成和双路线调度

| 功能 | 主要文件 | 说明 |
| --- | --- | --- |
| 运行入口 | `collectors/run_fixture_live_smoke.py` | 测试库通用入口；单种子全闭环当前支持药师帮 |
| 淘宝受限闭环 | `collectors/run_taobao_store_closed_loop.py` | 使用已验证店铺主页，最多进入 1 个候选详情页 |
| 任务队列 | `src/price_specialist/services.py` | 创建批次、入队、租约任务、保存结果和候选 |
| 批次编排 | `src/price_specialist/orchestrator.py` | 分平台执行、限速、搜索后创建详情任务、一个平台异常时不停其他平台 |
| 任务与结果模型 | `src/price_specialist/models.py` | 数据库表：批次、任务、候选、价格、店铺、事件、破价和通知 |
| 输入输出契约 | `src/price_specialist/schemas.py` | 平台采集器、编排器和 API 之间的 Pydantic 结构 |
| 状态定义 | `src/price_specialist/enums.py` | 采集、任务、价格、候选和人工事件状态 |

### 4. 平台搜索和详情页采集

| 功能 | 主要文件 | 说明 |
| --- | --- | --- |
| 统一采集层 | `src/price_specialist/collector.py` | 调用 OpenCLI，识别登录/验证/限流，解析搜索、店铺和详情字段 |
| 淘宝店内搜索 | `opencli-adapters/taobao/clis/taobao/shop-search.js` | 操作实际店内搜索框，校验搜索词、店铺域名和候选列表 |
| 药师帮全站搜索 | `opencli-adapters/yaoshibang/clis/yaoshibang/search.js` | 返回商品、价格、规格、库存、`wholesale_id` 和 `provider_id` |
| 药师帮详情 | `opencli-adapters/yaoshibang/clis/yaoshibang/detail.js` | 读取详情价、规格、起购数量、店铺和生产厂家 |
| 药师帮店铺身份 | `shop.js` / `resolve-provider.js` | 核验供应商档案，或按店名解析 `provider_id` |
| 药师帮登录 | `auth.js` | 检查持久化会话；需验证时转人工 |
| 京东 | `collector.py` 中的通用分支 | 尚无项目内专用适配器和当前闭环入口，不能算已完成 |

### 5. 候选筛选、详情确认和正式价格

| 功能 | 主要文件 | 说明 |
| --- | --- | --- |
| 链接规范化与去重 | `src/price_specialist/search.py` | 统一淘宝、京东、药师帮商品链接，按商品 ID 去重 |
| 药品/店铺/规格候选分类 | `search.py` + `services.py` | 区分既有对象、同店新链接、新店、规格可疑和不匹配 |
| 搜索后详情任务 | `orchestrator.py` | 只将有效候选升级为 `inspect_candidate`，并将药师帮 `provider_id` 传入详情页 |
| 正式价格放行 | `collectors/run_fixture_live_smoke.py` / `run_taobao_store_closed_loop.py` | 将详情核验成功的候选标记为 `verified_detail` 和 `is_formal_price=True` |
| CSV 导出 | `collectors/export_fixture_run_csv.py` | 按批次导出中文列名的任务、候选、价格、事件和测试种子 |

### 6. 包装换算、控价比较和破价事件

| 功能 | 主要文件 | 当前情况 |
| --- | --- | --- |
| 药品与规格标准化 | `src/price_specialist/catalog.py` | 已有品牌、通用名、规格、每盒数量和最小单位解析 |
| 价格解析与控价判断 | `src/price_specialist/pricing.py` | 已有单盒价/最小单位价计算及低于控价判断 |
| 详情结果接控价 | `src/price_specialist/services.py::evaluate_fixed_result` | 只对包装和控价都精确命中的监控对象放行；新候选闭环尚未完全接通 |
| 破价事件表 | `models.py::PriceBreakEvent` | 表结构已有，当前仅用于 dry-run |

### 7. 异常暂停、证据与人工处理

| 功能 | 主要文件 | 说明 |
| --- | --- | --- |
| 平台异常识别 | `collector.py::detect_access_state` | 识别登录、验证、限流和风险页面 |
| 平台级暂停 | `orchestrator.py` | 当前平台的店内和全站路线一起暂停，其他平台继续 |
| 证据保存 | `src/price_specialist/evidence.py` | 保存脱敏后原始字段、元数据、截图和 SHA-256 |
| 人工事件状态机 | `src/price_specialist/incidents.py` | 待处理、处理中、延期、恢复、禁用会话等转换 |
| 人工工作台 | `src/price_specialist/api.py` 的 `/workbench` | 查看事件、截图并执行恢复检查 |

### 8. 责任路由、通知和公司系统

| 功能 | 主要文件 | 当前情况 |
| --- | --- | --- |
| 责任人路由 | `src/price_specialist/routing.py` | 已能判断命中责任人还是转中央待分配 |
| 通知预览 | `src/price_specialist/alerts.py` | 只生成 dry-run 载荷，不发真实消息 |
| 通知去重 | `routing.py::delivery_idempotency_key` | 生成事件+收件人+渠道唯一键 |
| 公司系统 API | `src/price_specialist/api.py` | 已有健康检查、批次和人工事件 API；价格历史与案件页待建 |
| 钉钉 CLI 机器人 | 尚无实现文件 | 后续建立真实发送适配器，现阶段不得声称已完成 |

### 9. 定时任务与运维

| 功能 | 主要文件 | 当前情况 |
| --- | --- | --- |
| 定时器 | `src/price_specialist/scheduler.py` | 只有默认禁用的每周调度定义，尚未常驻运行 |
| 命令行 | `src/price_specialist/cli.py` | 数据检查、建库、入队、批次执行、会话检查和 API 启动 |
| 配置 | `src/price_specialist/config.py` + `.env.example` | 数据库、证据目录、OpenCLI、平台白名单和 dry-run 通知 |
| 数据库连接 | `src/price_specialist/database.py` | SQLite/PostgreSQL 引擎、会话和建表 |
| 迁移 | `migrations/versions/*.py` | 初始表结构及店铺主页字段 |
| 日志 | `src/price_specialist/logging_config.py` | JSON 日志格式 |

## 当前入口

### 开发环境

```bash
cd "/Users/helson/coding/cttq_work/数字员工drugget"
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
```

`.env.example` 当前的平台列表没有写药师帮。需要运行药师帮时，请在本地 `.env` 中设置：

```dotenv
PRICE_SPECIALIST_ALLOWED_PLATFORMS=jd,taobao,yaoshibang
```

### 运行测试

```bash
.venv/bin/pytest
```

### 药师帮单种子受限闭环

```bash
.venv/bin/python collectors/run_fixture_live_smoke.py \
  --seed-key 'GLOBAL_SEARCH|yaoshibang|优立维|brand_generic' \
  --platform yaoshibang
```

或：

```bash
.venv/bin/python collectors/run_fixture_live_smoke.py \
  --store-id W00010 --brand 葛泰 --platform yaoshibang
```

### 淘宝店内受限闭环

```bash
.venv/bin/python collectors/run_taobao_store_closed_loop.py
```

### 本地 API 和人工工作台

```bash
.venv/bin/price-specialist serve
```

启动后访问 `http://127.0.0.1:8000/workbench`。P0 没有认证，程序会拒绝绑定非本机地址。

## 数据与输出边界

- `data/raw/`：原始业务文档，只读。
- `data/knowledge-base/`：由构建脚本生成的全量标准化数据。
- `data/fixtures/`：小规模快速验证输入，不能反写全量知识库。
- `price_specialist.db`：当前本地运行库，是生成文件，不是源数据。
- `artifacts/evidence/<run_id>/<task_id>/`：截图、原始字段、元数据和哈希。
- `artifacts/runs/`：按批次保存中文列名 CSV 审计包；`verified/` 为在线闭环基线，`history/` 和 `retired/` 仅供回溯。
- `archive/prototypes/current-stage/`：旧原型与历史证据，已废弃为正式入口。

## 当前不得误解的三件事

1. 药师帮和淘宝各有一条正式详情价，但不代表价格标准化和破价判断已完成。
2. 京东配置项、状态和限速骨架已有，但不代表京东闭环已完成。
3. `alerts.py` 和 `routing.py` 只生成通知预览，项目当前没有真实钉钉发送。

## 安全边界

验证码、滑块、登录异常、限流和平台风险提示必须暂停当前平台的两条路线并转人工。项目不包含验证码破解、自动拖动、代理池、设备指纹伪造或反检测功能。

## 详细文件树

所有文件的用途、当前状态与废弃标记见 [FILE_TREE.md](FILE_TREE.md)。
