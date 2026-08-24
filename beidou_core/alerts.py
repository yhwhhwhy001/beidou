"""告警通道 — 统一 Incident JSONL 事件 + 异步 Webhook 队列。

P0 (CRITICAL/LOCKDOWN) 永不抑制，并在本地事件持久化后立即入队。
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import threading
import time
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from beidou_observability.monitoring.incident_manager import IncidentManager
from beidou_observability.telemetry import (
    AlertSeverity,
    AlertSuppressor,
    AutoAction,
    Incident,
)
from beidou_reporting.engine import ReportGenerator

logger = logging.getLogger(__name__)


class AlertDispatcher:
    """多渠道告警分发器；不执行控制动作。"""

    def __init__(
        self,
        webhook_url: str = "",
        alerts_file: str = "evidence/beidou_alerts.jsonl",
        delivery_file: str | None = None,
        max_delivery_attempts: int = 5,
        webhook_timeout: float = 5.0,
        incident_manager: IncidentManager | None = None,
        start_webhook_worker: bool = True,
    ) -> None:
        if max_delivery_attempts < 1:
            raise ValueError("max_delivery_attempts must be positive")
        if webhook_timeout <= 0:
            raise ValueError("webhook_timeout must be positive")
        self._webhook_url = webhook_url
        self._webhook_timeout = float(webhook_timeout)
        self._alerts_file = alerts_file
        self._delivery_file = Path(delivery_file or f"{alerts_file}.delivery.jsonl")
        self._max_delivery_attempts = max_delivery_attempts
        self._suppressor = AlertSuppressor(window_seconds=300.0)
        self._report_generator = ReportGenerator()
        self._lock = threading.Lock()
        self._incident_manager = incident_manager or IncidentManager(event_log_path=alerts_file)
        self._active_incidents: dict[str, Incident] = self._incident_manager.alert_incidents
        self._alert_count: dict[str, int] = {}
        self._tz = timezone(__import__("datetime").timedelta(hours=8))
        self._start_time = datetime.now(self._tz)
        self._portfolio_provider: Callable[[], str] | None = None
        self._delivery_state: dict[str, dict[str, Any]] = {}
        self._delivery_load_errors: list[str] = []
        self._delivery_persistence_error = False
        self._delivery_io_lock = threading.Lock()
        self._webhook_queue: queue.Queue[str] = queue.Queue()
        self._queued_delivery_ids: set[str] = set()
        self._queued_incidents: dict[str, Incident] = {}
        self._webhook_stop = threading.Event()
        self._webhook_thread: threading.Thread | None = None
        self._webhook_drain_on_close = True
        self._load_delivery_state()
        if self._webhook_url and start_webhook_worker:
            self._start_webhook_worker()

    def set_portfolio_provider(self, fn: Callable[[], str]) -> None:
        """注入持仓摘要提供器，webhook 推送时追加到描述末尾。"""
        self._portfolio_provider = fn

    def send_incident(
        self,
        severity: AlertSeverity,
        title: str,
        description: str,
        auto_action: AutoAction | None = None,
        category: str = "runtime",
        gap_reasons: list[str] | None = None,
        source_check_id: str = "",
        entity_type: str = "",
        entity_id: str = "",
        correlation_id: str | None = None,
        evidence_hash: str = "",
        dedupe_key: str | None = None,
    ) -> Incident:
        """创建并分发事故告警。

        事故生命周期由 ``monitoring.IncidentManager`` 统一管理；此类只做
        入口编排、抑制和渠道投递。``auto_action`` 是由 severity 派生的
        声明字段，不会在 Dispatcher 内执行控制动作。

        ``gap_reasons``: 保护覆盖缺口 reason 明细, 随 incident 贯通到
        get_active_incidents, 供 supervisor A/B 分流消费。
        """
        effective_dedupe_key = dedupe_key or f"{category}:{title}"
        effective_action = auto_action or AutoAction.ALERT

        # Preserve the historical ordering: an existing active incident is
        # updated before suppression is considered.
        existing = self._incident_manager.get_active_alert(effective_dedupe_key)
        if existing is not None:
            incident, _created = self._incident_manager.create_or_dedupe_alert(
                severity,
                title,
                description,
                auto_action=effective_action,
                category=category,
                dedupe_key=effective_dedupe_key,
                source_check_id=source_check_id,
                entity_type=entity_type,
                entity_id=entity_id,
                correlation_id=correlation_id,
                evidence_hash=evidence_hash,
                gap_reasons=gap_reasons,
            )
            return incident

        # P0 永不抑制
        # 检查抑制 (BD-FIX: 传递 category/title 用于指纹去重)
        if severity not in (AlertSeverity.CRITICAL, AlertSeverity.LOCKDOWN) and self._suppressor.should_suppress(
            severity, effective_dedupe_key, category=category, title=title
        ):
            return Incident(
                incident_id=f"suppressed-{datetime.now(self._tz).strftime('%Y%m%d%H%M%S%f')}",
                severity=severity,
                title=title,
                description=description,
                root_cause_category=category,
                dedupe_key=effective_dedupe_key,
                source_check_id=source_check_id,
                entity_type=entity_type,
                entity_id=entity_id,
                correlation_id=correlation_id,
                evidence_hash=evidence_hash,
                auto_action=effective_action,
                gap_reasons=list(gap_reasons or []),
            )

        incident, created = self._incident_manager.create_or_dedupe_alert(
            severity,
            title,
            description,
            auto_action=effective_action,
            category=category,
            dedupe_key=effective_dedupe_key,
            source_check_id=source_check_id,
            entity_type=entity_type,
            entity_id=entity_id,
            correlation_id=correlation_id,
            evidence_hash=evidence_hash,
            gap_reasons=gap_reasons,
        )
        if created:
            self._dispatch(incident)
        return incident

    def _dispatch(self, incident: Incident) -> None:
        with self._lock:
            self._active_incidents[incident.incident_id] = incident
            self._alert_count[incident.severity.value] = self._alert_count.get(incident.severity.value, 0) + 1

        # The IncidentManager has already durably recorded OPEN.  Persist a
        # replayable delivery fact before handing the network work to a worker.
        if self._webhook_url:
            self._record_delivery_event(incident, "PENDING", attempts=0)
            self._enqueue_webhook(incident)

        # 生成事故报告
        try:
            self._report_generator.generate_incident_report(
                incident_id=incident.incident_id,
                title=incident.title,
                description=incident.description,
                severity=incident.severity.value,
                detected_at=incident.detected_at,
                auto_action=incident.auto_action.value,
            )
        except Exception as exc:
            logger.error("incident report generation failed for %s: %s", incident.incident_id, type(exc).__name__)

    def _start_webhook_worker(self) -> None:
        if self._webhook_thread is not None and self._webhook_thread.is_alive():
            return
        self._webhook_stop.clear()
        self._webhook_drain_on_close = True
        self._webhook_thread = threading.Thread(
            target=self._webhook_worker_loop,
            name="beidou-alert-webhook",
            daemon=True,
        )
        self._webhook_thread.start()

    def _enqueue_webhook(self, incident: Incident) -> bool:
        with self._lock:
            if incident.incident_id in self._queued_delivery_ids:
                return False
            self._queued_delivery_ids.add(incident.incident_id)
            self._queued_incidents[incident.incident_id] = incident
        self._webhook_queue.put(incident.incident_id)
        return True

    def _webhook_worker_loop(self) -> None:
        while True:
            try:
                incident_id = self._webhook_queue.get(timeout=0.2)
            except queue.Empty:
                if self._webhook_stop.is_set():
                    return
                continue
            try:
                if self._webhook_stop.is_set() and not self._webhook_drain_on_close:
                    continue
                with self._lock:
                    incident = self._queued_incidents.get(incident_id) or self._active_incidents.get(incident_id)
                if incident is None:
                    incident = self._incident_manager.get_alert_by_id(incident_id)
                if incident is not None:
                    self._send_webhook(incident)
            except Exception as exc:
                logger.error("webhook worker failed for alert %s: %s", incident_id, type(exc).__name__)
            finally:
                with self._lock:
                    self._queued_delivery_ids.discard(incident_id)
                    self._queued_incidents.pop(incident_id, None)
                self._webhook_queue.task_done()

    def flush_delivery(self, *, timeout: float = 5.0) -> bool:
        """Wait for queued webhook work without changing durable retry state."""
        deadline = time.monotonic() + max(0.0, float(timeout))
        while time.monotonic() < deadline:
            if self._webhook_queue.unfinished_tasks == 0:
                return True
            time.sleep(0.01)
        return self._webhook_queue.unfinished_tasks == 0

    def close(self, *, flush: bool = False, timeout: float = 5.0) -> None:
        """Stop the daemon worker; pending work remains replayable when not flushed."""
        if flush:
            self.flush_delivery(timeout=timeout)
        self._webhook_drain_on_close = bool(flush)
        self._webhook_stop.set()
        worker = self._webhook_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=max(0.0, float(timeout)))
        self._webhook_thread = None

    def _write_to_file(self, incident: Incident) -> None:
        record = {
            "incident_id": incident.incident_id,
            "severity": incident.severity.value,
            "title": incident.title,
            "description": incident.description,
            "category": str(getattr(incident, "root_cause_category", "") or "runtime"),
            "source_check_id": str(getattr(incident, "source_check_id", "") or ""),
            "entity_type": str(getattr(incident, "entity_type", "") or ""),
            "entity_id": str(getattr(incident, "entity_id", "") or ""),
            "correlation_id": str(getattr(incident, "correlation_id", "") or ""),
            "evidence_hash": str(getattr(incident, "evidence_hash", "") or ""),
            "auto_action": incident.auto_action.value,
            "detected_at": incident.detected_at.isoformat(),
            "status": incident.status.value,
        }
        alert_path = Path(self._alerts_file)
        alert_path.parent.mkdir(parents=True, exist_ok=True)
        with alert_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _send_webhook(self, incident: Incident) -> bool:
        """发送 webhook 告警。支持飞书/Server酱/PushPlus/企业微信/通用JSON。"""
        url = self._webhook_url
        with self._lock:
            previous = self._delivery_state.get(incident.incident_id, {})
            attempts = int(previous.get("attempts", 0)) + 1
        ts = incident.detected_at.strftime("%m-%d %H:%M")
        title = f"[{incident.severity.value}] {incident.title}"
        # 正文：事件+操作+时间
        body = f"**{incident.description}**\n操作: {incident.auto_action.value}  |  {ts} UTC+8"
        # 追加持仓
        portfolio = ""
        if self._portfolio_provider:
            try:
                portfolio = self._portfolio_provider()
            except Exception as exc:
                logger.warning("portfolio provider failed for alert %s: %s", incident.incident_id, type(exc).__name__)

        try:
            if "open.feishu.cn" in url or "open.larksuite.com" in url:
                # 飞书卡片
                elements: list[dict[str, Any]] = [
                    {"tag": "markdown", "content": body},
                ]
                if portfolio:
                    elements.append({"tag": "hr"})
                    elements.append({"tag": "markdown", "content": portfolio})
                elements.append(
                    {
                        "tag": "note",
                        "elements": [{"tag": "plain_text", "content": f"北斗 V2.0 | {incident.incident_id} | {ts}"}],
                    }
                )
                card_payload: dict[str, Any] = {
                    "msg_type": "interactive",
                    "card": {
                        "header": {
                            "title": {"content": title, "tag": "plain_text"},
                            "template": "red" if incident.severity.value in ("CRITICAL", "LOCKDOWN") else "yellow",
                        },
                        "elements": elements,
                    },
                }
                payload = json.dumps(card_payload).encode()
            elif "sctapi.ftqq.com" in url:
                payload = json.dumps({"title": title, "desp": f"{body}\n\n{portfolio}"}).encode()
            elif "pushplus.plus" in url:
                payload = json.dumps(
                    {
                        "token": url.split("token=")[-1] if "token=" in url else "",
                        "title": title,
                        "content": f"{body}\n\n{portfolio}",
                    }
                ).encode()
            elif "qyapi.weixin.qq.com" in url:
                payload = json.dumps(
                    {
                        "msgtype": "markdown",
                        "markdown": {"content": f"## {title}\n{body}\n\n{portfolio}"},
                    }
                ).encode()
            else:
                payload = json.dumps(
                    {
                        "incident_id": incident.incident_id,
                        "severity": incident.severity.value,
                        "title": incident.title,
                        "description": incident.description,
                        "auto_action": incident.auto_action.value,
                        "detected_at": incident.detected_at.isoformat(),
                        "portfolio": portfolio,
                    }
                ).encode()

            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=self._webhook_timeout) as response:  # nosec B310 - webhook URL is policy-validated
                http_status = int(getattr(response, "status", 200))
                if http_status < 200 or http_status >= 300:
                    raise RuntimeError(f"webhook HTTP status {http_status}")
            self._record_delivery_event(incident, "DELIVERED", attempts=attempts)
            return True
        except Exception as exc:
            logger.error("webhook delivery failed for alert %s: %s", incident.incident_id, type(exc).__name__)
            delivery_status = "DEAD_LETTER" if attempts >= self._max_delivery_attempts else "FAILED"
            backoff_seconds = min(300.0, float(2 ** min(attempts, 8)))
            self._record_delivery_event(
                incident,
                delivery_status,
                attempts=attempts,
                error=type(exc).__name__,
                next_retry_at=datetime.now(timezone.utc).timestamp() + backoff_seconds,
            )
            return False

    def retry_pending(self, *, max_items: int = 10, now: float | None = None) -> int:
        """Retry due webhook deliveries from the durable sidecar.

        The supervisor may call this once per monitoring cycle.  Missing or
        corrupt delivery evidence is never treated as delivered; it remains
        visible through :meth:`get_delivery_health`.
        """
        if not self._webhook_url or max_items < 1:
            return 0
        current_time = datetime.now(timezone.utc).timestamp() if now is None else float(now)
        with self._lock:
            candidates = [
                dict(record)
                for record in self._delivery_state.values()
                # BD-FIX: DEAD_LETTER 可安全重试（幂等键在，C4 审查：
                # 旧逻辑死信永不重试 → alert_delivery P0 永久 FAIL →
                # LOCKED → 跨重启残留 → 崩溃循环）。DEAD_LETTER 的
                # attempts 已打满，重试前重置预算。
                if float(record.get("next_retry_at", 0.0)) <= current_time
                and str(record.get("incident_id", "")) not in self._queued_delivery_ids
                and (
                    (
                        record.get("status") in {"PENDING", "FAILED"}
                        and int(record.get("attempts", 0)) < self._max_delivery_attempts
                    )
                    or record.get("status") == "DEAD_LETTER"
                )
            ][:max_items]

        retried = 0
        for record in candidates:
            incident = self._active_incidents.get(str(record.get("incident_id", "")))
            if incident is None:
                incident = self._incident_from_delivery(record)
            if incident is None:
                continue
            if str(record.get("status", "")) == "DEAD_LETTER":
                # 重试死信前重置 attempts，重新获得完整预算
                self._record_delivery_event(incident, "PENDING", attempts=0)
            elif str(record.get("status", "")) == "FAILED":
                self._record_delivery_event(
                    incident,
                    "PENDING",
                    attempts=int(record.get("attempts", 0)),
                    next_retry_at=0.0,
                )
            if self._enqueue_webhook(incident):
                retried += 1
        return retried

    def get_delivery_health(self) -> dict[str, Any]:
        """Return durable webhook delivery health; UNKNOWN is explicit."""
        with self._lock:
            records = list(self._delivery_state.values())
            statuses = [record.get("status") for record in records]
            return {
                "configured": bool(self._webhook_url),
                "timeout_seconds": self._webhook_timeout,
                "queued": self._webhook_queue.unfinished_tasks,
                "pending": sum(status in {"PENDING", "FAILED"} for status in statuses),
                "failed": sum(status == "FAILED" for status in statuses),
                "dead_letter": sum(status == "DEAD_LETTER" for status in statuses),
                "delivered": sum(status == "DELIVERED" for status in statuses),
                "critical_pending": sum(
                    record.get("severity") in {AlertSeverity.CRITICAL.value, AlertSeverity.LOCKDOWN.value}
                    and record.get("status") in {"PENDING", "FAILED"}
                    for record in records
                ),
                "unknown": int(bool(self._delivery_load_errors or self._delivery_persistence_error)),
                "load_errors": len(self._delivery_load_errors),
                "persistence_error": int(self._delivery_persistence_error),
            }

    def _record_delivery_event(
        self,
        incident: Incident,
        status: str,
        *,
        attempts: int,
        error: str = "",
        next_retry_at: float = 0.0,
    ) -> None:
        record: dict[str, Any] = {
            "incident_id": incident.incident_id,
            "severity": incident.severity.value,
            "title": incident.title,
            "description": incident.description,
            "category": str(getattr(incident, "root_cause_category", "") or "runtime"),
            "source_check_id": str(getattr(incident, "source_check_id", "") or ""),
            "entity_type": str(getattr(incident, "entity_type", "") or ""),
            "entity_id": str(getattr(incident, "entity_id", "") or ""),
            "correlation_id": str(getattr(incident, "correlation_id", "") or ""),
            "evidence_hash": str(getattr(incident, "evidence_hash", "") or ""),
            "auto_action": incident.auto_action.value,
            "detected_at": incident.detected_at.isoformat(),
            "status": status,
            "attempts": attempts,
            "error": error,
            "next_retry_at": next_retry_at,
            "event_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            with self._delivery_io_lock:
                self._delivery_file.parent.mkdir(parents=True, exist_ok=True)
                with self._delivery_file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                    handle.flush()
                    os.fsync(handle.fileno())
        except OSError as exc:
            # The incident itself is already persisted separately, but the
            # delivery fact is now UNKNOWN.  Do not claim it was sent.
            self._delivery_persistence_error = True
            logger.critical("alert delivery state persistence failed: %s", type(exc).__name__)
        with self._lock:
            self._delivery_state[incident.incident_id] = record

    def _load_delivery_state(self) -> None:
        if not self._delivery_file.exists():
            return
        try:
            with self._delivery_file.open(encoding="utf-8") as handle:
                for line_number, line in enumerate(handle, start=1):
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        incident_id = record.get("incident_id")
                        status = record.get("status")
                        if not isinstance(incident_id, str) or status not in {
                            "PENDING",
                            "FAILED",
                            "DELIVERED",
                            "DEAD_LETTER",
                        }:
                            raise ValueError("invalid delivery record")
                        self._delivery_state[incident_id] = dict(record)
                    except (TypeError, ValueError, json.JSONDecodeError) as exc:
                        self._delivery_load_errors.append(f"line {line_number}: {type(exc).__name__}")
        except OSError as exc:
            self._delivery_load_errors.append(f"file: {type(exc).__name__}")

    @staticmethod
    def _incident_from_delivery(record: dict[str, Any]) -> Incident | None:
        try:
            detected_at = datetime.fromisoformat(str(record["detected_at"]).replace("Z", "+00:00"))
            if detected_at.tzinfo is None:
                detected_at = detected_at.replace(tzinfo=timezone.utc)
            return Incident(
                incident_id=str(record["incident_id"]),
                severity=AlertSeverity(str(record["severity"])),
                title=str(record["title"]),
                description=str(record["description"]),
                root_cause_category=str(record.get("category", "runtime")),
                dedupe_key=str(record.get("dedupe_key") or f"{record.get('category', 'runtime')}:{record['title']}"),
                source_check_id=str(record.get("source_check_id", "")),
                entity_type=str(record.get("entity_type", "")),
                entity_id=str(record.get("entity_id", "")),
                correlation_id=str(record.get("correlation_id", "")) or None,
                evidence_hash=str(record.get("evidence_hash", "")),
                auto_action=AutoAction(str(record["auto_action"])),
                detected_at=detected_at.astimezone(timezone.utc),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.error("cannot reconstruct pending alert incident: %s", type(exc).__name__)
            return None

    def resolve_incident(
        self,
        incident_id: str,
        *,
        resolution: str = "resolved",
        evidence_hash: str = "",
    ) -> None:
        self._incident_manager.resolve_alert(
            incident_id,
            resolution=resolution,
            evidence_hash=evidence_hash,
        )

    def bridge_monitoring_check(self, result: Any) -> Incident | None:
        """Open or resolve a P0 Incident directly from a deep-monitor result."""
        severity = getattr(getattr(result, "severity", None), "value", getattr(result, "severity", ""))
        if str(severity) != "P0":
            return None
        status = str(getattr(getattr(result, "status", None), "value", getattr(result, "status", "UNKNOWN")))
        evidence = dict(getattr(result, "evidence", None) or {})
        check_id = str(getattr(result, "check_id", "") or "monitoring.unknown")
        entity_type = str(evidence.get("entity_type", getattr(result, "entity_type", "")) or "")
        entity_id = str(evidence.get("entity_id", getattr(result, "entity_id", "")) or "")
        correlation_id = str(evidence.get("correlation_id", getattr(result, "correlation_id", "")) or "")
        evidence_hash = str(evidence.get("evidence_hash", "") or "")
        if not evidence_hash:
            try:
                payload = result.to_dict()
            except AttributeError:
                payload = {
                    "check_id": check_id,
                    "status": status,
                    "severity": str(severity),
                    "message": str(getattr(result, "message", "")),
                    "evidence": evidence,
                }
            evidence_hash = hashlib.sha256(
                json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
            ).hexdigest()
        dedupe_key = f"monitoring:{check_id}:{entity_type}:{entity_id}"
        if status in {"FAIL", "UNKNOWN"}:
            gap_reasons = [str(item.get("reason", "")) for item in (evidence.get("gaps") or []) if item.get("reason")]
            return self.send_incident(
                AlertSeverity.CRITICAL,
                str(getattr(result, "name", "") or check_id),
                str(getattr(result, "message", "") or status),
                category="monitoring",
                source_check_id=check_id,
                entity_type=entity_type,
                entity_id=entity_id,
                correlation_id=correlation_id or None,
                evidence_hash=evidence_hash,
                dedupe_key=dedupe_key,
                gap_reasons=gap_reasons,
            )
        if status == "PASS":
            existing = self._incident_manager.get_active_alert(dedupe_key)
            if existing is not None:
                self.resolve_incident(
                    existing.incident_id,
                    resolution=f"monitoring check recovered: {check_id}",
                    evidence_hash=evidence_hash,
                )
        return None

    def get_active_incidents(self) -> list[dict[str, Any]]:
        if self._incident_manager.get_alert_load_errors():
            raise RuntimeError("INCIDENT_STORE_UNKNOWN")
        with self._lock:
            return [
                {
                    "incident_id": i.incident_id,
                    "severity": i.severity.value,
                    "title": i.title,
                    "status": i.status.value,
                    "detected_at": i.detected_at.isoformat(),
                    "description": str(getattr(i, "description", "") or "")[:300],
                    "category": str(getattr(i, "root_cause_category", "") or "runtime"),
                    "dedupe_key": str(getattr(i, "dedupe_key", "") or ""),
                    "source_check_id": str(getattr(i, "source_check_id", "") or ""),
                    "entity_type": str(getattr(i, "entity_type", "") or ""),
                    "entity_id": str(getattr(i, "entity_id", "") or ""),
                    "correlation_id": str(getattr(i, "correlation_id", "") or ""),
                    "evidence_hash": str(getattr(i, "evidence_hash", "") or ""),
                    "auto_action": i.auto_action.value,
                    "gap_reasons": [str(r) for r in (getattr(i, "gap_reasons", None) or [])],
                }
                for i in self._active_incidents.values()
            ]

    def get_alert_stats(self) -> dict[str, int]:
        with self._lock:
            stats = {
                "total": sum(self._alert_count.values()),
                "active": len(self._active_incidents),
                **self._alert_count,
            }
        delivery = self.get_delivery_health()
        stats.update(
            {
                "webhook_pending": int(delivery["pending"]),
                "webhook_failed": int(delivery["failed"]),
                "webhook_dead_letter": int(delivery["dead_letter"]),
                "webhook_unknown": int(delivery["unknown"]),
                "webhook_critical_pending": int(delivery["critical_pending"]),
            }
        )
        return stats

    def get_report_generator(self) -> ReportGenerator:
        return self._report_generator
