<script lang="ts" setup>
import type { QbTorrentInfo } from '#/downloader';
import { cd2Api } from './api';
import {
  claimRepairHashes,
  createRepairSeat,
  finishRepairHashes,
  releaseRepairHashes,
  repairOwnerId,
} from './downloader-repair-state';

defineProps<{ torrent: QbTorrentInfo }>();

const { config } = storeToRefs(useConfigStore());
const downloader = useDownloaderStore();
const { selectedHashes } = storeToRefs(downloader);
const { t } = useMyI18n();

const seat = createRepairSeat();
const pageActive = ref(false);
const barReady = ref(false);
let barObserver: MutationObserver | null = null;

const isOwner = computed(
  () => pageActive.value && repairOwnerId.value === seat && barReady.value
);

function claimOwner() {
  if (repairOwnerId.value === 0) repairOwnerId.value = seat;
}

watch(repairOwnerId, (owner) => {
  if (owner === 0 && pageActive.value) {
    claimOwner();
    if (repairOwnerId.value === seat) watchBar();
  }
});

function fillCount(key: string, values: Record<string, number>) {
  return Object.entries(values).reduce(
    (text, [name, value]) => text.replaceAll(`{${name}}`, String(value)),
    String(t(key, values))
  );
}

const { execute: sendRepair } = useApi(
  (hashes: string[]) => cd2Api.repairTorrents(hashes),
  {
    showMessage: true,
    onSuccess() {
      downloader.getAll();
    },
  }
);

function onRepair() {
  if (!config.value.cd2?.enable) {
    useMessage().warning(t('downloader.cd2_repair_disabled'));
    return;
  }
  const hashes = selectedHashes.value.slice();
  if (hashes.length === 0) return;
  const { accepted, cooled } = claimRepairHashes(hashes);
  if (accepted.length === 0) {
    useMessage().warning(t('downloader.cd2_repair_cooling'));
    return;
  }
  downloader.clearSelection();
  useMessage().success(
    cooled.length > 0
      ? fillCount('downloader.cd2_repair_sent_partial', {
          n: accepted.length,
          cooled: cooled.length,
        })
      : fillCount('downloader.cd2_repair_sent', { n: accepted.length })
  );
  void sendRepair(accepted).then((result) => {
    if (!result.ok) releaseRepairHashes(accepted);
    else finishRepairHashes(accepted);
  });
}

function syncBar() {
  barReady.value = document.querySelector('.action-bar-buttons') != null;
}

function watchBar() {
  syncBar();
  barObserver?.disconnect();
  barObserver = new MutationObserver(syncBar);
  barObserver.observe(document.body, { childList: true, subtree: true });
}

onMounted(() => {
  pageActive.value = true;
  claimOwner();
  if (repairOwnerId.value === seat) watchBar();
});

onActivated(() => {
  pageActive.value = true;
  claimOwner();
  if (repairOwnerId.value === seat) watchBar();
});

onDeactivated(() => {
  pageActive.value = false;
  if (repairOwnerId.value === seat) {
    repairOwnerId.value = 0;
    barObserver?.disconnect();
    barObserver = null;
  }
});

onUnmounted(() => {
  if (repairOwnerId.value === seat) repairOwnerId.value = 0;
  barObserver?.disconnect();
});
</script>

<template>
  <span class="cd2-repair-anchor"></span>
  <Teleport v-if="isOwner" to=".action-bar-buttons">
    <ab-button variant="secondary" size="sm" @click="onRepair">
      {{ $t('downloader.action.cd2_repair') }}
    </ab-button>
  </Teleport>
</template>

<style scoped>
.cd2-repair-anchor {
  display: none;
}
</style>
