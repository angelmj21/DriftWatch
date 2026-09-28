import { useMemo, useState, useEffect } from 'react';
import {
  ComposedChart, Line, Area, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, ReferenceArea, ReferenceDot,
} from 'recharts';
import styles from './ErrorRateChart.module.css';

const WINDOW_MS = 5 * 60 * 1000;

const SEVERITY_COLOR_VAR = {
  INFO: '--sev-info',
  WARN: '--sev-warn',
  CRITICAL: '--sev-critical',
};

function useCssVar(name, fallback) {
  const [value, setValue] = useState(fallback);
  useEffect(() => {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    if (v) setValue(v);
  }, [name]);
  return value;
}

function formatTime(ts) {
  return new Date(ts).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function CustomTooltip({ active, payload }) {
  if (!active || !payload?.length) return null;
  const point = payload[0]?.payload;
  if (!point) return null;
  return (
    <div className={styles.tooltip}>
      <div className={styles.tooltipTime}>{formatTime(point.ts)}</div>
      <div>Observed: {point.error_rate?.toFixed(2)}%</div>
      {point.baseline != null && <div>Baseline: {point.baseline.toFixed(2)}%</div>}
      {point.band_low != null && point.band_high != null && (
        <div>Band: {point.band_low.toFixed(2)}%–{point.band_high.toFixed(2)}%</div>
      )}
    </div>
  );
}

// Props: { metrics: LiveMetric[], alerts: Alert[] }
export function ErrorRateChart({ metrics = [], alerts = [] }) {
  const accent = useCssVar('--accent', '#7c5cff');
  const muted = useCssVar('--muted', '#8891a0');
  const border = useCssVar('--border', '#232830');
  const critical = useCssVar('--sev-critical', '#e5484d');

  const data = useMemo(
    () =>
      metrics.map((m) => ({
        ...m,
        ts: new Date(m.timestamp).getTime(),
        bandRange: m.band_low != null && m.band_high != null ? m.band_high - m.band_low : null,
      })),
    [metrics]
  );

  const anomalySegments = useMemo(() => {
    const segments = [];
    let start = null;
    data.forEach((point, i) => {
      if (point.state === 'anomaly') {
        if (start == null) start = point.ts;
      } else if (start != null) {
        segments.push([start, data[i - 1].ts]);
        start = null;
      }
    });
    if (start != null) segments.push([start, data[data.length - 1]?.ts]);
    return segments;
  }, [data]);

  const isEmpty = data.length === 0;
  const isLearning = !isEmpty && data.every((d) => d.state === 'learning');
  const windowStart = data.length ? data[0].ts : Date.now() - WINDOW_MS;
  const windowEnd = data.length ? data[data.length - 1].ts : Date.now();

  const visibleAlerts = alerts.filter((a) => new Date(a.started_at).getTime() >= windowStart);

  return (
    <div className={styles.wrap}>
      {isEmpty && <div className={styles.empty}>Waiting for metrics…</div>}
      {!isEmpty && isLearning && <div className={styles.overlay}>Learning normal behaviour…</div>}
      {!isEmpty && (
        <ResponsiveContainer width="100%" height={280}>
          <ComposedChart data={data} margin={{ top: 12, right: 16, bottom: 4, left: 0 }}>
            <CartesianGrid stroke={border} strokeDasharray="3 3" vertical={false} />
            <XAxis
              dataKey="ts"
              type="number"
              domain={[windowStart, windowEnd]}
              tickFormatter={formatTime}
              stroke={muted}
              tick={{ fontSize: 11 }}
            />
            <YAxis stroke={muted} tick={{ fontSize: 11 }} unit="%" />
            <Tooltip content={<CustomTooltip />} />

            {!isLearning && (
              <Area dataKey="band_low" stackId="band" stroke="none" fill="transparent" isAnimationActive={false} />
            )}
            {!isLearning && (
              <Area dataKey="bandRange" stackId="band" stroke="none" fill={accent} fillOpacity={0.12} isAnimationActive={false} />
            )}
            {!isLearning && (
              <Line dataKey="baseline" stroke={muted} strokeDasharray="4 4" dot={false} isAnimationActive={false} />
            )}
            <Line dataKey="error_rate" stroke={accent} strokeWidth={2} dot={false} isAnimationActive={false} />

            {anomalySegments.map(([x1, x2], i) => (
              <ReferenceArea key={i} x1={x1} x2={x2} fill={critical} fillOpacity={0.08} strokeOpacity={0} />
            ))}

            {visibleAlerts.map((a) => (
              <ReferenceDot
                key={a.id}
                x={new Date(a.started_at).getTime()}
                y={a.observed}
                r={4}
                fill={`var(${SEVERITY_COLOR_VAR[a.severity] || '--accent'})`}
                stroke="none"
                isFront
              />
            ))}
          </ComposedChart>
        </ResponsiveContainer>
      )}
    </div>
  );
}
