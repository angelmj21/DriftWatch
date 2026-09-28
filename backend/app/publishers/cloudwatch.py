"""Best-effort batched CloudWatch Logs and metric publishing for alerts."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from uuid import uuid4

import boto3

from ..alerts.schema import Alert
from ..config import settings


logger = logging.getLogger(__name__)
_BATCH_SIZE = 10
_FLUSH_INTERVAL_S = 1.0
_METRIC_BATCH_SIZE = 20
_MAX_ATTEMPTS = 3
_INITIAL_BACKOFF_S = 0.5


class CloudWatchPublisher:
    def __init__(self) -> None:
        self._log_group = settings.cw_log_group
        self._metric_namespace = settings.cw_metric_namespace
        self._log_stream = f"alerts-{uuid4().hex}"
        self._configured = bool(self._log_group and self._metric_namespace)
        self._warned_unconfigured = False
        self._warned_clients = False
        self._logs_ready = False
        self._queue: list[Alert] = []
        self._queue_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._flush_task: asyncio.Task[None] | None = None
        self._logs_client = None
        self._cloudwatch_client = None

        try:
            session = boto3.Session(region_name=settings.aws_region)
            self._logs_client = session.client("logs")
            self._cloudwatch_client = session.client("cloudwatch")
        except Exception:
            logger.exception("Could not create CloudWatch clients; publishing is disabled")

    async def publish(self, alert: Alert) -> None:
        if not self._configured:
            if not self._warned_unconfigured:
                logger.warning(
                    "CloudWatch log group or metric namespace is not configured; publishing is disabled"
                )
                self._warned_unconfigured = True
            return

        if self._logs_client is None or self._cloudwatch_client is None:
            if not self._warned_clients:
                logger.warning("CloudWatch clients are unavailable; alerts will not be published")
                self._warned_clients = True
            return

        try:
            batch: list[Alert] = []
            async with self._queue_lock:
                self._queue.append(alert)
                if len(self._queue) >= _BATCH_SIZE:
                    batch = self._queue
                    self._queue = []
                if self._queue and (
                    self._flush_task is None or self._flush_task.done()
                ):
                    self._flush_task = asyncio.create_task(self._flush_after_delay())

            if batch:
                await self._send_batch(batch)
        except Exception:
            logger.exception("CloudWatch publisher failed to queue alert %s", alert.id)

    async def _flush_after_delay(self) -> None:
        try:
            await asyncio.sleep(_FLUSH_INTERVAL_S)
            async with self._queue_lock:
                batch = self._queue
                self._queue = []
                self._flush_task = None
            if batch:
                await self._send_batch(batch)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("CloudWatch timed batch flush failed")

    async def _send_batch(self, alerts: list[Alert]) -> None:
        if not alerts:
            return

        async with self._send_lock:
            if not self._logs_ready:
                self._logs_ready = await self._retry(
                    self._ensure_log_destination_sync,
                    "create CloudWatch log destination",
                )

            ordered_alerts = sorted(alerts, key=lambda item: item.updated_at)
            if self._logs_ready:
                log_events = [
                    {
                        "timestamp": int(alert.updated_at.timestamp() * 1000),
                        "message": alert.to_json(),
                    }
                    for alert in ordered_alerts
                ]
                await self._retry(
                    self._put_log_events_sync,
                    "put CloudWatch log events",
                    logEvents=log_events,
                )

            metric_data = [
                metric
                for alert in ordered_alerts
                for metric in self._metric_data(alert)
            ]
            for start in range(0, len(metric_data), _METRIC_BATCH_SIZE):
                await self._retry(
                    self._put_metric_data_sync,
                    "put CloudWatch metrics",
                    MetricData=metric_data[start : start + _METRIC_BATCH_SIZE],
                )

    async def _retry(self, operation, operation_name: str, **kwargs) -> bool:
        for attempt in range(_MAX_ATTEMPTS):
            try:
                await asyncio.to_thread(operation, **kwargs)
                return True
            except Exception:
                if attempt + 1 == _MAX_ATTEMPTS:
                    logger.exception(
                        "Failed to %s after %s attempts",
                        operation_name,
                        _MAX_ATTEMPTS,
                    )
                    return False
                delay_s = _INITIAL_BACKOFF_S * (2**attempt)
                logger.warning(
                    "Failed to %s; retrying in %.1f seconds",
                    operation_name,
                    delay_s,
                    exc_info=True,
                )
                await asyncio.sleep(delay_s)
        return False

    def _ensure_log_destination_sync(self) -> None:
        try:
            self._logs_client.create_log_group(logGroupName=self._log_group)
        except Exception as exc:
            if self._error_code(exc) != "ResourceAlreadyExistsException":
                raise

        try:
            self._logs_client.create_log_stream(
                logGroupName=self._log_group,
                logStreamName=self._log_stream,
            )
        except Exception as exc:
            if self._error_code(exc) != "ResourceAlreadyExistsException":
                raise

    def _put_log_events_sync(self, **kwargs) -> None:
        self._logs_client.put_log_events(
            logGroupName=self._log_group,
            logStreamName=self._log_stream,
            **kwargs,
        )

    def _put_metric_data_sync(self, **kwargs) -> None:
        self._cloudwatch_client.put_metric_data(
            Namespace=self._metric_namespace,
            **kwargs,
        )

    @staticmethod
    def _error_code(exc: Exception) -> str | None:
        response = getattr(exc, "response", None)
        if not isinstance(response, dict):
            return None
        error = response.get("Error", {})
        if not isinstance(error, dict):
            return None
        code = error.get("Code")
        return code if isinstance(code, str) else None

    @staticmethod
    def _metric_data(alert: Alert) -> list[dict]:
        timestamp: datetime = alert.updated_at.astimezone(timezone.utc)
        dimensions = [{"Name": "Severity", "Value": alert.severity}]
        return [
            {
                "MetricName": "AlertCount",
                "Dimensions": dimensions,
                "Timestamp": timestamp,
                "Value": 1.0,
                "Unit": "Count",
            },
            {
                "MetricName": "ObservedErrorRate",
                "Dimensions": dimensions,
                "Timestamp": timestamp,
                "Value": alert.observed,
                "Unit": "Percent",
            },
        ]