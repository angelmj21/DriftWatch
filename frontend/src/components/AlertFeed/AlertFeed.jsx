import { useState, useMemo } from 'react';
import { AlertCard } from '../AlertCard/AlertCard.jsx';
import styles from './AlertFeed.module.css';

const FILTERS = ['ALL', 'INFO', 'WARN', 'CRITICAL'];

export function AlertFeed({ alerts = [] }) {
  const [filter, setFilter] = useState('ALL');

  const sorted = useMemo(
    () => [...alerts].sort((a, b) => new Date(b.updated_at) - new Date(a.updated_at)),
    [alerts]
  );
  const filtered = filter === 'ALL' ? sorted : sorted.filter((a) => a.severity === filter);

  return (
    <div className={styles.wrap}>
      <div className={styles.filters}>
        {FILTERS.map((f) => (
          <button
            key={f}
            className={`${styles.filterButton} ${filter === f ? styles.active : ''}`}
            onClick={() => setFilter(f)}
          >
            {f}
          </button>
        ))}
      </div>
      <div className={styles.list}>
        {filtered.length === 0 && <div className={styles.empty}>No alerts.</div>}
        {filtered.map((a) => (
          <AlertCard key={a.id} alert={a} />
        ))}
      </div>
    </div>
  );
}
