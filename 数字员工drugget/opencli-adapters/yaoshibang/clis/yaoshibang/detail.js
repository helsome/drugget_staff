import { cli, Strategy } from '@jackwener/opencli/registry';
import { ArgumentError, CommandExecutionError, EmptyResultError } from '@jackwener/opencli/errors';

/**
 * 药师帮 商品详情
 *
 * 通过 wholesaleId 和 providerId 获取商品详细信息。
 * 数据从 Vuex store 中提取，包含价格、规格、SKU、店铺、库存等。
 */

// 拦截弹窗标记：当详情页被供应商级拦截（连锁总部选择、采购活动ID缺失等）
// 时页面不加载商品数据，detail 轮询会误判为 EMPTY_RESULT。这里显式识别，
// 让 collector 映射成 PAGE_CHANGED（人工介入）而非 PARSE_ERROR。
export const BLOCKED_MARKERS = [
    '请选择要下单的连锁总部',
    '采购活动ID不能为空',
    '连锁总部',
];
export const HOMEPAGE_TITLE = '药师帮-海量品种、充足库存，好药一步到终端';

/**
 * 纯函数：根据页面状态判断是否被拦截弹窗挡住。
 * 便于单元测试；运行时由 detectBlockingModal(page) 调用。
 */
export function isBlockingModalState({ title, bodyText, modalVisible }) {
    const text = String(bodyText || '');
    const markers = BLOCKED_MARKERS.filter(m => text.includes(m));
    if (markers.length > 0) {
        return { blocked: true, title, markers };
    }
    // 标题是首页标题 + 弹窗可见 = 详情页未渲染（仍停在首页）
    if (title === HOMEPAGE_TITLE && modalVisible) {
        return { blocked: true, title, markers: ['homepage_with_modal'] };
    }
    return { blocked: false };
}

async function detectBlockingModal(page) {
    const state = await page.evaluate(`
        (() => {
            const title = document.title || '';
            const bodyText = document.body ? document.body.innerText : '';
            const modalVisible = !!document.querySelector(
                '.el-message-box, .van-dialog, .van-overlay, .mint-msgbox, .el-dialog'
            );
            return { title, bodyText, modalVisible };
        })()
    `);
    return isBlockingModalState(state);
}

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
        { name: 'purchase_activity_id', required: false, help: '采购活动ID（来自搜索结果）' },
        { name: 'search_query', required: false, help: '原始搜索词；用于从搜索结果上下文核验商品' },
    ],
    columns: ['field', 'value'],
    navigateBefore: false,
    func: async (page, kwargs) => {
        const wholesaleId = String(kwargs.wholesale_id || '').trim();
        const providerId = String(kwargs.provider_id || '').trim();
        const purchaseActivityId = String(kwargs.purchase_activity_id || '').trim();
        const searchQuery = String(kwargs.search_query || '').trim();

        if (!wholesaleId) throw new ArgumentError('wholesale_id 不能为空');
        if (!providerId) throw new ArgumentError('provider_id 不能为空');

        // 药师帮当前 Web 版的商品卡片已经包含详情核验所需字段；直接拼接
        // /druginfo URL 会绕过采购上下文初始化并触发“采购活动ID不能为空”。
        // 有原始搜索词时，从搜索页按 wholesaleId + providerId 精确定位商品，
        // 这是与人工页面操作一致的稳定读取路径。
        if (searchQuery) {
            const searchUrl = `https://dian.ysbang.cn/#/indexContent?searchkey=${encodeURIComponent(searchQuery)}&page=1&pagesize=60&firstSearch=true`;
            await page.goto(searchUrl);
            await page.wait(6);
            const matched = await page.evaluate(`
                (async () => {
                    const wholesaleId = ${JSON.stringify(wholesaleId)};
                    const providerId = ${JSON.stringify(providerId)};
                    for (let i = 0; i < 15; i++) {
                        const app = document.querySelector('#app');
                        const store = app && app.__vue__ && app.__vue__.$store;
                        const dl = store && store.state && store.state.drugList;
                        const list = dl && dl.drugList;
                        if (Array.isArray(list)) {
                            const info = list.find(item =>
                                String(item.wholesaleid) === wholesaleId &&
                                String(item.providerId || item.provider_id || '') === providerId
                            );
                            if (info) return {
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
                        await new Promise(r => setTimeout(r, 1000));
                    }
                    return null;
                })()
            `);
            if (!matched) {
                throw new EmptyResultError('yaoshibang', `搜索上下文未找到商品 wholesaleId=${wholesaleId}, providerId=${providerId}`);
            }
            const fields = extractDetailFields(matched);
            fields['采集来源'] = 'search_context_verified';
            return Object.entries(fields).map(([field, value]) => ({ field, value }));
        }

        const params = new URLSearchParams({ wholesaleId, providerId });
        if (purchaseActivityId) params.set('purchaseActivityId', purchaseActivityId);
        const detailUrl = `https://dian.ysbang.cn/#/druginfo?${params.toString()}`;
        await page.goto(detailUrl);
        await page.wait(5);

        // 立即检测拦截弹窗：部分供应商会触发"请选择连锁总部"等弹窗，
        // 导致详情页完全不渲染。提前抛出阻断错误避免 15 秒空轮询。
        const earlyBlock = await detectBlockingModal(page);
        if (earlyBlock.blocked) {
            throw new CommandExecutionError(
                `yaoshibang detail 阻断弹窗: ${earlyBlock.markers.join(',')}`,
                '供应商页面被拦截，需人工介入'
            );
        }

        // 轮询 Vuex drugList，最多等待 15 秒；中途每 3 秒复检拦截弹窗。
        const drugInfo = await page.evaluate(`
            (async () => {
                const maxWait = 15;
                const wholesaleId = ${JSON.stringify(wholesaleId)};
                for (let i = 0; i < maxWait; i++) {
                    const app = document.querySelector('#app');
                    if (app && app.__vue__) {
                        const store = app.__vue__.$store;
                        if (store && store.state && store.state.drugList) {
                            const dl = store.state.drugList;
                            // 详情页数据可能在 drugDetail 或直接从 drugList 中查找
                            let info = null;

                            // 方式1: 从 drugDetail 获取
                            if (dl.drugDetail && dl.drugDetail.wholesaleid == wholesaleId) {
                                info = dl.drugDetail;
                            }

                            // 方式2: 从 drugList 中查找
                            if (!info && Array.isArray(dl.drugList)) {
                                info = dl.drugList.find(item =>
                                    String(item.wholesaleid) === wholesaleId
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
                                    _blockedAt: null,
                                };
                            }
                        }
                    }
                    await new Promise(r => setTimeout(r, 1000));
                }
                // 最后一次复检拦截弹窗：弹窗可能在轮询中途出现。
                const bodyText = document.body ? document.body.innerText : '';
                const title = document.title || '';
                const modalVisible = !!document.querySelector(
                    '.el-message-box, .van-dialog, .van-overlay, .mint-msgbox, .el-dialog'
                );
                return { _notFound: true, title, bodyText, modalVisible };
            })()
        `);

        // 轮询结束后若仍未找到商品，再判定是拦截弹窗还是真正的 EMPTY_RESULT。
        if (!drugInfo || drugInfo._notFound) {
            const finalState = drugInfo && drugInfo._notFound
                ? isBlockingModalState({
                    title: drugInfo.title,
                    bodyText: drugInfo.bodyText,
                    modalVisible: drugInfo.modalVisible,
                })
                : await detectBlockingModal(page);
            if (finalState.blocked) {
                throw new CommandExecutionError(
                    `yaoshibang detail 阻断弹窗: ${finalState.markers.join(',')}`,
                    '供应商页面被拦截，需人工介入'
                );
            }
            throw new EmptyResultError('yaoshibang', `未找到商品 wholesaleId=${wholesaleId}`);
        }

        const fields = extractDetailFields(drugInfo);
        // 返回 key-value 数组格式，与 jd/taobao detail 一致
        return Object.entries(fields).map(([field, value]) => ({ field, value }));
    },
});
