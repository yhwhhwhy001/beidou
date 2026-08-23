"""告警通道 — JSONL 文件 + Webhook 通知。

P0 (CRITICAL/LOCKDOWN) 永不抑制，立即发送。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import urllib.request
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from beidou_observability.telemetry import (
    AlertSeverity,
    AlertSuppressor,
    AutoAction,
    Incident,
)
from beidou_reporting.engine import ReportGenerator

logger = logging.getLogger(__name__)


class AlertDispatcher:
    """多渠道告警分发器。"""

    def __init__(
        self,
        webhook_url: str = "",
        alerts_file: str = "evidence/beidou_alerts.jsonl",
        delivery_file: str | None = None,
        max_delivery_attempts: int = 5,
    ) -> None:
        if max_delivery_attempts < 1:
            raise ValueError("max_delivery_attempts must be positive")
        self._webhook_url = webhook_url
        self._alerts_file = alerts_file
        self._delivery_file = Path(delivery_file or f"{alerts_file}.delivery.jsonl")
        self._max_delivery_attempts = max_delivery_attempts
        self._suppressor = AlertSuppressor(window_seconds=300.0)
        self._report_generator = ReportGenerator()
        self._lock = threading.Lock()
        self._active_incidents: dict[str, Incident] = {}
        self._alert_count: dict[str, int] = {}
        self._tz = timezone(__import__("datetime").timedelta(hours=8))
        self._start_time = datetime.now(self._tz)
        self._portfolio_provider: Callable[[], str] | None = None
        self._delivery_state: dict[str, dict[str, Any]] = {}
        self._delivery_load_errors: list[str] = []
        self._delivery_persistence_error = False
        self._delivery_io_lock = threading.Lock()
        self._load_delivery_state()

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
    ) -> Incident:
        """创建并分发事故告警。相同 category+title 的事故自动去重，更新已有事故。

        ``gap_reasons``: 保护覆盖缺口 reason 明细 (可观测性修复), 随
        incident 贯通到 get_active_incidents, 供 supervisor A/B 分流消费。
        """
        # Deduplicate: if an active incident with the same category+title exists,
        # update it instead of creating a new one.
        dedup_key = f"{category}:{title}"
        with self._lock:
            for existing_inc in list(self._active_incidents.values()):
                existing_key = f"{getattr(existing_inc, 'root_cause_category', '')}:{existing_inc.title}"
                if existing_key == dedup_key:
                    # Update the existing incident in place
                    existing_inc.description = description
                    existing_inc.gap_reasons = list(gap_reasons or [])
                    existing_inc._last_updated = datetime.now(timezone.utc)  # M21: 运行时字段
                    return existing_inc

        incident_id = f"inc-{datetime.now(self._tz).strftime('%Y%m%d%H%M%S%f')}-{category}"

        incident = Incident(
            incident_id=incident_id,
            severity=severity,
            title=title,
            description=description,
            root_cause_category=category,
            auto_action=auto_action or AutoAction.ALERT,
            gap_reasons=list(gap_reasons or []),
        )

        # P0 永不抑制
        if severity in (AlertSeverity.CRITICAL, AlertSeverity.LOCKDOWN):
            self._dispatch(incident)
            return incident

        # 检查抑制 (BD-FIX: 传递 category/title 用于指纹去重)
        if self._suppressor.should_suppress(severity, f"{category}:{title}", category=category, title=title):
            return incident

        self._dispatch(incident)
        return incident

    def _dispatch(self, incident: Incident) -> None:
        with self._lock:
            self._active_incidents[incident.incident_id] = incident
            self._alert_count[incident.severity.value] = self._alert_count.get(incident.severity.value, 0) + 1

        # 写入 JSONL 文件
        self._write_to_file(incident)

        # Webhook
        if self._webhook_url:
            # Queue before attempting network delivery.  A process crash
            # between these two operations leaves a replayable PENDING fact.
            self._record_delivery_event(incident, "PENDING", attempts=0)
            self._send_webhook(incident)

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

    def _write_to_file(self, incident: Incident) -> None:
        record = {
            "incident_id": incident.incident_id,
            "severity": incident.severity.value,
            "title": incident.title,
            "description": incident.description,
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
            with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310 - webhook URL is policy-validated
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
                with self._lock:
                    self._delivery_state[str(record.get("incident_id", ""))] = {
                        **record,
                        "attempts": 0,
                        "status": "PENDING",
                    }
            self._send_webhook(incident)
            retried += 1
        return retried

    def get_delivery_health(self) -> dict[str, Any]:
        """Return durable webhook delivery health; UNKNOWN is explicit."""
        with self._lock:
            records = list(self._delivery_state.values())
            statuses = [record.get("status") for record in records]
            return {
                "configured": bool(self._webhook_url),
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
                auto_action=AutoAction(str(record["auto_action"])),
                detected_at=detected_at.astimezone(timezone.utc),
            )
        except (KeyError, TypeError, ValueError) as exc:
            logger.error("cannot reconstruct pending alert incident: %s", type(exc).__name__)
            return None

    def resolve_incident(self, incident_id: str) -> None:
        with self._lock:
            incident = self._active_incidents.get(incident_id)
            if incident:
                incident.resolve("resolved")
                del self._active_incidents[incident_id]

    def get_active_incidents(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "incident_id": i.incident_id,
                    "severity": i.severity.value,
                    "title": i.title,
                    "status": i.status.value,
                    "detected_at": i.detected_at.isoformat(),
                    "description": str(getattr(i, "description", "") or "")[:300],
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
