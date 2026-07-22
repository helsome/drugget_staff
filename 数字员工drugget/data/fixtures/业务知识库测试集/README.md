# 价格专员测试知识库

这个目录由 `scripts/build_test_knowledge_base.py` 从正式业务知识库确定性抽取，用于验证以店铺为入口的双路线调度。正式知识库和原始数据不会被修改。

## 当前规模

- 店铺：5 家（淘宝、药师帮）。
- 店铺药品监控目标：8 条。
- 历史商品链接线索：18 条；仅作线索，不作为当前有效入口。
- 统一任务种子：20 条。
- 历史价格样本：70 条。

## 店铺驱动目标

| 平台 | 店铺 | 目标药品 |
| --- | --- | --- |
| 淘宝 | W00001 阿里健康大药房 | 依伦平、优立维 |
| 淘宝 | W00038 阜胜堂医药专营店 | 托妥 |
| 药师帮 | W00010 云天下 | 葛泰 |
| 药师帮 | W00019 扶正药局 | 优立维 |
| 药师帮 | W06410 敬一堂 | 依伦平、托妥 |

## 任务路线

- `task_seeds.seed_type = STORE_SEARCH`：进入指定店铺搜索目标药品。
- `task_seeds.seed_type = GLOBAL_SEARCH`：按品牌+通用名、品牌+规格进行全局搜索，再解析候选店铺。
- `historical_product_clues`：历史商品链接降级后的候选线索，不能替代店铺搜索。
- 测试库不包含京东记录；`fixture_info.platforms` 为 `taobao,yaoshibang`。

## 重建

```bash
cd "/Users/helson/coding/cttq_work/数字员工drugget"
.venv/bin/python scripts/build_test_knowledge_base.py
```

输出数据库：`price_specialist_test.sqlite3`  
详细数量：`summary.json`
