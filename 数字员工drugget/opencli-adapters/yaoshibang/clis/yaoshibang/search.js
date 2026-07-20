import { cli, Strategy } from '@jackwener/opencli/registry';
import { ArgumentError } from '@jackwener/opencli/errors';

/**
 * 药师帮 药品搜索
 *
 * 搜索关键词（品牌+通用名），返回商品列表。
 * 药师帮使用 Vue SPA，商品数据存储在 Vuex store 中。
 * 价格数据在 DOM 层被字体混淆，但 priceToken（protobuf）包含明文价格。
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

function extractSearchResults(drugList, limit) {
  if (!Array.isArray(drugList)) return [];
  const results = [];
  for (let i = 0; i < drugList.length && results.length < limit; i++) {
    const item = drugList[i];
    if (!item) continue;
    const price = parsePriceFromToken(item.priceToken);
    const joinCar = item.joinCarMap || {};
    results.push({
      rank: i + 1,
      title: item.drugname || item.cn_name || '',
      price: price !== null ? price.toFixed(2) : null,
      spec: item.specification || '',
      shop: item.provider_name || '',
      wholesale_id: String(item.wholesaleid || ''),
      provider_id: String(item.providerId || ''),
      manufacturer: item.manufacturer || '',
      unit: item.unit || joinCar.unit || '',
      stock: joinCar.stockDisplay || joinCar.stockStatus || '',
      url: `https://dian.ysbang.cn/#/druginfo?wholesaleId=${item.wholesaleid}&providerId=${item.providerId}`,
    });
  }
  return results;
}

cli({
    site: 'yaoshibang',
    name: 'search',
    access: 'read',
    description: '药师帮药品搜索',
    domain: 'dian.ysbang.cn',
    strategy: Strategy.COOKIE,
    args: [
        { name: 'query', positional: true, required: true, help: '搜索关键词（品牌+通用名）' },
        { name: 'limit', type: 'int', default: 10, help: '返回结果数量 (max 60)' },
        { name: 'page', type: 'int', default: 1, help: '页码' },
    ],
    columns: ['rank', 'title', 'price', 'spec', 'shop', 'wholesale_id', 'provider_id', 'manufacturer', 'unit', 'stock', 'url'],
    navigateBefore: false,
    func: async (page, kwargs) => {
        const query = String(kwargs.query || '').trim();
        if (!query) {
            throw new ArgumentError('药师帮搜索关键词不能为空');
        }
        const limit = Math.min(Math.max(Number(kwargs.limit) || 10, 1), 60);
        const pageNum = Math.max(Number(kwargs.page) || 1, 1);

        const searchUrl = `https://dian.ysbang.cn/#/indexContent?searchkey=${encodeURIComponent(query)}&page=${pageNum}&pagesize=${limit}&firstSearch=true`;
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

        return extractSearchResults(data, limit);
    },
});