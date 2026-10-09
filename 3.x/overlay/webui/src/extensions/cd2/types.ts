export interface CloudDrive2 {
  enable: boolean;
  host: string;
  username: string;
  password: string;
  target_dir: string;
  offline_dir: string;
  stall_time: number;
  stall_min_speed: number;
  scan_interval: number;
  local_path: string;
  pause_qb_torrent: boolean;
  resume_after_recheck: boolean;
}

export const defaultCd2Config: CloudDrive2 = {
  enable: false,
  host: '',
  username: '',
  password: '',
  target_dir: '',
  offline_dir: '',
  stall_time: 60,
  stall_min_speed: 1,
  scan_interval: 300,
  local_path: '/cd2-offline',
  pause_qb_torrent: true,
  resume_after_recheck: true,
};
