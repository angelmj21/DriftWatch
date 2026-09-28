import { useEffect, useRef, useState, useCallback } from 'react';
import { CONTROL_URL } from '../../config.js';
import styles from './IncidentControl.module.css';

const SCENARIOS = ['spike', 'drift', 'new_error', 'cascade'];

// TODO(confirm with backend): exact /status response shape assumed here is
// { active: [{ type, service? }, ...] }. Adjust activeScenarios below once
// confirmed.
export function IncidentControl() {
  const [services, setServices] = useState([]);
  const [selectedService, setSelectedService] = useState('');
  const [status, setStatus] = useState(null);
  const [generatorDown, setGeneratorDown] = useState(false);
  const [pending, setPending] = useState(null);
  const [error, setError] = useState(null);
  const pollRef = useRef(null);

  const fetchScenariosList = useCallback(async () => {
    try {
      const res = await fetch(`${CONTROL_URL}/scenarios`);
      if (!res.ok) throw new Error();
      const data = await res.json();
      if (Array.isArray(data.services)) setServices(data.services);
      setGeneratorDown(false);
    } catch {
      setGeneratorDown(true);
    }
  }, []);

  const pollStatus = useCallback(async () => {
    try {
      const res = await fetch(`${CONTROL_URL}/status`);
      if (!res.ok) throw new Error();
      setStatus(await res.json());
      setGeneratorDown(false);
    } catch {
      setGeneratorDown(true);
      setStatus(null);
    }
  }, []);

  useEffect(() => {
    fetchScenariosList();
    pollStatus();
    pollRef.current = setInterval(pollStatus, 3000);
    return () => clearInterval(pollRef.current);
  }, [fetchScenariosList, pollStatus]);

  const activeScenarios = new Set((status?.active || []).map((s) => s.type));

  async function trigger(scenario) {
    setError(null);
    setPending(scenario);
    try {
      const res = await fetch(`${CONTROL_URL}/scenario`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: scenario, service: selectedService || undefined }),
      });
      if (res.status === 409) {
        setError('That scenario is already running.');
      } else if (res.status === 422) {
        const body = await res.json().catch(() => null);
        setError(body?.detail || 'Invalid scenario request.');
      } else if (!res.ok) {
        setError(`Failed to start scenario (${res.status}).`);
      } else {
        await pollStatus();
      }
    } catch {
      setGeneratorDown(true);
      setError('Generator control API is unreachable.');
    } finally {
      setPending(null);
    }
  }

  if (generatorDown) {
    return <div className={styles.down}>Generator control API is unreachable.</div>;
  }

  return (
    <div className={styles.wrap}>
      {services.length > 0 && (
        <select className={styles.select} value={selectedService} onChange={(e) => setSelectedService(e.target.value)}>
          <option value="">All services</option>
          {services.map((s) => (
            <option key={s} value={s}>{s}</option>
          ))}
        </select>
      )}
      <div className={styles.buttons}>
        {SCENARIOS.map((s) => (
          <button
            key={s}
            className={styles.button}
            disabled={activeScenarios.has(s) || pending === s}
            onClick={() => trigger(s)}
          >
            {pending === s ? 'Starting…' : activeScenarios.has(s) ? `${s} (active)` : s}
          </button>
        ))}
      </div>
      {error && <div className={styles.error}>{error}</div>}
    </div>
  );
}
