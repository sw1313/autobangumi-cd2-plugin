<script lang="ts" setup>
import type { CloudDrive2 } from '@/extensions/cd2/types';
import type { SettingItem } from '#/components';
import { cd2Api } from '@/extensions/cd2/api';
const { t } = useMyI18n();
const { getSettingGroup } = useConfigStore();

const cd2 = getSettingGroup('cd2');
const testing = ref(false);
const testResult = ref<{ success: boolean; message: string } | null>(null);

const items: SettingItem<CloudDrive2>[] = [
  {
    configKey: 'enable',
    label: () => t('config.cd2_set.enable'),
    type: 'switch',
  },
  {
    configKey: 'host',
    label: () => t('config.cd2_set.host'),
    type: 'input',
    prop: {
      type: 'text',
      placeholder: 'http://clouddrive.local:19798',
    },
  },
  {
    configKey: 'username',
    label: () => t('config.cd2_set.username'),
    type: 'input',
    prop: {
      type: 'text',
      placeholder: 'admin',
    },
  },
  {
    configKey: 'password',
    label: () => t('config.cd2_set.password'),
    type: 'input',
    prop: {
      type: 'text',
      placeholder: 'password',
    },
    bottomLine: true,
  },
  {
    configKey: 'offline_dir',
    label: () => t('config.cd2_set.offline_dir'),
    type: 'input',
    prop: {
      type: 'text',
      placeholder: '/115/动漫/cd2-offline',
    },
  },
  {
    configKey: 'local_path',
    label: () => t('config.cd2_set.local_path'),
    type: 'input',
    prop: {
      type: 'text',
      placeholder: '/cd2-offline',
    },
    bottomLine: true,
  },
  {
    configKey: 'stall_time',
    label: () => t('config.cd2_set.stall_time'),
    type: 'input',
    prop: {
      type: 'number',
      placeholder: '60',
    },
  },
  {
    configKey: 'stall_min_speed',
    label: () => t('config.cd2_set.stall_min_speed'),
    type: 'input',
    prop: {
      type: 'number',
      placeholder: '1',
    },
  },
  {
    configKey: 'scan_interval',
    label: () => t('config.cd2_set.scan_interval'),
    type: 'input',
    prop: {
      type: 'number',
      placeholder: '300',
    },
  },
  {
    configKey: 'pause_qb_torrent',
    label: () => t('config.cd2_set.pause_qb_torrent'),
    type: 'switch',
  },
  {
    configKey: 'resume_after_recheck',
    label: () => t('config.cd2_set.resume_after_recheck'),
    type: 'switch',
  },
];

async function handleTest() {
  const cfg = cd2.value;
  if (!cfg.host?.trim()) {
    useMessage().error(
      useMyI18n().returnUserLangText({
        en: 'Please enter CD2 address.',
        'zh-CN': '请填写 CD2 地址。',
      })
    );
    return;
  }
  if (!cfg.username?.trim()) {
    useMessage().error(
      useMyI18n().returnUserLangText({
        en: 'Please enter CD2 username.',
        'zh-CN': '请填写 CD2 用户名。',
      })
    );
    return;
  }

  testing.value = true;
  testResult.value = null;
  try {
    const res = await cd2Api.testConnection({
      host: cfg.host.trim(),
      username: cfg.username.trim(),
      password: cfg.password?.trim() || '********',
    });
    testResult.value = {
      success: res.success,
      message: res.msg_zh || res.msg_en,
    };
    if (res.success) {
      useMessage().success(res.msg_zh || res.msg_en);
    } else {
      useMessage().error(res.msg_zh || res.msg_en);
    }
  } catch (e: unknown) {
    const err = e as { msg_zh?: string; msg_en?: string };
    const message =
      err.msg_zh || err.msg_en || useMyI18n().t('config.cd2_set.test_failed');
    testResult.value = { success: false, message };
    useMessage().error(message);
  } finally {
    testing.value = false;
  }
}
</script>

<template>
  <ab-fold-panel :title="$t('config.cd2_set.title')">
    <p class="cd2-desc">{{ $t('config.cd2_set.desc') }}</p>
    <div space-y-8>
      <ab-setting
        v-for="i in items"
        :key="i.configKey"
        v-bind="i"
        v-model:data="cd2[i.configKey]"
      ></ab-setting>
      <ab-button type="secondary" :loading="testing" @click="handleTest">
        {{ $t('config.cd2_set.test') }}
      </ab-button>
      <p
        v-if="testResult"
        class="test-result"
        :class="testResult.success ? 'test-success' : 'test-error'"
      >
        {{ testResult.message }}
      </p>
    </div>
  </ab-fold-panel>
</template>

<style scoped>
.cd2-desc {
  margin-bottom: 12px;
  color: var(--color-text-secondary);
  font-size: 13px;
  line-height: 1.5;
}

.test-result {
  margin-top: 8px;
  font-size: 13px;
  line-height: 1.5;
}

.test-success {
  color: var(--color-success, #18a058);
}

.test-error {
  color: var(--color-error, #d03050);
}
</style>
