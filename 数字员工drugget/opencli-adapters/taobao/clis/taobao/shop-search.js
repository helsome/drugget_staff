import { cli, Strategy } from '@jackwener/opencli/registry';
import { ArgumentError } from '@jackwener/opencli/errors';

const normalize = (value) => String(value || '').replace(/\s+/g, ' ').trim();

function isTaobaoShopHome(value) {
  try {
    const url = new URL(value);
    return /^shop\d+\.(taobao|tmall)\.com$/i.test(url.hostname);
  } catch { return false; }
}

/**
 * Real storefront interaction for legacy Taobao shop pages.
 * It intentionally uses CDP native pointer/keyboard operations instead of
 * assigning input.value or DOM el.click(), because those bypass the page's
 * event listeners and can submit an empty global-search query.
 */
cli({
  site: 'taobao',
  name: 'shop-search',
  access: 'read',
  description: '在已验证淘宝店铺主页中真实输入并搜索商品；不回退到全站搜索',
  domain: 'taobao.com',
  strategy: Strategy.COOKIE,
  browser: true,
  navigateBefore: false,
  siteSession: 'persistent',
  defaultWindowMode: 'foreground',
  args: [
    { name: 'query', positional: true, required: true, help: '店内搜索词，必须为品牌名+通用名' },
    { name: 'shop_home_url', required: true, help: '已验证的真实店铺主页（shop数字.taobao.com 或 tmall.com）' },
    { name: 'expected_shop_name', required: true, help: '责任档案中的店铺名称；页面必须包含该名称' },
    { name: 'limit', type: 'int', default: 20, help: '最多返回候选数（max 40）' },
  ],
  columns: ['status', 'reason', 'query', 'query_verified', 'current_url', 'shop_name', 'title', 'item_id', 'url', 'rank'],
  func: async (page, kwargs) => {
    const query = String(kwargs.query || '').trim();
    const shopHome = String(kwargs.shop_home_url || '').trim();
    const expectedShopName = String(kwargs.expected_shop_name || '').trim();
    const limit = Math.min(Math.max(Number(kwargs.limit) || 20, 1), 40);
    if (!query) throw new ArgumentError('query 不能为空');
    if (!isTaobaoShopHome(shopHome)) throw new ArgumentError('shop_home_url 必须是已验证的 shop数字.taobao.com 或 shop数字.tmall.com 主页');
    if (!expectedShopName) throw new ArgumentError('expected_shop_name 不能为空');

    await page.goto(shopHome, { settleMs: 1500 });
    await page.wait(2);
    const initial = await page.evaluate(`(() => {
      const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
      const input = document.querySelector('input[placeholder="搜索本店商品"]');
      const button = document.querySelector('.search-local');
      const shop = normalize(document.title || document.querySelector('#mallLogo a')?.getAttribute('title') || '');
      const rect = (node) => { const r=node?.getBoundingClientRect(); return r && r.width > 0 && r.height > 0 ? {x:r.left+r.width/2,y:r.top+r.height/2} : null; };
      return { url: location.href, shop, body: document.body?.innerText || '', input: rect(input), button: rect(button) };
    })()`);
    if (!String(initial?.body || '').includes(expectedShopName) && !String(initial?.shop || '').includes(expectedShopName)) {
      return [{ status: 'not_found', reason: 'store_identity_mismatch', query, query_verified: false,
        current_url: initial?.url || '', shop_name: initial?.shop || '', title: '', item_id: '', url: '', rank: '' }];
    }
    if (!initial?.input || !initial?.button) {
      return [{ status: 'not_found', reason: 'store_search_controls_not_found', query, query_verified: false,
        current_url: initial?.url || '', shop_name: expectedShopName, title: '', item_id: '', url: '', rank: '' }];
    }

    await page.nativeClick(Math.round(initial.input.x), Math.round(initial.input.y));
    await page.nativeKeyPress('A', ['Meta']);
    await page.nativeKeyPress('Backspace');
    await page.nativeType(query);
    await page.wait(0.4);
    const typed = await page.evaluate(`(() => document.querySelector('input[placeholder="搜索本店商品"]')?.value || '')()`);
    if (typed !== query) {
      return [{ status: 'not_found', reason: 'native_input_not_retained', query, query_verified: false,
        current_url: await page.evaluate('location.href'), shop_name: expectedShopName, title: '', item_id: '', url: '', rank: '' }];
    }

    await page.nativeClick(Math.round(initial.button.x), Math.round(initial.button.y));
    await page.wait(3);
    const result = await page.evaluate(`(async () => {
      const normalize = value => String(value || '').replace(/\\s+/g, ' ').trim();
      const wanted = ${JSON.stringify(query)};
      const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
      for (let i=0; i<12; i++) {
        const local = document.querySelector('input[placeholder="搜索本店商品"]')?.value || '';
        const links = Array.from(document.querySelectorAll('a[href*="item.htm"]'));
        if (local === wanted && links.length) break;
        await sleep(500);
      }
      const local = document.querySelector('input[placeholder="搜索本店商品"]')?.value || '';
      const shop = normalize(document.title || document.querySelector('#mallLogo a')?.getAttribute('title') || '');
      const body = document.body?.innerText || '';
      const cards = Array.from(document.querySelectorAll('[class*="cardContainer--"]')).map(card => {
        const fiberKey = Object.getOwnPropertyNames(card).find(key => key.startsWith('__reactFiber'));
        let fiber = fiberKey ? card[fiberKey] : null;
        while (fiber && !(fiber.memoizedProps || {}).itemCardData) fiber = fiber.return;
        const item = fiber?.memoizedProps?.itemCardData;
        const title = normalize(item?.title || card.querySelector('[class*="title--"]')?.textContent || '');
        const itemId = String(item?.itemId || '');
        const url = String(item?.itemUrl || '');
        return { title, item_id: itemId, url };
      }).filter(item => item.title && /^\\d+$/.test(item.item_id) && /detail\\.(taobao|tmall)\\.com/.test(item.url)).slice(0, ${limit});
      return { url: location.href, shop, local, body, cards };
    })()`);
    const queryVerified = result?.local === query || String(result?.body || '').includes(`当前搜索: ${query}`);
    const currentUrl = String(result?.url || '');
    const escapedToGlobal = /(^|\.)s\.taobao\.com$/i.test(new URL(currentUrl || shopHome).hostname);
    if (!queryVerified || escapedToGlobal) {
      return [{ status: 'not_found', reason: escapedToGlobal ? 'store_search_navigation_lost' : 'store_query_not_effective',
        query, query_verified: false, current_url: currentUrl, shop_name: expectedShopName, title: '', item_id: '', url: '', rank: '' }];
    }
    const terms = query.split(/\s+/).filter(Boolean);
    const candidates = (result?.cards || []).filter(card => terms.every(term => card.title.includes(term)));
    if (!candidates.length) {
      return [{ status: 'not_found', reason: 'store_no_results', query, query_verified: true,
        current_url: currentUrl, shop_name: expectedShopName, title: '', item_id: '', url: '', rank: '' }];
    }
    const selected = candidates[0];
    return [{ status: 'success', reason: '', query, query_verified: true, current_url: currentUrl,
      shop_name: expectedShopName, title: selected.title, item_id: selected.item_id,
      url: selected.url, rank: 1 }];
  },
});
