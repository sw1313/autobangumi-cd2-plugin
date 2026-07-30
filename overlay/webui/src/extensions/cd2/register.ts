import type { Component } from 'vue';
import type { Config } from '#/config';
import ConfigCd2 from './config-cd2.vue';
import en from './i18n/en.json';
import zhCN from './i18n/zh-CN.json';

export interface LocalExtensionsRegistry {
  configSections: Array<{
    id: string;
    titleKey: string;
    component: Component;
    groups: Array<keyof Config>;
    keywords: string[];
  }>;
  i18n: Record<string, Record<string, unknown>>;
}

export function registerCd2Extension(ext: LocalExtensionsRegistry) {
  ext.configSections.push({
    id: 'cd2',
    titleKey: 'config.cd2_set.title',
    component: ConfigCd2,
    groups: ['cd2'],
    keywords: ['cd2', 'clouddrive', 'offline', '115', 'stall', 'cloud'],
  });

  ext.i18n['zh-CN'] = { ...(ext.i18n['zh-CN'] ?? {}), ...zhCN };
  ext.i18n.en = { ...(ext.i18n.en ?? {}), ...en };
}
