import { cli, Strategy } from '@jackwener/opencli/registry';
import { ArgumentError } from '@jackwener/opencli/errors';

/**
 * 药师帮 店铺信息
 *
 * 获取供应商（店铺）的基本信息。
 * 药师帮的店铺/供应商信息从 Vuex store 中获取。
 */

cli({
    site: 'yaoshibang',
    name: 'shop',
    access: 'read',
    description: '药师帮店铺信息',
    domain: 'dian.ysbang.cn',
    strategy: Strategy.COOKIE,
    args: [
        { name: 'provider_id', positional: true, required: true, help: '供应商ID' },
    ],
    columns: ['shop_name', 'provider_id', 'shop_url', 'platform'],
    navigateBefore: false,
    func: async (page, kwargs) => {
        const providerId = String(kwargs.provider_id || '').trim();
        if (!providerId) {
            throw new ArgumentError('provider_id 不能为空');
        }

        // 通过搜索一个商品来进入系统，然后从 store 中获取供应商信息
        // 也可直接访问供应商页面
        const shopUrl = `https://dian.ysbang.cn/#/provider?providerId=${providerId}`;
        await page.goto(shopUrl);
        await page.wait(5);

        const shopInfo = await page.evaluate(`
            (async () => {
                const maxWait = 10;
                for (let i = 0; i < maxWait; i++) {
                    const app = document.querySelector('#app');
                    if (app && app.__vue__) {
                        const store = app.__vue__.$store;
                        if (store && store.state) {
                            // 从 drugList store 中获取供应商信息
                            const dl = store.state.drugList;
                            if (dl) {
                                // 从供应商筛选列表中查找
                                const providerList = dl.providerFilterList || [];
                                const provider = providerList.find(p =>
                                    String(p.provider_id || p.providerId) === ${JSON.stringify(providerId)}
                                );
                                if (provider) {
                                    return {
                                        shop_name: provider.name || provider.provider_name || '',
                                        provider_id: String(provider.provider_id || provider.providerId || ''),
                                    };
                                }
                            }
                        }
                    }
                    await new Promise(r => setTimeout(r, 1000));
                }
                return null;
            })()
        `);

        return [{
            shop_name: shopInfo?.shop_name || '',
            provider_id: providerId,
            shop_url: `https://dian.ysbang.cn/#/provider?providerId=${providerId}`,
            platform: 'yaoshibang',
        }];
    },
});