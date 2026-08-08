"""告警通道 — JSONL 文件 + Webhook 通知。

P0 (CRITICAL/LOCKDOWN) 永不抑制，立即发送。
"""

from __future__ import annotations

import contextlib
import json
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Any

from beidou_observability.telemetry import (
    AlertSeverity,
    AlertSuppressor,
    AutoAction,
    Incident,
)
from beidou_reporting.engine import ReportGenerator


class AlertDispatcher:
    """多渠道告警分发器。"""

    def __init__(self, webhook_url: str = "", alerts_file: str = "/tmp/beidou_alerts.jsonl") -> None:
        self._webhook_url = webhook_url
        self._alerts_file = alerts_file
        self._suppressor = AlertSuppressor(window_seconds=300.0)
        self._report_generator = ReportGenerator()
        self._lock = threading.Lock()
        self._active_incidents: dict[str, Incident] = {}
        self._alert_count: dict[str, int] = {}
        self._start_time = datetime.now(timezone.utc)
        self._portfolio_provider: Callable[[], str] | None = None

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
    ) -> Incident:
        """创建并分发事故告警。"""
        incident_id = f"inc-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{category}"

        incident = Incident(
            incident_id=incident_id,
            severity=severity,
            title=title,
            description=description,
            root_cause_category=category,
            auto_action=auto_action or AutoAction.ALERT,
        )

        # P0 永不抑制
        if severity in (AlertSeverity.CRITICAL, AlertSeverity.LOCKDOWN):
            self._dispatch(incident)
            return incident

        # 检查抑制
        if self._suppressor.should_suppress(severity, f"{category}:{title}"):
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
            self._send_webhook(incident)

        # 生成事故报告
        with contextlib.suppress(Exception):
            self._report_generator.generate_incident_report(
                incident_id=incident.incident_id,
                title=incident.title,
                description=incident.description,
                severity=incident.severity.value,
                detected_at=incident.detected_at,
                auto_action=incident.auto_action.value,
            )

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
        with open(self._alerts_file, "a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _send_webhook(self, incident: Incident) -> None:
        """发送 webhook 告警。自动识别推送服务类型。

        支持: Server酱 / PushPlus / 企业微信 / 飞书(Lark) / 通用 JSON
        """
        url = self._webhook_url
        title = f"[{incident.severity.value}] {incident.title}"
        desc = f"{incident.description}\n操作: {incident.auto_action.value}\n时间: {incident.detected_at.isoformat()}\nID: {incident.incident_id}"
        # 追加持仓摘要
        if self._portfolio_provider:
            try:
                portfolio = self._portfolio_provider()
                if portfolio:
                    desc += f"\n\n{portfolio}"
            except Exception:
                pass

        try:
            if "sctapi.ftqq.com" in url:
                payload = json.dumps({"title": title, "desp": desc}).encode()
            elif "pushplus.plus" in url:
                payload = json.dumps({
                    "token": url.split("token=")[-1] if "token=" in url else "",
                    "title": title, "content": desc,
                }).encode()
            elif "open.feishu.cn" in url or "open.larksuite.com" in url:
                # 飞书/Lark 机器人
                payload = json.dumps({
                    "msg_type": "interactive",
                    "card": {
                        "header": {
                            "title": {"content": title, "tag": "plain_text"},
                            "template": "red" if incident.severity.value == "CRITICAL" else "yellow",
                        },
                        "elements": [
                            {"tag": "markdown", "content": desc.replace("\n", "\n\n")},
                            {"tag": "note", "elements": [
                                {"tag": "plain_text", "content": f"北斗 V2.0 | {incident.incident_id}"}
                            ]},
                        ],
                    },
                }).encode()
            elif "qyapi.weixin.qq.com" in url:
                payload = json.dumps({
                    "msgtype": "markdown",
                    "markdown": {"content": f"## {title}\n{desc}"},
                }).encode()
            else:
                payload = json.dumps({
                    "incident_id": incident.incident_id,
                    "severity": incident.severity.value,
                    "title": incident.title, "description": incident.description,
                    "auto_action": incident.auto_action.value,
                    "detected_at": incident.detected_at.isoformat(),
                }).encode()

            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

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
                }
                for i in self._active_incidents.values()
            ]

    def get_alert_stats(self) -> dict[str, int]:
        with self._lock:
            return {
                "total": sum(self._alert_count.values()),
                "active": len(self._active_incidents),
                **self._alert_count,
            }

    def get_report_generator(self) -> ReportGenerator:
        return self._report_generator
