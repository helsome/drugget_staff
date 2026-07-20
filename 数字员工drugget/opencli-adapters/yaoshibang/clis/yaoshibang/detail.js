import { cli, Strategy } from '@jackwener/opencli/registry';
import { ArgumentError, EmptyResultError } from '@jackwener/opencli/errors';

/**
 * 药师帮 商品详情
 *
 * 通过 wholesaleId 和 providerId 获取商品详细信息。
 * 数据从 Vuex store 中提取，包含价格、规格、SKU、店铺、库存等。
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

function extractDetailFields(drugInfo) {
  if (!drugInfo) return {};

  const joinCar = drugInfo.joinCarMap || {};
  const price = parsePriceFromToken(drugInfo.priceToken);

  return {
    '商品名称': drugInfo.drugname || drugInfo.cn_name || '',
    '价格': price !== null ? price.toFixed(2) : '',
    '规格': drugInfo.specification || '',
    '商品ID': String(drugInfo.wholesaleid || ''),
    '供应商ID': String(drugInfo.providerId || drugInfo.provider_id || ''),
    '供应商名称': drugInfo.provider_name || '',
    '生产厂家': drugInfo.manufacturer || '',
    '单位': drugInfo.unit || joinCar.unit || '',
    '库存': joinCar.stockDisplay || joinCar.stockStatus || '',
    '起购数量': String(joinCar.drugMinAmount || drugInfo.minamount || ''),
    '有效期': joinCar.validDate || drugInfo.valid_date || '',
    '链接': `https://dian.ysbang.cn/#/druginfo?wholesaleId=${drugInfo.wholesaleid}&providerId=${drugInfo.providerId || drugInfo.provider_id}`,
  };
}

cli({
    site: 'yaoshibang',
    name: 'detail',
    access: 'read',
    description: '药师帮商品详情',
    domain: 'dian.ysbang.cn',
    strategy: Strategy.COOKIE,
    args: [
        { name: 'wholesale_id', positional: true, required: true, help: '商品批发ID' },
        { name: 'provider_id', required: true, help: '供应商ID' },
    ],
    columns: ['field', 'value'],
    navigateBefore: false,
    func: async (page, kwargs) => {
        const wholesaleId = String(kwargs.wholesale_id || '').trim();
        const providerId = String(kwargs.provider_id || '').trim();

        if (!wholesaleId) throw new ArgumentError('wholesale_id 不能为空');
        if (!providerId) throw new ArgumentError('provider_id 不能为空');

        const detailUrl = `https://dian.ysbang.cn/#/druginfo?wholesaleId=${wholesaleId}&providerId=${providerId}`;
        await page.goto(detailUrl);
        await page.wait(5);

        const drugInfo = await page.evaluate(`
            (async () => {
                const maxWait = 15;
                for (let i = 0; i < maxWait; i++) {
                    const app = document.querySelector('#app');
                    if (app && app.__vue__) {
                        const store = app.__vue__.$store;
                        if (store && store.state && store.state.drugList) {
                            const dl = store.state.drugList;
                            // 详情页数据可能在 drugDetail 或直接从 drugList 中查找
                            let info = null;

                            // 方式1: 从 drugDetail 获取
                            if (dl.drugDetail && dl.drugDetail.wholesaleid == ${JSON.stringify(wholesaleId)}) {
                                info = dl.drugDetail;
                            }

                            // 方式2: 从 drugList 中查找
                            if (!info && Array.isArray(dl.drugList)) {
                                info = dl.drugList.find(item =>
                                    String(item.wholesaleid) === ${JSON.stringify(wholesaleId)}
                                );
                            }

                            if (info) {
                                return {
                                    wholesaleid: info.wholesaleid,
                                    drugname: info.drugname,
                                    cn_name: info.cn_name,
                                    priceToken: info.priceToken,
                                    specification: info.specification,
                                    provider_name: info.provider_name,
                                    providerId: info.providerId || info.provider_id,
                                    manufacturer: info.manufacturer,
                                    unit: info.unit || (info.joinCarMap ? info.joinCarMap.unit : ''),
                                    minamount: info.minamount,
                                    valid_date: info.valid_date,
                                    joinCarMap: info.joinCarMap ? {
                                        stockDisplay: info.joinCarMap.stockDisplay,
                                        stockStatus: info.joinCarMap.stockStatus,
                                        validDate: info.joinCarMap.validDate,
                                        unit: info.joinCarMap.unit,
                                        drugMinAmount: info.joinCarMap.drugMinAmount,
                                        limitMessage: info.joinCarMap.limitMessage,
                                        wholesaleId: info.joinCarMap.wholesaleId,
                                    } : null,
                                };
                            }
                        }
                    }
                    await new Promise(r => setTimeout(r, 1000));
                }
                return null;
            })()
        `);

        if (!drugInfo) {
            throw new EmptyResultError('yaoshibang', `未找到商品 wholesaleId=${wholesaleId}`);
        }

        const fields = extractDetailFields(drugInfo);
        // 返回 key-value 数组格式，与 jd/taobao detail 一致
        return Object.entries(fields).map(([field, value]) => ({ field, value }));
    },
});