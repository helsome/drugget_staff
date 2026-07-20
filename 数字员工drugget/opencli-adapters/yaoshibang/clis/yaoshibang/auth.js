import { cli, Strategy } from '@jackwener/opencli/registry';
import { AuthRequiredError, CommandExecutionError } from '@jackwener/opencli/errors';

/**
 * 药师帮 登录认证
 *
 * 药师帮 (dian.ysbang.cn) 是 B2B 药品采购平台。
 * 登录态由 HttpOnly cookie 维护，通过检查 Vuex store 中是否显示用户名来判断登录状态。
 */

async function hasYsbangSessionCookie(page) {
  try {
    // 通过页面内容判断登录状态，而不是依赖 cookies API
    await page.goto('https://dian.ysbang.cn/#/home');
    await page.wait(4);
    const loggedIn = await page.evaluate(`
      (() => {
        try {
          const app = document.querySelector('#app');
          if (!app || !app.__vue__) return false;
          const state = app.__vue__.$store.state;
          const user = state.user || {};
          const profile = user.userInfo || user.userBriefInfo || user;
          return !!(profile && (profile.userName || profile.userId || profile.uname));
        } catch(e) { return false; }
      })()
    `);
    return loggedIn;
  } catch {
    return false;
  }
}

async function verifyYsbangIdentity(page) {
  if (!await hasYsbangSessionCookie(page)) {
    throw new AuthRequiredError('dian.ysbang.cn', '药师帮 session cookies 缺失');
  }
  await page.goto('https://dian.ysbang.cn/#/home');
  await page.wait(5);

  const probe = await page.evaluate(`
    (() => {
      try {
        const app = document.querySelector('#app');
        if (!app || !app.__vue__) {
          return { kind: 'auth', detail: '药师帮页面未加载完成' };
        }
        const state = app.__vue__.$store.state;
        const user = state.user || {};
        const profile = user.userInfo || user.userBriefInfo || user;
        if (!profile || !(profile.userName || profile.userId || profile.uname)) {
          return { kind: 'auth', detail: '未检测到登录用户信息' };
        }
        // Merchant/account fields differ by identity type. Keep these separate
        // from provider_id: a logged-in buyer account is not a supplier ID.
        return {
          ok: true,
          user_id: String(profile.userId || profile.id || ''),
          name: String(profile.userName || profile.uname || profile.name || ''),
          merchant_id: String(profile.merchantId || profile.providerId || profile.provider_id || profile.shopId || ''),
          merchant_name: String(profile.merchantName || profile.providerName || profile.provider_name || profile.shopName || profile.storeFullName || ''),
          identity_type: String(profile.userType || profile.role || profile.storeType || ''),
        };
      } catch(e) {
        return { kind: 'auth', detail: 'check error: ' + e.message };
      }
    })()
  `);

  if (probe?.kind === 'auth') throw new AuthRequiredError('dian.ysbang.cn', probe.detail);
  if (!probe?.ok) throw new CommandExecutionError(`Unexpected 药师帮 probe: ${JSON.stringify(probe)}`);

  return {
    user_id: probe.user_id,
    name: probe.name,
    merchant_id: probe.merchant_id,
    merchant_name: probe.merchant_name,
    identity_type: probe.identity_type,
  };
}

// ── whoami ──
cli({
    site: 'yaoshibang',
    name: 'whoami',
    access: 'read',
    description: '查看药师帮当前登录身份',
    domain: 'dian.ysbang.cn',
    strategy: Strategy.COOKIE,
    browser: true,
    columns: ['user_id', 'name', 'merchant_id', 'merchant_name', 'identity_type', 'logged_in'],
    navigateBefore: false,
    siteSession: 'persistent',
    defaultWindowMode: 'foreground',
    func: async (page) => {
        try {
            const identity = await verifyYsbangIdentity(page);
            return { ...identity, logged_in: true };
        } catch (e) {
            if (e instanceof AuthRequiredError) {
                return { user_id: '', name: '', merchant_id: '', merchant_name: '', identity_type: '', logged_in: false };
            }
            throw e;
        }
    },
});

// ── login ──
cli({
    site: 'yaoshibang',
    name: 'login',
    access: 'write',
    description: '打开药师帮登录页面，等待用户完成登录',
    domain: 'dian.ysbang.cn',
    strategy: Strategy.COOKIE,
    browser: true,
    args: [
        { name: 'timeout', type: 'int', default: 300, help: '最大等待秒数' },
    ],
    columns: ['user_id', 'name', 'merchant_id', 'merchant_name', 'identity_type', 'logged_in'],
    navigateBefore: false,
    siteSession: 'persistent',
    defaultWindowMode: 'foreground',
    func: async (page, kwargs) => {
        const timeout = Number(kwargs.timeout) || 300;
        await page.goto('https://dian.ysbang.cn/#/login');
        await page.wait(2);

        const start = Date.now();
        while ((Date.now() - start) / 1000 < timeout) {
            if (!await hasYsbangSessionCookie(page)) {
                await page.wait(3);
                continue;
            }
            try {
                const identity = await verifyYsbangIdentity(page);
                return { ...identity, logged_in: true };
            } catch {
                await page.wait(3);
            }
        }
        throw new AuthRequiredError('dian.ysbang.cn', '登录超时');
    },
});
