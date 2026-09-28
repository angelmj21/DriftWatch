import styles from './StatusBadge.module.css';

const CONNECTION_LABELS = {
  connecting: 'Reconnecting…',
  live: 'Live',
  polling: 'Polling',
  down: 'Down',
};

const STATE_LABELS = {
  learning: 'Learning normal behaviour…',
  normal: 'Normal',
  anomaly: 'Anomaly detected',
};

// Props-driven only. No hooks, no side effects.
export function StatusBadge({ state, connection }) {
  return (
    <div className={styles.wrap} role="status">
      <span className={`${styles.dot} ${styles[state] || ''}`} aria-hidden="true" />
      <span className={styles.stateLabel}>{STATE_LABELS[state] ?? state}</span>
      <span className={`${styles.connection} ${styles[connection] || ''}`}>
        {CONNECTION_LABELS[connection] ?? connection}
      </span>
    </div>
  );
}
