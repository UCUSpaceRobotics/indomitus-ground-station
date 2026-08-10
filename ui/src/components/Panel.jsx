/** Shared chrome for every dashboard card: title row, optional actions, body. */
export default function Panel({ icon: Icon, title, actions, children, className = '', bodyClassName = '' }) {
  return (
    <section className={`panel ${className}`.trim()}>
      <header className="panel-head">
        <h2>
          {Icon && <Icon size={16} />}
          {title}
        </h2>
        {actions && <div className="panel-actions">{actions}</div>}
      </header>
      <div className={`panel-body ${bodyClassName}`.trim()}>{children}</div>
    </section>
  );
}
