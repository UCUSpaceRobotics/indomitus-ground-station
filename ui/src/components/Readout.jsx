import { NO_VALUE } from '../lib/format';

/**
 * One labelled telemetry value.
 *
 * `noData` renders an em dash rather than a zero. That distinction is the whole
 * point of the component: a rover UI that shows "0.00 m/s" when the topic is
 * simply absent tells the operator something false.
 */
export default function Readout({ icon: Icon, label, value, unit, tone = 'default', noData = false, title }) {
  const state = noData ? 'nodata' : tone;
  return (
    <div className={`readout is-${state}`} title={title}>
      <span className="readout-label">
        {Icon && <Icon size={13} />}
        {label}
      </span>
      <span className="readout-value mono">
        {noData ? NO_VALUE : value}
        {!noData && unit && <span className="readout-unit">{unit}</span>}
      </span>
    </div>
  );
}

/** Horizontal meter for bounded values (battery, signal, axis deflection). */
export function Bar({ value, min = 0, max = 1, tone = 'accent', bipolar = false }) {
  const span = max - min || 1;
  const ratio = Math.min(1, Math.max(0, (value - min) / span));
  if (bipolar) {
    const center = (0 - min) / span;
    const left = Math.min(center, ratio);
    const width = Math.abs(ratio - center);
    return (
      <div className="bar">
        <div className="bar-zero" style={{ left: `${center * 100}%` }} />
        <div
          className={`bar-fill is-${tone}`}
          style={{ left: `${left * 100}%`, width: `${width * 100}%` }}
        />
      </div>
    );
  }
  return (
    <div className="bar">
      <div className={`bar-fill is-${tone}`} style={{ left: 0, width: `${ratio * 100}%` }} />
    </div>
  );
}
