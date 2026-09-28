import { useAlertStream } from './useAlertStream.js';
import { Reveal } from './components/shared/Reveal.jsx';
import { StatusBadge } from './components/StatusBadge/StatusBadge.jsx';
import { ErrorRateChart } from './components/ErrorRateChart/ErrorRateChart.jsx';
import { AlertFeed } from './components/AlertFeed/AlertFeed.jsx';
import { SignaturePanel } from './components/SignaturePanel/SignaturePanel.jsx';
import { IncidentTimeline } from './components/IncidentTimeline/IncidentTimeline.jsx';
import { IncidentControl } from './components/IncidentControl/IncidentControl.jsx';
import styles from './App.module.css';

const NAV = [
  { id: 'overview', label: 'Overview' },
  { id: 'alerts', label: 'Alerts' },
  { id: 'history', label: 'History' },
  { id: 'control', label: 'Control' },
];

// useAlertStream() is called exactly once, here. Everything below is
// props-driven from this single source of truth.
export default function App() {
  const { metrics, alerts, signatures, state, connection } = useAlertStream();

  return (
    <div className={styles.app}>
      <header className={styles.header}>
        <span className={styles.brand}>DriftWatch</span>
        <nav className={styles.nav}>
          {NAV.map((n) => (
            <a key={n.id} href={`#${n.id}`}>{n.label}</a>
          ))}
        </nav>
        <StatusBadge state={state} connection={connection} />
      </header>

      {(connection === 'polling' || connection === 'down') && (
        <div className={styles.banner}>WebSocket lost — polling fallback</div>
      )}

      <main className={styles.main}>
        <section id="overview" className={styles.section}>
          <Reveal>
            <ErrorRateChart metrics={metrics} alerts={alerts} />
          </Reveal>
        </section>

        <section id="alerts" className={styles.splitSection}>
          <Reveal>
            <AlertFeed alerts={alerts} />
          </Reveal>
          <Reveal delay={0.05}>
            <SignaturePanel top={signatures.top} new={signatures.new} />
          </Reveal>
        </section>

        <section id="history" className={styles.section}>
          <Reveal>
            <IncidentTimeline alerts={alerts} />
          </Reveal>
        </section>

        <section id="control" className={styles.section}>
          <Reveal>
            <IncidentControl />
          </Reveal>
        </section>
      </main>
    </div>
  );
}
