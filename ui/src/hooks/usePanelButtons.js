import { useEffect, useRef } from 'react';
import { useConfig } from '../config';
import { useTopic } from '../ros/useTopic';

/**
 * Rising-edge detector across both control-box boards.
 *
 * Two sources, because the panel has two: the joystick board's own 9 switches
 * ride in `Joy.buttons`, while the button board's 23 arrive on `/switches`.
 * A binding is therefore `{source, index}`, not a bare number — index 3 means
 * different hardware on each board.
 *
 * Edges only. Holding a switch closed must not repeat the action, and a toggle
 * left in the "on" position must not re-fire on every message.
 *
 * @param {(button: {source: 'joy'|'switches', index: number}) => void} onPress
 */
export function usePanelButtons(onPress) {
  const config = useConfig();

  // 50 ms so a short press is not missed between render flushes; the button
  // board only transmits on change, and switch_reader republishes at 10 Hz.
  const joy = useTopic(config.topics.joy, 'sensor_msgs/Joy', { throttleMs: 0, renderMs: 50 });
  const switches = useTopic(config.topics.switches, 'std_msgs/Int32MultiArray', {
    throttleMs: 0,
    renderMs: 50,
  });

  const previous = useRef({ joy: [], switches: [] });
  // Held in a ref so swapping the handler (entering "learn" mode, say) does not
  // re-run the effect and replay the current state as a fresh edge.
  const handler = useRef(onPress);
  handler.current = onPress;

  const joyButtons = joy.message?.buttons;
  const switchStates = switches.message?.data;

  useEffect(() => {
    const emitEdges = (source, current) => {
      if (!Array.isArray(current)) return;
      const before = previous.current[source];
      for (let i = 0; i < current.length; i += 1) {
        if (current[i] === 1 && before[i] !== 1) {
          handler.current?.({ source, index: i });
        }
      }
      previous.current[source] = current.slice();
    };

    emitEdges('joy', joyButtons);
    emitEdges('switches', switchStates);
  }, [joyButtons, switchStates]);
}

export function buttonLabel(binding) {
  if (!binding) return null;
  const board = binding.source === 'joy' ? 'stick board' : 'button board';
  return `${board} #${binding.index}`;
}

export function sameButton(a, b) {
  return Boolean(a && b && a.source === b.source && a.index === b.index);
}
