import { createContext, useContext } from 'react';

/**
 * `generation` is bumped on every successful (re)connect. roslib does not
 * replay subscriptions across sockets, so every hook that subscribes keys its
 * effect on it and re-subscribes after a reconnect.
 */
export const RosContext = createContext({
  ros: null,
  status: 'idle',
  generation: 0,
  connected: false,
  attempt: 0,
  error: null,
  latencyMs: null,
  clockSkewMs: null,
  url: '',
  reconnect: () => {},
});

export function useRos() {
  return useContext(RosContext);
}
