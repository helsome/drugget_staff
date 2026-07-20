import { describe, expect, it } from 'vitest';
import { getRegistry } from '@jackwener/opencli/registry';
import './shop-search.js';

describe('taobao shop-search registration', () => {
  it('registers a read-only persistent storefront-search command', () => {
    const command = getRegistry().get('taobao/shop-search');
    expect(command).toBeDefined();
    expect(command.access).toBe('read');
    expect(command.siteSession).toBe('persistent');
    expect(command.columns).toContain('query_verified');
  });
});
