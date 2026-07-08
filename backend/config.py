"""
Central configuration, loaded from environment variables (or a .env file).
Both the SMTP receiver and the API import this so they always agree on
the domain, database path, and retention policy.
"""
import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# The domain your MX record points to, e.g. "mytrashmail.com".
# The SMTP server only accepts mail for this domain (any subdomain of it too).
DOMAIN = os.getenv("DOMAIN", "example.com").lower().strip()

# Where the SQLite database file lives.
DB_PATH = os.getenv("DB_PATH", str(Path(__file__).parent / "mail.db"))

# How long a message is kept before it's auto-deleted.
RETENTION_HOURS = float(os.getenv("RETENTION_HOURS", "3"))

# How often the cleanup sweep runs.
CLEANUP_INTERVAL_MINUTES = float(os.getenv("CLEANUP_INTERVAL_MINUTES", "15"))

# Hard cap on an incoming message's total size (headers + body + attachments).
MAX_MESSAGE_BYTES = int(os.getenv("MAX_MESSAGE_BYTES", str(10 * 1024 * 1024)))  # 10 MB

# Hard cap on how many messages a single mailbox can hold at once
# (oldest gets dropped first). Keeps one address from filling the disk.
MAX_MESSAGES_PER_MAILBOX = int(os.getenv("MAX_MESSAGES_PER_MAILBOX", "100"))

# SMTP receiver bind address/port. Port 25 needs root or setcap (see README).
SMTP_HOST = os.getenv("SMTP_HOST", "0.0.0.0")
SMTP_PORT = int(os.getenv("SMTP_PORT", "25"))

# API bind address/port. Put this behind nginx/certbot for real TLS.
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))

# Local-parts nobody should be able to claim (postmaster is required by RFC 5321).
RESERVED_MAILBOXES = {"postmaster", "abuse", "admin"}
