# 淘宝店内搜索 OpenCLI 适配器

## 目标

以真实的原生鼠标与键盘事件完成淘宝店铺内搜索，避免直接写入 DOM 输入框导致搜索词未进入页面前端状态。该适配器仅用于已验证的真实店铺主页，绝不降级为淘宝全站搜索。

## 命令

```bash
opencli taobao shop-search '品牌名 通用名' \
  --shop_home_url 'https://shop数字.taobao.com/' \
  --expected_shop_name '责任店名称' -f json
```

## 强制校验

1. 主页必须为 `shop数字.taobao.com` 或 `shop数字.tmall.com`，拒绝通用主页。
2. 原生输入后，输入框必须保留完整关键词。
3. 点击后必须仍位于店铺域名，且页面显示 `当前搜索: 品牌名 通用名` 或保留本店搜索词。
4. 候选标题必须同时含品牌名与通用名；只选择排名第一的有效候选。
5. 无控件、搜索词丢失、跳转全站、无候选、详情页未产生商品 ID 均返回 `not_found`，不得产生正式价格。

## 测试

1. 静态：`opencli validate taobao`。
2. 回归：`pytest -q tests/test_collector.py tests/test_orchestrator.py tests/test_search.py`。
3. 在线受限验证：使用 W00001 阿里健康大药房与 `托妥 瑞舒伐他汀钙片`；检查 JSON 的 `query_verified`、`current_url`、候选标题、`item_id`。
4. 闭环验证：运行 `采集器/run_taobao_store_closed_loop.py`，仅当 CSV 同时出现详情任务成功、`verified_detail`、`是否正式价格=True` 才算成功。
