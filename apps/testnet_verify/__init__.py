"""The sole Testnet Verification composition root."""

from .cli import main
from .config import VerifierConfig
from .runtime import VerificationRuntime, VerificationSummary, build_runtime

__all__ = ["VerificationRuntime", "VerificationSummary", "VerifierConfig", "build_runtime", "main"]
