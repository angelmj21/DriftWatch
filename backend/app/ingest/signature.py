"""Error signature masker for DriftWatch log events.

Groups similar errors under a single stable signature by masking variable
parts (IDs, numbers, hex tokens, IPs, path parameters) so that e.g.
'Database connection timeout after 5000ms' and '...after 5002ms' share
the same signature.

Implements make_signature(event) -> (signature_id, masked_template)
exactly as specified in docs/interfaces.md.
"""

import hashlib
import re
from typing import Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.app.ingest.parser import LogEvent

# ---------- masking regexes (applied in order) ----------

# UUIDs: 8-4-4-4-12 hex pattern (must come before generic hex)
_RE_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Hex tokens of 4+ chars (request IDs, hashes, etc.)
_RE_HEX = re.compile(r"\b[0-9a-fA-F]{4,}\b")

# IPv4 addresses
_RE_IP = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}\b"
)

# Path segments that look like numeric IDs: /patients/8421 -> /patients/<ID>
_RE_PATH_ID = re.compile(r"(?<=/)\d+(?=/|$)")

# Durations and numbers: standalone digits, optionally with unit suffix
# e.g. 5000ms, 42, 50, 300s — but not words like "PID"
_RE_NUMBER = re.compile(r"\b\d+(?:\.\d+)?(?:ms|s|m|h|KB|MB|GB|B)?\b")

# Collapse multiple whitespace into single space
_RE_WHITESPACE = re.compile(r"\s+")


def _mask_text(text: str) -> str:
    """Apply masking rules to a text string in the correct order."""
    # 1. UUIDs -> <ID>
    result = _RE_UUID.sub("<ID>", text)
    # 2. Hex tokens -> <ID>
    result = _RE_HEX.sub("<ID>", result)
    # 3. IP addresses -> <IP>
    result = _RE_IP.sub("<IP>", result)
    # 4. Path IDs -> <ID>
    result = _RE_PATH_ID.sub("<ID>", result)
    # 5. Numbers/durations -> <N>
    result = _RE_NUMBER.sub("<N>", result)
    # 6. Collapse whitespace
    result = _RE_WHITESPACE.sub(" ", result).strip()
    return result


def make_signature(event: "LogEvent") -> Tuple[str, str]:
    """Compute a stable error signature from a LogEvent.

    Parameters
    ----------
    event : LogEvent
        A parsed log event (from parser.py).

    Returns
    -------
    tuple[str, str]
        (signature_id, masked_template) where signature_id is the first
        10 hex characters of the SHA-1 hash of masked_template.
    """
    # Source text: event.message if present, otherwise fallback to HTTP status
    if event.message:
        source = event.message
    else:
        status = event.status if event.status is not None else 0
        source = f"HTTP {status}"

    masked_template = _mask_text(source)

    # Stable hash: first 10 hex chars of SHA-1
    sig_hash = hashlib.sha1(masked_template.encode("utf-8")).hexdigest()[:10]

    return (sig_hash, masked_template)
