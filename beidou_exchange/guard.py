"""Local write guard: exact host allow-list, kill-switch file, reduce-only pass-through.

This is not an authorization system.  It is the last local check before a
mutating request leaves the process: a mainnet host is refused unless three
independent things all say yes (:class:`MainnetRelease`), and while the
kill-switch file exists only risk-reducing writes (cancel, reduce-only orders,
leverage/session control) are allowed.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ALLOWED_HOSTS: frozenset[str] = frozenset({"demo-fapi.binance.com", "testnet.binancefuture.com"})
# docs/MAINNET_READINESS.md §4.2: a SEPARATE set, never merged into ALLOWED_HOSTS.  A host here is let through
# only when `mainnet_refusal` finds nothing to refuse; `ALLOWED_HOSTS` stays word for word what it was.
MAINNET_HOSTS: frozenset[str] = frozenset({"fapi.binance.com"})
MUTATING_METHODS: frozenset[str] = frozenset({"POST", "PUT", "DELETE"})
# `/fapi/v1/algoOrder` (2026-10-19): Binance moved STOP_MARKET, TAKE_PROFIT_MARKET and the other conditional types
# there from `/fapi/v1/order`.  `is_risk_reducing` answers True for every path NOT in this set (leverage, margin
# type, listen keys), so until it was listed a risk-adding conditional order would have passed an engaged kill
# switch as "session control".  Nothing places one today; the day something does, the switch already covers it.
ORDER_PATHS: frozenset[str] = frozenset({"/fapi/v1/order", "/fapi/v1/batchOrders", "/fapi/v1/algoOrder"})
#: The operator's sign-off (§4.2 item 1).  Absolute, from this file's own checkout, so a process can only be
#: released by a sign-off committed beside the code it is running.
SIGNOFF_PATH = Path(__file__).resolve().parents[1] / "governance" / "MAINNET_ENABLED"


class GuardError(PermissionError):
    pass


@dataclass(frozen=True)
class MainnetRelease:
    """What one command brings to the mainnet question (docs/MAINNET_READINESS.md §4.2-4.3).

    Three things from three places, all required: the operator's sign-off in the repository naming this
    profile, the ``--mainnet`` flag on this command line, and the profile declaring itself mainnet in both
    its file name and its ``profile:`` key.  One mistake can produce one of them; the decision is made
    here and nowhere else, so the readers cannot disagree (L1-07's shape).
    """

    profile_path: str
    profile_label: str
    flag: bool
    signoff_path: Path = SIGNOFF_PATH


def mainnet_refusal(release: MainnetRelease | None) -> str | None:
    """Why this command may not reach a mainnet host, or None when all three conditions hold."""
    if release is None:
        return "no mainnet release was presented"
    reasons: list[str] = []
    if not release.flag:
        reasons.append("this command did not pass --mainnet")
    profile = Path(release.profile_path)
    if "mainnet" not in profile.name.lower():
        reasons.append(f"profile file {profile.name!r} does not name mainnet")
    if "mainnet" not in str(release.profile_label or "").lower():
        reasons.append(f"profile key `profile: {release.profile_label}` does not name mainnet")
    signed = _signed_profile(release.signoff_path)
    if signed is None:
        reasons.append(f"no operator sign-off at {release.signoff_path}")
    else:
        root = release.signoff_path.parent.parent
        if _anchored(signed, root) != _anchored(release.profile_path, root):
            reasons.append(f"the sign-off names {signed!r}, not {release.profile_path!r}")
    return "; ".join(reasons) or None


def _signed_profile(path: Path) -> str | None:
    """The ``profile:`` line of the sign-off, or None when there is no readable sign-off naming one.

    One ``key: value`` line read by hand: this package may not import a YAML parser
    (`tests/architecture/test_import_rules.py`), and the guard needs exactly one field.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, sep, value = line.partition(":")
        if sep and key.strip() == "profile":
            value = value.split("#", 1)[0].strip().strip("'\"")
            return value or None
    return None


def _anchored(path: str, root: Path) -> Path:
    candidate = Path(path)
    return (candidate if candidate.is_absolute() else root / candidate).resolve()


def normalize_host(rest_url: str, *, mainnet: MainnetRelease | None = None) -> str:
    parsed = urlsplit(str(rest_url or "").strip())
    if parsed.scheme.lower() != "https":
        raise GuardError("REST URL must use https")
    if not parsed.hostname:
        raise GuardError("REST URL must contain a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise GuardError("credentials in the REST URL are forbidden")
    if parsed.port not in (None, 443):
        raise GuardError("REST URL must use port 443")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise GuardError("REST base URL must not carry a path, query or fragment")
    host = parsed.hostname.lower()
    if host in ALLOWED_HOSTS:
        return host
    if host in MAINNET_HOSTS:
        refusal = mainnet_refusal(mainnet)
        if refusal is not None:
            raise GuardError(f"host {host!r} is mainnet and is refused: {refusal}")
        return host
    raise GuardError(f"host {host!r} is not an allowed demo/testnet host")


def is_risk_reducing(method: str, path: str, params: Mapping[str, Any]) -> bool:
    if method == "DELETE":
        return True
    if path in ORDER_PATHS:
        flag = str(params.get("reduceOnly", "false")).lower()
        return flag == "true" or str(params.get("closePosition", "false")).lower() == "true"
    return path not in ORDER_PATHS  # leverage / margin type / listen-key style session control


def _as_paths(value: str | Path | Sequence[str | Path] | None) -> tuple[Path, ...]:
    """One path, several, or none - callers predate the plural and must keep working."""
    if value is None:
        return ()
    if isinstance(value, (str, Path)):
        return (Path(value),)
    return tuple(Path(item) for item in value)


class WriteGuard:
    def __init__(
        self,
        rest_url: str,
        kill_switch_path: str | Path | Sequence[str | Path] | None = None,
        *,
        mainnet: MainnetRelease | None = None,
    ) -> None:
        self.host = normalize_host(rest_url, mainnet=mainnet)
        self.kill_switch_paths = _as_paths(kill_switch_path)

    @property
    def kill_switch_path(self) -> Path | None:
        """The first configured path, kept for callers and logs that name a single file."""
        return self.kill_switch_paths[0] if self.kill_switch_paths else None

    def kill_switch_engaged(self) -> bool:
        """Engaged if ANY configured file exists (L1-07).

        Three places ask this question - the engine's guard, this one at the HTTP layer, and the CLI -
        and the account-scoped path was added to only the first.  This is the one that refuses a
        risk-adding order at the wire, so a switch the engine honours and the guard does not would be
        the worst of the three to leave disagreeing.
        """
        return any(path.exists() for path in self.kill_switch_paths)

    def engage_kill_switch(self, reason: str = "") -> Path:
        if self.kill_switch_path is None:
            raise GuardError("no kill switch path configured")
        self.kill_switch_path.parent.mkdir(parents=True, exist_ok=True)
        self.kill_switch_path.write_text(reason or "engaged\n", encoding="utf-8")
        return self.kill_switch_path

    def authorize(self, method: str, path: str, params: Mapping[str, Any]) -> None:
        method = method.upper()
        if method not in MUTATING_METHODS:
            return
        if self.kill_switch_engaged() and not is_risk_reducing(method, path, params):
            raise GuardError(f"kill switch engaged at {self.kill_switch_path}; risk-increasing write refused")
