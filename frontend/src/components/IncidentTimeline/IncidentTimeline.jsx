import { useMemo } from 'react';
import styles from './IncidentTimeline.module.css';

const WINDOW_MS = 30 * 60 * 1000;

function formatDuration(ms) {
  const totalMinutes = Math.max(1, Math.round(ms / 60000));
  if (totalMinutes < 60) return `${totalMinutes}m`;
  const h = Math.floor(totalMinutes / 60);
  const m = totalMinutes % 60;
  return `${h}h ${m}m`;
}

// One Alert.id = one incident (cascade already bundles services into a
// single alert object per the frozen contract), so no separate grouping
// step is needed beyond reading each alert's own lifecycle.
export function IncidentTimeline({ alerts = [] }) {
  const windowEnd = Date.now();
  const windowStart = windowEnd - WINDOW_MS;

  const incidents = useMemo(() => {
    return alerts
      .map((a) => {
        const start = new Date(a.started_at).getTime();
        const end = a.resolved_at ? new Date(a.resolved_at).getTime() : windowEnd;
        return { ...a, start, end };
      })
      .filter((a) => a.end >= windowStart)
      .sort((a, b) => a.start - b.start);
  }, [alerts, windowStart, windowEnd]);

  if (incidents.length === 0) {
    return <div className={styles.empty}>No incidents in the last 30 minutes.</div>;
  }

  const span = windowEnd - windowStart;

  return (
    <div className={styles.wrap}>
      {incidents.map((inc) => {
        const left = Math.max(0, ((inc.start - windowStart) / span) * 100);
        const rawWidth = ((Math.min(inc.end, windowEnd) - Math.max(inc.start, windowStart)) / span) * 100;
        const width = Math.max(1, rawWidth);
        const open = !inc.resolved_at;
        const sevClass = inc.severity?.toLowerCase();
        return (
          <div key={inc.id} className={styles.row}>
            <div
              className={`${styles.bar} ${styles[sevClass] || ''} ${open ? styles.open : ''}`}
              style={{ left: `${left}%`, width: `${width}%` }}
              title={`${(inc.services || []).join(', ')} · ${inc.severity} · ${formatDuration(inc.end - inc.start)}${inc.explanation ? ` — ${inc.explanation}` : ''}`}
            />
          </div>
        );
      })}
    </div>
  );
}
