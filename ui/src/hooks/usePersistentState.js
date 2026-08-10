import { useCallback, useEffect, useState } from 'react';

/**
 * useState backed by localStorage. Used for per-monitor view preferences so an
 * operator's layout survives the page reloads that happen during a run.
 */
export function usePersistentState(key, initialValue) {
  const [value, setValue] = useState(() => {
    try {
      const raw = window.localStorage.getItem(key);
      return raw === null ? initialValue : JSON.parse(raw);
    } catch {
      return initialValue;
    }
  });

  useEffect(() => {
    try {
      window.localStorage.setItem(key, JSON.stringify(value));
    } catch {
      // Storage unavailable (private mode / quota): keep going in memory.
    }
  }, [key, value]);

  const reset = useCallback(() => setValue(initialValue), [initialValue]);

  return [value, setValue, reset];
}
