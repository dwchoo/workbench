"""Explicit local raw activation; existing sources keep their already chosen writer."""
from datetime import datetime, timezone
from pathlib import Path
import os
import stat
from uuid import UUID

from workbench.backend.paths import DataDirError, InstanceLock, ensure_private_dir, write_private_json
from workbench.backend.summary_compatibility import SummaryCompatibility, _read_owned


def _canonical_ref(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


class RawRotationPolicy:
    def __init__(self, path: Path, compatibility: SummaryCompatibility):
        self.path, self.compatibility = Path(path), compatibility

    def _read(self):
        try:
            parent = self.path.parent.lstat()
            if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid()
                    or stat.S_IMODE(parent.st_mode) != 0o700):
                raise DataDirError("unsafe raw rotation directory")
            value = _read_owned(self.path)
            if value is None:
                return None
            if (set(value) != {"schema_version", "activated", "evidence", "activated_at"}
                    or type(value["schema_version"]) is not int or value["schema_version"] != 1
                    or value["activated"] is not True or not _canonical_ref(value["evidence"])
                    or not isinstance(value["activated_at"], str)
                    or datetime.fromisoformat(value["activated_at"]).utcoffset() is None):
                raise ValueError("invalid raw rotation policy")
            return value
        except FileNotFoundError:
            return None

    def activate(self, evidence: str):
        # Reject before allocation, then recheck while protection/verify publication is locked.
        if (not _canonical_ref(evidence) or not self.compatibility.verify_reference(evidence)
                or not self.compatibility.admission()):
            raise ValueError("verified_activation_evidence_unavailable")
        with self.compatibility._mutation():
            lock = InstanceLock(self.path.with_suffix(".lock"))
            lock.acquire()
            try:
                if (not self.compatibility.verify_reference(evidence) or not self.compatibility.admission()):
                    raise ValueError("verified_activation_evidence_unavailable")
                existing = self._read()
                if existing is not None:
                    return existing  # No renewed activation or source conversion.
                value = {"schema_version": 1, "activated": True, "evidence": evidence,
                         "activated_at": datetime.now(timezone.utc).isoformat()}
                write_private_json(self.path, value)
                return value
            finally:
                lock.release()

    def admits_new_source(self):
        try:
            return self._read() is not None and self.compatibility.admission() is True
        except (OSError, ValueError, TypeError, RecursionError, DataDirError):
            return False  # Compatibility failure never stops the main work.

    def status(self):
        """Policy plus last check only; no live identity or verification work."""
        try:
            policy = self._read()
            reason = "activation_required" if policy is None else "compatibility_not_checked"
        except (OSError, ValueError, TypeError, RecursionError, DataDirError):
            policy, reason = None, "raw_rotation_policy_unavailable"
        observe = getattr(self.compatibility, "observed_status", None)
        observed = observe() if callable(observe) else {}
        admitted_states = ("verified", "version_drift_unverified", "unrelated_change_observed")
        segmented = policy is not None and observed.get("state") in admitted_states and observed.get("lastCheckedAt")
        if policy is not None and observed.get("lastCheckedAt"):
            reason = observed.get("reason") or observed.get("state")
        return {"activated": policy is not None,
                "activatedAt": policy["activated_at"] if policy else None,
                "evidence": policy["evidence"] if policy else None,
                "newSourcePolicy": "segmented-v1" if segmented else "legacy-stop",
                "reason": reason, "lastCheckedAt": observed.get("lastCheckedAt"),
                "observationOnly": True}
