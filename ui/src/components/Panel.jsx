import { ChevronDown } from 'lucide-react';

/** Shared chrome for every dashboard card: title row, optional actions, body.
 *
 * Pass `collapsible` with `collapsed`/`onToggleCollapse` to get a triangle in
 * the title row that folds the body away — used to give the cameras the whole
 * screen when the operator does not need the log. */
export default function Panel({
  icon: Icon,
  title,
  actions,
  children,
  className = '',
  bodyClassName = '',
  collapsible = false,
  collapsed = false,
  onToggleCollapse,
}) {
  const isCollapsed = collapsible && collapsed;
  const heading = (
    <>
      {Icon && <Icon size={16} />}
      {title}
    </>
  );

  return (
    <section className={`panel ${isCollapsed ? 'is-collapsed' : ''} ${className}`.trim()}>
      <header className="panel-head">
        {collapsible ? (
          <button
            type="button"
            className="panel-toggle"
            onClick={onToggleCollapse}
            aria-expanded={!isCollapsed}
            title={isCollapsed ? 'Expand' : 'Collapse'}
          >
            <ChevronDown size={14} className="panel-caret" />
            <h2>{heading}</h2>
          </button>
        ) : (
          <h2>{heading}</h2>
        )}
        {actions && !isCollapsed && <div className="panel-actions">{actions}</div>}
      </header>
      {!isCollapsed && <div className={`panel-body ${bodyClassName}`.trim()}>{children}</div>}
    </section>
  );
}
