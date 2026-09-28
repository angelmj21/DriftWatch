import { useEffect, useRef, useState, useCallback } from 'react';
import { WS_URL, API_URL } from './config.js';

// TODO(confirm with Joshua/Angel): exact WS message envelope
// ({ type: 'metric' | 'alert' | 'signatures' | 'snapshot', data }) and the
// REST snapshot path used for polling fallback. Adjust applyMessage /
// startPolling below once the real backend shape is confirmed.

const METRICS_WINDOW_MS = 5 * 60 * 1000;
const RECONNECT_DELAY_MS = 2000;
const POLL_INTERVAL_MS = 3000;

export function useAlertStream() {
  const [metrics, setMetrics] = useState([]);
  const [alerts, setAlerts] = useState([]);
  const [signatures, setSignatures] = useState({ top: [], new: [] });
  const [state, setState] = useState('learning');
  const [connection, setConnection] = useState('connecting');
  const [lastMessageAt, setLastMessageAt] = useState(null);

  const wsRef = useRef(null);
  const pollTimerRef = useRef(null);
  const reconnectTimerRef = useRef(null);
  const alertsMapRef = useRef(new Map());

  const applyMessage = useCallback((msg) => {
    setLastMessageAt(Date.now());

    if (msg.type === 'metric' && msg.data) {
      setState(msg.data.state ?? 'learning');
      setMetrics((prev) => {
        const cutoff = Date.now() - METRICS_WINDOW_MS;
        return [...prev, msg.data].filter(
          (m) => new Date(m.timestamp).getTime() >= cutoff
        );
      });
    }

    if (msg.type === 'alert' && msg.data) {
      alertsMapRef.current.set(msg.data.id, msg.data);
      setAlerts(Array.from(alertsMapRef.current.values()));
    }

    if (msg.type === 'signatures' && msg.data) {
      setSignatures(msg.data);
    }

    if (msg.type === 'snapshot' && msg.data) {
      if (msg.data.metrics) setMetrics(msg.data.metrics);
      if (msg.data.alerts) {
        alertsMapRef.current = new Map(msg.data.alerts.map((a) => [a.id, a]));
        setAlerts(msg.data.alerts);
      }
      if (msg.data.signatures) setSignatures(msg.data.signatures);
      if (msg.data.state) setState(msg.data.state);
    }
  }, []);

  const stopPolling = useCallback(() => {
    if (pollTimerRef.current) {
      clearInterval(pollTimerRef.current);
      pollTimerRef.current = null;
    }
  }, []);

  const startPolling = useCallback(() => {
    if (pollTimerRef.current) return;
    setConnection('polling');
    const poll = async () => {
      try {
        const res = await fetch(`${API_URL}/api/snapshot`);
        if (!res.ok) throw new Error(`snapshot ${res.status}`);
        const data = await res.json();
        applyMessage({ type: 'snapshot', data });
        setConnection('polling');
      } catch {
        setConnection('down');
      }
    };
    poll();
    pollTimerRef.current = setInterval(poll, POLL_INTERVAL_MS);
  }, [applyMessage]);

  const connect = useCallback(() => {
    setConnection((c) => (c === 'live' ? c : 'connecting'));
    const ws = new WebSocket(WS_URL);
    wsRef.current = ws;

    ws.onopen = () => {
      stopPolling();
      setConnection('live');
    };
    ws.onmessage = (evt) => {
      try {
        applyMessage(JSON.parse(evt.data));
      } catch {
        // ignore malformed frame
      }
    };
    ws.onerror = () => {
      ws.close();
    };
    ws.onclose = () => {
      startPolling();
      reconnectTimerRef.current = setTimeout(connect, RECONNECT_DELAY_MS);
    };
  }, [applyMessage, startPolling, stopPolling]);

  useEffect(() => {
    connect();
    return () => {
      wsRef.current?.close();
      stopPolling();
      clearTimeout(reconnectTimerRef.current);
    };
  }, [connect, stopPolling]);

  return { metrics, alerts, signatures, state, connection, lastMessageAt };
}
