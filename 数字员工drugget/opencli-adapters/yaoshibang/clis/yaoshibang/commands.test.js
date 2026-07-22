import { describe, expect, it } from 'vitest';
import { getRegistry } from '@jackwener/opencli/registry';

// 导入所有命令以触发 cli() 注册
import './auth.js';
import './search.js';
import './detail.js';
import './shop.js';
import './resolve-provider.js';
import './provider-search.js';
import { isBlockingModalState, BLOCKED_MARKERS, HOMEPAGE_TITLE } from './detail.js';

describe('yaoshibang command registration', () => {
    it('registers all yaoshibang commands', () => {
        for (const name of ['search', 'detail', 'shop', 'resolve-provider', 'provider-search']) {
            const cmd = getRegistry().get(`yaoshibang/${name}`);
            expect(cmd, `yaoshibang/${name} should be registered`).toBeDefined();
        }
    });

    it('registers auth commands', () => {
        const whoami = getRegistry().get('yaoshibang/whoami');
        expect(whoami).toBeDefined();
        expect(whoami.access).toBe('read');

        const login = getRegistry().get('yaoshibang/login');
        expect(login).toBeDefined();
        expect(login.access).toBe('write');
    });

    it('search command has correct columns', () => {
        const cmd = getRegistry().get('yaoshibang/search');
        expect(cmd).toBeDefined();
        expect(cmd.columns).toEqual([
            'rank', 'title', 'price', 'spec', 'shop',
            'wholesale_id', 'provider_id', 'purchase_activity_id', 'manufacturer',
            'unit', 'stock', 'url',
        ]);
    });

    it('detail command has correct columns', () => {
        const cmd = getRegistry().get('yaoshibang/detail');
        expect(cmd).toBeDefined();
        expect(cmd.columns).toEqual(['field', 'value']);
    });

    it('shop command has correct columns', () => {
        const cmd = getRegistry().get('yaoshibang/shop');
        expect(cmd).toBeDefined();
        expect(cmd.columns).toEqual(['shop_name', 'provider_id', 'shop_url', 'platform']);
    });

    it('resolve-provider command has correct columns', () => {
        const cmd = getRegistry().get('yaoshibang/resolve-provider');
        expect(cmd.columns).toEqual(['shop_name', 'provider_id', 'match_confidence', 'evidence', 'shop_url', 'platform']);
    });

    it('provider-search command has correct columns', () => {
        const cmd = getRegistry().get('yaoshibang/provider-search');
        expect(cmd).toBeDefined();
        expect(cmd.columns).toEqual([
            'rank', 'title', 'price', 'spec', 'shop',
            'wholesale_id', 'provider_id', 'manufacturer',
            'unit', 'stock', 'url',
        ]);
    });

    it('all commands use COOKIE strategy', () => {
        for (const name of ['search', 'detail', 'shop', 'resolve-provider', 'provider-search']) {
            const cmd = getRegistry().get(`yaoshibang/${name}`);
            expect(cmd.strategy).toBe('cookie');
        }
    });
});

describe('isBlockingModalState', () => {
    it('detects explicit blocked markers in body text', () => {
        const result = isBlockingModalState({
            title: '药师帮',
            bodyText: '请选择要下单的连锁总部《》',
            modalVisible: false,
        });
        expect(result.blocked).toBe(true);
        expect(result.markers).toContain('请选择要下单的连锁总部');
    });

    it('detects 采购活动ID不能为空 marker', () => {
        const result = isBlockingModalState({
            title: '',
            bodyText: '采购活动ID不能为空',
            modalVisible: false,
        });
        expect(result.blocked).toBe(true);
        expect(result.markers).toContain('采购活动ID不能为空');
    });

    it('detects homepage title with visible modal as blocked', () => {
        const result = isBlockingModalState({
            title: HOMEPAGE_TITLE,
            bodyText: 'some neutral content',
            modalVisible: true,
        });
        expect(result.blocked).toBe(true);
        expect(result.markers).toEqual(['homepage_with_modal']);
    });

    it('returns not blocked for normal detail page', () => {
        const result = isBlockingModalState({
            title: '托妥 10mg*28片 - 药师帮',
            bodyText: '商品详情...',
            modalVisible: false,
        });
        expect(result.blocked).toBe(false);
    });

    it('returns not blocked when homepage title but no modal', () => {
        // 仅标题是首页标题但无弹窗，不算拦截（可能是正常加载中）
        const result = isBlockingModalState({
            title: HOMEPAGE_TITLE,
            bodyText: '',
            modalVisible: false,
        });
        expect(result.blocked).toBe(false);
    });

    it('BLOCKED_MARKERS exports expected entries', () => {
        expect(BLOCKED_MARKERS).toContain('请选择要下单的连锁总部');
        expect(BLOCKED_MARKERS).toContain('采购活动ID不能为空');
        expect(BLOCKED_MARKERS).toContain('连锁总部');
    });
});
