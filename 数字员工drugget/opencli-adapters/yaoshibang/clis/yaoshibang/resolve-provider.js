import { cli, Strategy } from '@jackwener/opencli/registry';
import { ArgumentError } from '@jackwener/opencli/errors';

/** Resolve a supplier ID from a human-maintained 药师帮 store name.
 *
 * A provider ID is only emitted with the search evidence that exposed it. The
 * caller must accept an exact unique match; fuzzy rows are candidates for a
 * human to review, never an automatic store mapping.
 */

function normalized(value) {
    return String(value || '').replace(/[\s·()（）【】\[\]-]/g, '').toLowerCase();
}

function confidence(target, name) {
    const wanted = normalized(target);
    const found = normalized(name);
    if (!wanted || !found) return 'none';
    if (wanted === found) return 'exact';
    if (found.includes(wanted) || wanted.includes(found)) return 'partial';
    return 'none';
}

cli({
    site: 'yaoshibang',
    name: 'resolve-provider',
    access: 'read',
    description: '按药师帮店铺名解析供应商 provider_id，并返回可审计的匹配证据',
    domain: 'dian.ysbang.cn',
    strategy: Strategy.COOKIE,
    browser: true,
    args: [
        { name: 'shop_name', positional: true, required: true, help: '责任档案中的药师帮店铺名称' },
        { name: 'limit', type: 'int', default: 20, help: '最多返回候选数 (max 60)' },
    ],
    columns: ['shop_name', 'provider_id', 'match_confidence', 'evidence', 'shop_url', 'platform'],
    navigateBefore: false,
    siteSession: 'persistent',
    defaultWindowMode: 'foreground',
    func: async (page, kwargs) => {
        const shopName = String(kwargs.shop_name || '').trim();
        if (!shopName) throw new ArgumentError('shop_name 不能为空');
        const limit = Math.min(Math.max(Number(kwargs.limit) || 20, 1), 60);
        const url = `https://dian.ysbang.cn/#/indexContent?searchkey=${encodeURIComponent(shopName)}&page=1&pagesize=${limit}&firstSearch=true`;
        await page.goto(url);
        await page.wait(6);
        const providers = await page.evaluate(`
            (async () => {
                for (let i = 0; i < 15; i++) {
                    const app = document.querySelector('#app');
                    const dl = app && app.__vue__ && app.__vue__.$store && app.__vue__.$store.state.drugList;
                    if (dl) {
                        const result = [];
                        for (const provider of (dl.providerFilterList || [])) {
                            result.push({ name: provider.name || provider.provider_name || '', id: provider.provider_id || provider.providerId || '' });
                        }
                        for (const item of (dl.drugList || [])) {
                            result.push({ name: item.provider_name || '', id: item.providerId || item.provider_id || '' });
                        }
                        if (result.length) return result;
                    }
                    await new Promise(resolve => setTimeout(resolve, 1000));
                }
                return [];
            })()
        `);
        const deduped = new Map();
        for (const provider of (providers || [])) {
            const providerId = String(provider.id || '').trim();
            const name = String(provider.name || '').trim();
            if (!providerId || !name) continue;
            const level = confidence(shopName, name);
            if (level === 'none') continue;
            const key = `${providerId}:${normalized(name)}`;
            if (!deduped.has(key)) deduped.set(key, { name, providerId, level });
        }
        const order = { exact: 0, partial: 1 };
        return [...deduped.values()]
            .sort((a, b) => order[a.level] - order[b.level] || a.name.localeCompare(b.name, 'zh-CN'))
            .map(item => ({
                shop_name: item.name,
                provider_id: item.providerId,
                match_confidence: item.level,
                evidence: `store_search:${shopName}; matched_provider:${item.name}`,
                shop_url: `https://dian.ysbang.cn/#/provider?providerId=${item.providerId}`,
                platform: 'yaoshibang',
            }));
    },
});
