// Runtime configuration, read from VITE_* env vars (see .env.example at the repo root).
// Defaults match the local ports in CONTRIBUTING.md.

/** REST base URL of the DriftWatch backend. */
export const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000'

/** WebSocket endpoint of the DriftWatch backend. */
export const WS_URL = import.meta.env.VITE_WS_URL ?? 'ws://localhost:8000/ws'

/** Generator control API (Simulate incident buttons). */
export const CONTROL_URL = import.meta.env.VITE_CONTROL_URL ?? 'http://localhost:8001'
