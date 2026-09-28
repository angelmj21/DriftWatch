"""Best-effort SNS delivery for alert lifecycle notifications."""
from __future__ import annotations

import asyncio
import logging

import boto3

from ..alerts.schema import Alert
from ..config import settings


logger = logging.getLogger(__name__)
_SEVERITY_RANK = {"INFO": 0, "WARN": 1, "CRITICAL": 2}
_MAX_ATTEMPTS = 3
_INITIAL_BACKOFF_S = 0.5


class SNSPublisher:
    def __init__(self) -> None:
        self._topic_arn = settings.sns_topic_arn
        self._minimum_severity = settings.sns_min_severity
        self._client = None
        self._warned_empty_topic = False
        self._open_sent: set[str] = set()
        self._highest_seen: dict[str, int] = {}

        try:
            self._client = boto3.client("sns", region_name=settings.aws_region)
        except Exception:
            logger.exception("Could not create the SNS client; SNS notifications are disabled")

    async def publish(self, alert: Alert) -> None:
        if not self._topic_arn:
            if not self._warned_empty_topic:
                logger.warning("SNS topic ARN is empty; SNS notifications are disabled")
                self._warned_empty_topic = True
            return

        if alert.status == "resolved" and alert.id not in self._open_sent:
            self._highest_seen.pop(alert.id, None)
            return

        severity_rank = _SEVERITY_RANK.get(alert.severity, -1)
        minimum_rank = _SEVERITY_RANK.get(self._minimum_severity, len(_SEVERITY_RANK))
        if severity_rank < minimum_rank:
            if alert.status == "open":
                self._highest_seen[alert.id] = max(
                    severity_rank, self._highest_seen.get(alert.id, severity_rank)
                )
            return

        if alert.status == "updated":
            previous_rank = self._highest_seen.get(alert.id)
            if previous_rank is None or severity_rank <= previous_rank:
                return
        elif alert.status == "open":
            if alert.id in self._open_sent:
                return
        elif alert.status != "resolved":
            return

        if self._client is None:
            logger.warning("SNS client is unavailable; skipped alert %s", alert.id)
            return

        try:
            subject = self._subject(alert)
            message = self._message(alert)
            for attempt in range(_MAX_ATTEMPTS):
                try:
                    await asyncio.to_thread(
                        self._client.publish,
                        TopicArn=self._topic_arn,
                        Subject=subject,
                        Message=message,
                    )
                    if alert.status == "open":
                        self._open_sent.add(alert.id)
                        self._highest_seen[alert.id] = max(
                            severity_rank,
                            self._highest_seen.get(alert.id, severity_rank),
                        )
                    elif alert.status == "resolved":
                        self._open_sent.discard(alert.id)
                        self._highest_seen.pop(alert.id, None)
                    else:
                        self._highest_seen[alert.id] = max(
                            severity_rank,
                            self._highest_seen.get(alert.id, severity_rank),
                        )
                    return
                except Exception:
                    if attempt + 1 == _MAX_ATTEMPTS:
                        logger.exception(
                            "SNS publish failed for alert %s after %s attempts",
                            alert.id,
                            _MAX_ATTEMPTS,
                        )
                        return
                    delay_s = _INITIAL_BACKOFF_S * (2**attempt)
                    logger.warning(
                        "SNS publish failed for alert %s; retrying in %.1f seconds",
                        alert.id,
                        delay_s,
                        exc_info=True,
                    )
                    await asyncio.sleep(delay_s)
        except Exception:
            logger.exception("SNS publisher failed to process alert %s", alert.id)

    @staticmethod
    def _subject(alert: Alert) -> str:
        services = ", ".join(alert.services)
        subject = (
            f"[DriftWatch][{alert.severity}] {services} error rate "
            f"{alert.observed:.1f}% (baseline {alert.baseline:.1f}%)."
        )
        return subject.encode("ascii", errors="replace").decode("ascii")[:100]

    @staticmethod
    def _message(alert: Alert) -> str:
        services = ", ".join(alert.services)
        return "\n".join(
            (
                alert.explanation,
                "",
                f"Incident: {alert.id}",
                f"Status: {alert.status}",
                f"Severity: {alert.severity}",
                f"Services: {services}",
                f"Observed error rate: {alert.observed:.1f}%",
                f"Learned baseline: {alert.baseline:.1f}%",
                f"Deviation: {alert.deviation:.1f} sigma",
                f"Slope: {alert.slope:.1f} percentage points per minute",
            )
        )