"""Simulated Microsoft 365 user provisioning (v0.6 placeholder).

This module fabricates realistic-looking M365 responses *without* any real
credentials or network calls, so the onboarding workflow can be exercised
end to end while learning. Every response carries ``"simulated": true``.

The deterministic fake backend is deliberately shaped like a real one: it
takes display name / mail / department, returns a user id and status, and
can be swapped for a Microsoft Graph client later (that still requires
credentials configured safely outside chat — see README limitations).

Deterministic behaviour: user ids are derived from a SHA-256 hash of the
requested mail address, so the same person always maps to the same id even
across replays. Timestamps are real "now" values because they describe when
the simulation ran, not who was simulated.
"""

import hashlib
from datetime import datetime, timezone


class M365Simulator:
    """A fake Microsoft 365 backend that never touches the network."""

    def create_user(self, display_name: str, mail: str, department: str) -> dict:
        """Pretend to create an M365 account and return its details.

        The returned ``id`` is deterministic: the same ``mail`` always maps
        to the same simulated GUID, which keeps replays consistent.
        """
        digest = hashlib.sha256(mail.lower().encode()).hexdigest()
        # Format the hash like a GUID (8-4-4-4-12) purely for realism.
        user_id = "-".join(
            (digest[0:8], digest[8:12], digest[12:16], digest[16:20], digest[20:32])
        )
        now = datetime.now(timezone.utc).isoformat()
        return {
            "simulated": True,
            "id": user_id,
            "display_name": display_name,
            "mail": mail.lower(),
            "user_principal_name": f"{mail.split('@')[0].lower()}@contoso.example",
            "account_enabled": True,
            "department": department,
            "assigned_licenses": ["ENTERPRISEPREMIUM_SIMULATED"],
            "created_date_time": now,
        }

    def get_status(self, user_id: str) -> dict:
        """Return the simulated provisioning status for a user id."""
        return {
            "simulated": True,
            "id": user_id,
            "provisioning_state": "success",
            "last_sync": datetime.now(timezone.utc).isoformat(),
        }
