import { describe, expect, it } from 'vitest';
import { getRegistry } from '@jackwener/opencli/registry';

// 导入所有命令以触发 cli() 注册
import './auth.js';
import './search.js';
import './detail.js';
import './shop.js';
import './resolve-provider.js';

describe('yaoshibang command registration', () => {
    it('registers all yaoshibang commands', () => {
        for (const name of ['search', 'detail', 'shop', 'resolve-provider']) {
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
            'wholesale_id', 'provider_id', 'manufacturer',
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

    it('all commands use COOKIE strategy', () => {
        for (const name of ['search', 'detail', 'shop', 'resolve-provider']) {
            const cmd = getRegistry().get(`yaoshibang/${name}`);
            expect(cmd.strategy).toBe('cookie');
        }
    });
});
