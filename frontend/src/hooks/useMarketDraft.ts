import { useId, useLayoutEffect } from 'react';
import { useAppStore } from '../stores/appStore';

/** 草稿登记仅在本标签页内有效，卸载后移除，不写入共享存储。 */
export function useMarketDraft(dirty: boolean): void {
  const id = useId();
  useLayoutEffect(() => {
    useAppStore.getState().setMarketDraft(id, dirty);
    return () => useAppStore.getState().setMarketDraft(id, false);
  }, [id, dirty]);
}
