import { useState } from 'react';
import styles from './AlertCard.module.css';

const SEVERITY_LABEL = { INFO: 'Info', WARN: 'Warning', CRITICAL: 'Critical' };

export function AlertCard({ alert }) {
  const [expanded, setExpanded] = useState(false);
  const sev = alert.severity?.toLowerCase();

  return (
    <div className={`${styles.card} ${styles[sev] || ''}`}>
      <div className={styles.header} onClick={() => setExpanded((e) => !e)}>
        <span className={styles.severity}>{SEVERITY_LABEL[alert.severity] || alert.severity}</span>
        <span className={styles.services}>{(alert.services || []).join(', ')}</span>
        <span className={styles.status}>{alert.status}</span>
      </div>
      <div className={styles.meta}>
        {alert.metric}: {alert.observed}% (baseline {alert.baseline ?? '—'}%)
      </div>
      {expanded && (
        <div className={styles.explanation}>
          {alert.explanation}
          {alert.top_signatures?.length > 0 && (
            <ul className={styles.sigList}>
              {alert.top_signatures.map((s) => (
                <li key={s.signature}>{s.signature} × {s.count}</li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
