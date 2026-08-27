import { useCallback } from 'react';
import * as ROSLIB from 'roslib';
import { useRos } from './context';

/**
 * Calls a ROS service and resolves with its response.
 *
 * roslib's `callService` takes success and failure callbacks and, on a bridge
 * that has gone away, calls neither — so a bare wrapper leaves the caller's
 * "saving…" spinner up forever. The timeout here makes failure observable,
 * which matters for the calibration flow: an operator has to know whether the
 * values actually reached the rover.
 *
 * @returns {(name: string, serviceType: string, values?: object, timeoutMs?: number) => Promise<object>}
 */
export function useServiceCaller() {
  const { ros, connected } = useRos();

  return useCallback(
    (name, serviceType, values = {}, timeoutMs = 5000) =>
      new Promise((resolve, reject) => {
        if (!ros || !connected) {
          reject(new Error('rosbridge is not connected'));
          return;
        }

        const service = new ROSLIB.Service({ ros, name, serviceType });
        let settled = false;

        const timer = setTimeout(() => {
          if (settled) return;
          settled = true;
          reject(new Error(`${name} timed out after ${timeoutMs} ms`));
        }, timeoutMs);

        // roslib v2 dropped the ServiceRequest wrapper — callService takes the
        // request object as-is, and `new ROSLIB.ServiceRequest(...)` throws.
        service.callService(
          values,
          (response) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            resolve(response);
          },
          (error) => {
            if (settled) return;
            settled = true;
            clearTimeout(timer);
            reject(new Error(String(error || `${name} failed`)));
          },
        );
      }),
    [ros, connected],
  );
}

/**
 * Builds an `rcl_interfaces/srv/SetParameters` request.
 *
 * Type tags are the ParameterType enum: 3 = double, 4 = string,
 * 7 = integer array, 8 = double array. Pass `['name', value, 'int_array']` to
 * force the integer form — `axis_map` is declared as integers on the node and
 * rclpy rejects a double array for it, which is not obvious from the error it
 * returns. Pass `'string'` for the arm mapping, whose bindings cross as one
 * short string per slot.
 */
export function parameterRequest(entries) {
  return {
    parameters: entries.map(([name, value, kind]) => {
      const isArray = Array.isArray(value);
      const isIntArray = isArray && kind === 'int_array';
      const isString = kind === 'string';
      return {
        name,
        value: {
          type: isString ? 4 : isIntArray ? 7 : isArray ? 8 : 3,
          double_value: isArray || isString ? 0 : Number(value),
          double_array_value: isArray && !isIntArray ? value.map(Number) : [],
          integer_array_value: isIntArray ? value.map((v) => Math.round(Number(v))) : [],
          // rosbridge fills unset fields itself, but roslib serializes exactly
          // what it is handed, and rclpy rejects a partial ParameterValue.
          bool_value: false,
          integer_value: 0,
          string_value: isString ? String(value) : '',
          byte_array_value: [],
          bool_array_value: [],
          string_array_value: [],
        },
      };
    }),
  };
}
