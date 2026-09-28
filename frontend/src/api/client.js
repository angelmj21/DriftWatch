// REST polling client for the DriftWatch backend (Issue #18).
// Endpoints: docs/interfaces.md "REST polling fallback". Base URL comes from src/config.js.
// No caching and no fake fallbacks: failures are thrown as ApiError-shaped objects.

import { API_URL } from '../config.js'

/**
 * NOTE: typedefs below follow the field list in the project issues (#2).
 * TODO: verify against docs/alert-schema.md once it is merged; if they differ, tell Angel.
 *
 * @typedef {Object} TopSignature
 * @property {string} signature
 * @property {number} count
 *
 * @typedef {Object} Alert
 * @property {string} id
 * @property {'open'|'updated'|'resolved'} status
 * @property {'INFO'|'WARN'|'CRITICAL'} severity
 * @property {string[]} services
 * @property {string} metric
 * @property {number} observed      percent, e.g. 14.2 means 14.2%
 * @property {number} baseline      percent
 * @property {number} deviation
 * @property {number} slope
 * @property {TopSignature[]} top_signatures
 * @property {string|null} new_signature
 * @property {string} explanation
 * @property {string} started_at    ISO-8601 UTC
 * @property {string} updated_at    ISO-8601 UTC
 * @property {string|null} resolved_at
 *
 * @typedef {Object} LiveMetric
 * @property {string} timestamp     ISO-8601 UTC
 * @property {number} error_rate    percent
 * @property {number|null} baseline    null while state is 'learning'
 * @property {number|null} band_low    null while state is 'learning'
 * @property {number|null} band_high   null while state is 'learning'
 * @property {'learning'|'normal'|'anomaly'} state
 *
 * @typedef {Object} Health
 * @property {string} status
 * @property {'learning'|'normal'|'anomaly'} state
 * @property {number} lines_processed
 * @property {number} uptime_s
 *
 * @typedef {Object} SignatureItem
 * @property {string} signature
 * @property {string} template
 * @property {number} count
 *
 * @typedef {Object} Signatures
 * @property {SignatureItem[]} top
 * @property {string[]} new
 *
 * @typedef {Object} ApiError
 * @property {number} status    HTTP status, or 0 for network error / timeout
 * @property {string} message
 */

export const TIMEOUT_MS = 5000

/** Build the consistent error object thrown by every function here. */
function apiError(status, message) {
  return { status, message }
}

/**
 * GET a path with a timeout. Timeout and network errors both become {status: 0, message}.
 * @param {string} path
 * @param {Record<string, string|number|undefined|null>} [params]
 */
async function get(path, params = {}) {
  const url = new URL(path, API_URL)
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== null && value !== '') {
      url.searchParams.set(key, String(value))
    }
  }

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS)
  let response
  try {
    response = await fetch(url, { signal: controller.signal })
  } catch (err) {
    if (err?.name === 'AbortError') {
      throw apiError(0, `Request timed out after ${TIMEOUT_MS / 1000}s`)
    }
    throw apiError(0, 'Network error: could not reach the backend')
  } finally {
    clearTimeout(timer)
  }

  if (!response.ok) {
    throw apiError(response.status, `${response.status} ${response.statusText}`.trim())
  }
  try {
    return await response.json()
  } catch {
    throw apiError(response.status, 'Invalid JSON in response')
  }
}

/** @returns {Promise<Health>} */
export function getHealth() {
  return get('/health')
}

/**
 * Last N metrics, oldest first.
 * @param {number} [limit]
 * @returns {Promise<LiveMetric[]>}
 */
export function getMetrics(limit) {
  return get('/metrics', { limit })
}

/**
 * Alert messages, newest first.
 * @param {{limit?: number, status?: 'open'|'updated'|'resolved'}} [options]
 * @returns {Promise<Alert[]>}
 */
export function getAlerts({ limit, status } = {}) {
  return get('/alerts', { limit, status })
}

/** @returns {Promise<Signatures>} */
export function getSignatures() {
  return get('/signatures')
}
