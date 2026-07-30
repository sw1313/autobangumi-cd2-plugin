import type { CloudDrive2 } from '@/extensions/cd2/types';

export const cd2Api = {
  async getConfig() {
    const { data } = await axios.get<CloudDrive2>(
      'api/v1/extensions/cd2/config',
      { silent: true }
    );
    return data;
  },

  async updateConfig(payload: CloudDrive2) {
    const { data } = await axios.patch<{ msg_en: string; msg_zh: string }>(
      'api/v1/extensions/cd2/config',
      payload
    );
    return data;
  },

  async testConnection(payload: {
    host: string;
    username: string;
    password: string;
  }) {
    const { data } = await axios.post<{
      success: boolean;
      msg_en: string;
      msg_zh: string;
    }>('api/v1/extensions/cd2/config/test', payload, { silent: true });
    return data;
  },

  async repairTorrents(hashes: string[]) {
    const { data } = await axios.post<{
      success: boolean;
      submitted: number;
      synced: number;
      skipped: number;
      failed: number;
      msg_en: string;
      msg_zh: string;
    }>('api/v1/extensions/cd2/downloader/torrents/cd2-repair', { hashes });
    return data!;
  },
};
