import { invoke } from '@tauri-apps/api/core';

export interface LocalServiceStatus {
  status: 'idle' | 'starting' | 'connected' | 'failed' | 'stopping';
  base_url: string;
  managed: boolean;
  error: string | null;
}

export const getLocalServiceStatus = () => invoke<LocalServiceStatus>('local_service_status');
export const startLocalService = () => invoke<void>('start_local_service');
