"""
Bootstrap an admin user. Run once before first use:
    python init_admin.py

Non-interactive (e.g. CI / containers): set ADMIN_USERNAME / ADMIN_PASSWORD env vars.
"""
import json
import os
import getpass
from werkzeug.security import generate_password_hash

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
USERS_FILE = os.path.join(DATA_DIR, "users.json")

os.makedirs(DATA_DIR, exist_ok=True)

if os.path.exists(USERS_FILE):
    with open(USERS_FILE) as f:
        users = json.load(f)
else:
    users = {}

print("=== TasPlan Review — Admin Setup ===")
username = (os.environ.get("ADMIN_USERNAME") or input("Admin username [admin]: ").strip() or "admin").lower()
name = os.environ.get("ADMIN_NAME") or input("Admin full name [Administrator]: ").strip() or "Administrator"
email = os.environ.get("ADMIN_EMAIL") or input("Admin email [admin@example.com]: ").strip() or "admin@example.com"
password = os.environ.get("ADMIN_PASSWORD") or getpass.getpass("Admin password: ")

if not password:
    print("Password cannot be empty.")
    raise SystemExit(1)

users[username] = {
    "name": name,
    "email": email,
    "role": "admin",
    "password": generate_password_hash(password),
    "created_at": "2026-01-01T00:00:00",
}

with open(USERS_FILE, "w") as f:
    json.dump(users, f, indent=2)

print(f"\nAdmin user '{username}' created. Run 'python main.py' to start TasPlan Review.")
