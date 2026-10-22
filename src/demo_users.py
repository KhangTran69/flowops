"""Generate a PBKDF2 hash snippet for FLOWOPS_DEMO_USERS in development."""
import getpass
import json
import sys

from src.ui_session import hash_demo_password


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] != "hash":
        print("Usage: python -m src.demo_users hash", file=sys.stderr)
        return 2
    username = input("Development username: ").strip()
    role = input("Role (HR, MANAGER, IT_ADMIN): ").strip().upper()
    if not username or role not in {"HR", "MANAGER", "IT_ADMIN"}:
        print("Username and one supported role are required.", file=sys.stderr)
        return 2
    password = getpass.getpass("Development password: ")
    if len(password) < 12:
        print("Use at least 12 characters.", file=sys.stderr)
        return 2
    print(json.dumps({username: {"password_hash": hash_demo_password(password), "roles": [role]}}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
