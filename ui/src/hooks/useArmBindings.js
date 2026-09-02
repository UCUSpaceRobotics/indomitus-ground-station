import { useCallback, useEffect, useRef, useState } from 'react';
import { useConfig } from '../config';
import { useRos } from '../ros/context';
import { useServiceCaller } from '../ros/useService';
import { ALL_KEYS, parseBind } from '../lib/armSlots';

const BIND_PREFIX = 'bind.';

/**
 * Read-only view of the arm mapping held by `arm_gamepad`.
 *
 * The node is the only place the mapping lives — it is not in the UI's config,
 * because the operator binds it by pressing controls, not by editing settings.
 * Panels that merely want to *name* a control therefore have to ask the node.
 *
 * Deliberately separate from ArmMappingPanel's own load: that one owns an
 * editing draft, a dirty flag and a status line, none of which a label needs.
 *
 * @returns {{bindings: Record<string, string>, reload: () => void}}
 */
export function useArmBindings() {
  const config = useConfig();
  const { connected } = useRos();
  const callService = useServiceCaller();
  const [bindings, setBindings] = useState({});

  const reload = useCallback(async () => {
    try {
      const response = await callService(
        `${config.armNode}/get_parameters`,
        'rcl_interfaces/srv/GetParameters',
        { names: ALL_KEYS.map((key) => BIND_PREFIX + key) },
      );
      const values = response?.values || [];
      const next = {};
      ALL_KEYS.forEach((key, i) => {
        const text = values[i]?.string_value || '';
        if (text) next[key] = text;
      });
      setBindings(next);
    } catch {
      // A label is not worth a visible error: the panel simply shows the
      // control as unassigned, which is what it looked like before.
      setBindings({});
    }
  }, [callService, config.armNode]);

  // Once per connection. The mapping only changes when somebody edits it on
  // the arm-mapping page, and that page reloads it itself.
  const loadedFor = useRef(null);
  useEffect(() => {
    if (!connected) {
      loadedFor.current = null;
      return;
    }
    if (loadedFor.current === config.armNode) return;
    loadedFor.current = config.armNode;
    reload();
  }, [connected, config.armNode, reload]);

  return { bindings, reload };
}

/**
 * Which arm slot sits on one console control, as `{key, bind}` — or null.
 *
 * @param {Record<string, string>} bindings  from useArmBindings
 * @param {'joy'|'switches'} source
 */
export function armSlotAt(bindings, source, index) {
  for (const [key, text] of Object.entries(bindings)) {
    const bind = parseBind(text);
    if (bind && bind.source === source && bind.index === index) return { key, bind };
  }
  return null;
}
