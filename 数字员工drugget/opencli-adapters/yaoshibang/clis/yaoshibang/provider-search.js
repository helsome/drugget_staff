import { cli, Strategy } from '@jackwener/opencli/registry';
import { ArgumentError } from '@jackwener/opencli/errors';

/**
 * 药师帮 供应商内搜索
 *
 * 在全局搜索结果中按 provider_id 过滤，专门用于 STORE_SEARCH 路线。
 * 相比 collector 端的"全局 search + 客户端按 provider_id 过滤"做法，这里把
 * pagesize 提升到 60（默认 search 的 3 倍），显著降低目标供应商因排名靠后
 * 而被截断的概率。
 *
 * 输出列与 search.js 完全一致，collector 的 SearchHit 构建逻辑无需改动。
 */

function parsePriceFromToken(priceToken) {
  if (!priceToken) return null;
  try {
    const decoded = Buffer.from(priceToken, 'base64').toString('latin1');
    const matches = decoded.match(/\d+\.\d+/g);
    if (matches && matches.length > 0) {
      return parseFloat(matches[0]);
    }
  } catch { /* fallthrough */ }
  return null;
}

function extractSearchResults(drugList, limit, providerId) {
  if (!Array.isArray(drugList)) return [];
  const results = [];
  for (let i = 0; i < drugList.length && results.length < limit; i++) {
    const item = drugList[i];
    if (!item) continue;
    const itemProviderId = String(item.providerId || item.provider_id || '');
    // 严格按 provider_id 过滤：这是 provider-search 的核心语义。
    if (providerId && itemProviderId !== providerId) continue;
    const price = parsePriceFromToken(item.priceToken);
    const joinCar = item.joinCarMap || {};
    // 保留原始全局 rank（在完整结果中的位置）便于审计。
    results.push({
      rank: i + 1,
      title: item.drugname || item.cn_name || '',
      price: price !== null ? price.toFixed(2) : null,
      spec: item.specification || '',
      shop: item.provider_name || '',
      wholesale_id: String(item.wholesaleid || ''),
      provider_id: itemProviderId,
      manufacturer: item.manufacturer || '',
      unit: item.unit || joinCar.unit || '',
      stock: joinCar.stockDisplay || joinCar.stockStatus || '',
      url: `https://dian.ysbang.cn/#/druginfo?wholesaleId=${item.wholesaleid}&providerId=${itemProviderId}`,
    });
  }
  return results;
}

cli({
    site: 'yaoshibang',
    name: 'provider-search',
    access: 'read',
    description: '药师帮供应商内搜索 - 在全局搜索结果中按 provider_id 过滤',
    domain: 'dian.ysbang.cn',
    strategy: Strategy.COOKIE,
    args: [
        { name: 'query', positional: true, required: true, help: '搜索关键词（品牌+通用名）' },
        { name: 'provider_id', required: true, help: '目标供应商ID' },
        { name: 'limit', type: 'int', default: 60, help: '全局搜索抓取条数（过滤前，max 60）' },
        { name: 'page', type: 'int', default: 1, help: '页码' },
    ],
    columns: ['rank', 'title', 'price', 'spec', 'shop', 'wholesale_id', 'provider_id', 'manufacturer', 'unit', 'stock', 'url'],
    navigateBefore: false,
    func: async (page, kwargs) => {
        const query = String(kwargs.query || '').trim();
        const providerId = String(kwargs.provider_id || '').trim();
        if (!query) {
            throw new ArgumentError('药师帮供应商内搜索关键词不能为空');
        }
        if (!providerId) {
            throw new ArgumentError('provider_id 不能为空');
        }
        // pagesize 提升到 60（默认 search 的 3 倍），降低目标供应商被截断的概率。
        const pageSize = Math.min(Math.max(Number(kwargs.limit) || 60, 1), 60);
        const pageNum = Math.max(Number(kwargs.page) || 1, 1);

        const searchUrl = `https://dian.ysbang.cn/#/indexContent?searchkey=${encodeURIComponent(query)}&page=${pageNum}&pagesize=${pageSize}&firstSearch=true`;
        await page.goto(searchUrl);
        await page.wait(6);

        const data = await page.evaluate(`
            (async () => {
                const maxWait = 15;
                for (let i = 0; i < maxWait; i++) {
                    const app = document.querySelector('#app');
                    if (app && app.__vue__) {
                        const store = app.__vue__.$store;
                        if (store && store.state && store.state.drugList) {
                            const drugList = store.state.drugList.drugList;
                            if (Array.isArray(drugList) && drugList.length > 0) {
                                return drugList.map(item => ({
                                    wholesaleid: item.wholesaleid,
                                    drugname: item.drugname,
                                    cn_name: item.cn_name,
                                    priceToken: item.priceToken,
                                    specification: item.specification,
                                    provider_name: item.provider_name,
                                    providerId: item.providerId || item.provider_id,
                                    manufacturer: item.manufacturer,
                                    unit: item.unit || (item.joinCarMap ? item.joinCarMap.unit : ''),
                                    joinCarMap: item.joinCarMap ? {
                                        stockDisplay: item.joinCarMap.stockDisplay,
                                        stockStatus: item.joinCarMap.stockStatus,
                                        validDate: item.joinCarMap.validDate,
                                        unit: item.joinCarMap.unit,
                                        drugMinAmount: item.joinCarMap.drugMinAmount,
                                        limitMessage: item.joinCarMap.limitMessage,
                                    } : null,
                                }));
                            }
                        }
                    }
                    await new Promise(r => setTimeout(r, 1000));
                }
                return [];
            })()
        `);

        return extractSearchResults(data, pageSize, providerId);
    },
});
