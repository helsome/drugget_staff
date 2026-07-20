# 价格专员测试数据

本目录只保存用于快速迭代和验证的数据。正式业务知识库仍位于 `业务知识库/`，原始历史文件仍位于 `过往抓取数据/`，两者均不由测试运行改写。

## 当前内容

```text
测试数据/
├── README.md
├── 脚本复用评估.md
└── 业务知识库测试集/
    ├── price_specialist_test.sqlite3
    ├── summary.json
    └── README.md
```

测试库以店铺为驱动，包含6家目标店铺、8条店铺药品监控目标、14条历史商品链接线索和20条统一任务种子（淘宝、药师帮各4条店铺搜索种子及6条全局搜索种子）。京东不在本轮测试范围内。历史链接仅作线索，不假定现在仍然有效。

## 后续运行目录

每次测试运行应写入独立目录或数据库批次，不能覆盖测试基准：

```text
测试数据/测试运行结果/<run_id>/
```

旧的 `outputs/current-stage/` 只作为历史原型和历史执行证据，不再作为新调度器的正式输入。

## 重建测试库

```bash
cd "/Users/helson/coding/cttq_work/数字员工drugget"
.venv/bin/python build_test_knowledge_base.py
```
