# AmlaDrop

AmlaDrop is a self-hosted disposable inbox service for QA, automation, and
email workflow testing — an internal alternative to third-party services
like Yopmail or Mailinator. Nobody signs up: anyone can type a name and
see whatever lands in `name@yourdomain.com`. It accepts inbound email for
your configured domain, stores messages in SQLite, and provides a web UI
+ API to read (and send) mail instantly.

## Architecture

```text
Internet ──25/tcp──▶ smtp_server.py (aiosmtpd, catch-all)
                              │
                              ▼
                         mail.db (SQLite)
                              ▲
                              │
Browser ──443/tcp──▶ nginx ──▶ app.py (FastAPI) ──▶ serves frontend/*.html
```

- `smtp_server.py` accepts mail for any local-part `@yourdomain.com` (or a subdomain of it) and writes it to SQLite. It rejects anything addressed to a different domain, so it's never an open relay for other people's mail. `postmaster`, `abuse`, and `admin` are reserved and can't be claimed as mailboxes.
- `app.py` is the REST API the web page polls, and also serves the static frontend, so only one process needs a public HTTPS endpoint. It also handles **compose/reply/forward**: sending mail from one mailbox on this domain to another writes directly into the recipient's inbox in SQLite — there's no outbound SMTP client anywhere in this app, so it can't be used to relay real mail to the outside world.
- A background cleanup loop in `app.py` deletes messages older than `RETENTION_HOURS` (default 3h) every `CLEANUP_INTERVAL_MINUTES`.

The frontend is two static pages, no build step:
- `index.html` — the inbox app: type a name to open a mailbox, a two-pane message list/detail view, compose/reply/forward, attachment downloads, and polling for new mail every 5s.
- `about.html` — a static info page describing the service.

## Project structure

```text
amladrop/
    backend/
        app.py            REST API + static frontend host
        smtp_server.py    catch-all SMTP receiver
        database.py       SQLite storage
        config.py         env-based settings
        requirements.txt
        .env.example
    frontend/
        index.html        inbox UI: view/compose/reply/forward
        about.html        static about/info page
    deploy/
        disposable-mail-api.service
        disposable-mail-smtp.service
        nginx.conf.example
```

## Local development

```bash
cd backend
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

Set environment variables (PowerShell example — avoids the privileged port 25 for SMTP):

```powershell
$env:DOMAIN = "example.com"
$env:DB_PATH = "./mail.db"
$env:RETENTION_HOURS = "3"
$env:CLEANUP_INTERVAL_MINUTES = "15"
$env:MAX_MESSAGE_BYTES = "10485760"
$env:MAX_MESSAGES_PER_MAILBOX = "100"
$env:API_HOST = "0.0.0.0"
$env:API_PORT = "8000"
$env:SMTP_HOST = "127.0.0.1"
$env:SMTP_PORT = "2525"
```

Run the API server, and in another terminal the SMTP receiver:

```bash
uvicorn app:app --host 0.0.0.0 --port 8000
python smtp_server.py
```

Open `http://localhost:8000`.

## Production deployment

### Prerequisites

- A domain name you control (a subdomain works fine, e.g. `mail.example.com`)
- A VPS with a public, static IP and root/sudo access (DigitalOcean, Hetzner, Linode, a plain EC2 box, etc.)
- Python 3.10+
- Inbound port 25 reachable from the internet. Most VPS providers allow **inbound** 25 by default — the restriction some clouds apply (AWS, GCP) is on **outbound** 25, which doesn't matter here since this service only receives.

### 1. DNS

| Type | Host | Value |
|---|---|---|
| A | `mail.yourdomain.com` | your server's IP |
| MX | `yourdomain.com` (priority 10) | `mail.yourdomain.com` |

Optional but recommended — since this server never sends mail, tell the world not to trust mail claiming to be *from* your domain:

| Type | Host | Value |
|---|---|---|
| TXT | `yourdomain.com` | `v=spf1 -all` |

Give DNS a few minutes to propagate before testing (`dig MX yourdomain.com`).

### 2. Install

```bash
sudo useradd -r -s /bin/false disposable-mail
sudo mkdir -p /opt/disposable-mail
sudo cp -r backend frontend /opt/disposable-mail/
cd /opt/disposable-mail/backend

python3 -m venv venv
./venv/bin/pip install -r requirements.txt

cp .env.example .env
nano .env   # set DOMAIN=yourdomain.com at minimum
```

`app.py` serves `../frontend` relative to `backend/`, so keep the two folders as siblings, exactly as copied above.

### 3. Binding port 25

Port 25 is privileged. Two options:

**Simplest — run the SMTP process as root** (what the provided systemd unit does). aiosmtpd drops no privileges on its own, so if you want to avoid running Python as root long-term, use setcap instead:

```bash
sudo setcap 'cap_net_bind_service=+ep' $(readlink -f /opt/disposable-mail/backend/venv/bin/python3.*)
```

Then change `User=root` to `User=disposable-mail` in `disposable-mail-smtp.service` before installing it.

### 4. systemd services

```bash
sudo cp deploy/disposable-mail-api.service /etc/systemd/system/
sudo cp deploy/disposable-mail-smtp.service /etc/systemd/system/
sudo chown -R disposable-mail:disposable-mail /opt/disposable-mail
sudo systemctl daemon-reload
sudo systemctl enable --now disposable-mail-api
sudo systemctl enable --now disposable-mail-smtp
sudo systemctl status disposable-mail-api disposable-mail-smtp
```

Watch logs with `journalctl -u disposable-mail-smtp -f` while you send a test email.

### 5. Web frontend over HTTPS

```bash
sudo apt install nginx certbot python3-certbot-nginx
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/disposable-mail
sudo ln -s /etc/nginx/sites-available/disposable-mail /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d yourdomain.com
```

(Edit `server_name` in that file to your actual domain first.)

### 6. Firewall

```bash
sudo ufw allow 25/tcp    # SMTP
sudo ufw allow 80/tcp    # HTTP (certbot renewal + redirect)
sudo ufw allow 443/tcp   # HTTPS
sudo ufw allow OpenSSH
sudo ufw enable
```

### 7. Test it

Visit `https://yourdomain.com`, type any name, then from a different mail account send a message to `thatname@yourdomain.com`. It should show up within a few seconds (the page polls every 5s). You can also compose a message from inside the UI to another `@yourdomain.com` mailbox — that path never leaves the server, it's a direct DB write.

From the command line, a quick synthetic test without a real mail account:

```bash
python3 -c "
import smtplib
smtplib.SMTP('yourdomain.com', 25).sendmail(
    'test@example.com', ['hello@yourdomain.com'],
    'Subject: it works\n\nfirst message'
)"
```

## API reference

All served by `app.py` under `/api`:

| Method & path | Purpose |
|---|---|
| `GET /api/domain` | Returns `{domain, retention_hours}` — the frontend uses this to label the address field and expiry hint. |
| `GET /api/inbox/{mailbox}` | List messages in a mailbox (subject/sender/snippet, newest first). |
| `GET /api/message/{id}` | Full message: body text/HTML + attachment metadata (no attachment bytes). |
| `GET /api/attachment/{id}/{index}` | Download one attachment's bytes. |
| `POST /api/send` | Deliver mail from one mailbox on this domain to another, in-process — no outbound SMTP. Powers compose/reply/forward in the UI. |
| `DELETE /api/message/{id}` | Delete a single message. |
| `DELETE /api/inbox/{mailbox}` | Clear an entire mailbox. |

Example `/api/send` payload:

```json
{
    "sender": "qa-user",
    "to": "target@example.com",
    "subject": "OTP test",
    "body": "Your OTP is 123456"
}
```

## Configuration reference (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `DOMAIN` | `example.com` | The only domain the SMTP server and `/api/send` accept mail for |
| `DB_PATH` | `./mail.db` | SQLite file location |
| `RETENTION_HOURS` | `3` | How long a message survives before auto-deletion |
| `CLEANUP_INTERVAL_MINUTES` | `15` | How often the expiry sweep runs |
| `MAX_MESSAGE_BYTES` | `10485760` (10MB) | Hard cap on one incoming message's size |
| `MAX_MESSAGES_PER_MAILBOX` | `100` | Oldest messages get dropped past this count |
| `SMTP_HOST` / `SMTP_PORT` | `0.0.0.0` / `25` | SMTP bind address |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | API bind address (put nginx in front of this) |

`postmaster`, `abuse`, and `admin` are reserved local-parts and always rejected — this isn't configurable via `.env` (see `RESERVED_MAILBOXES` in `config.py`).

## Security notes worth actually reading

- **This is intentionally an open system.** Anyone who can guess or type a mailbox name can read it, and (via compose) send as it to any other mailbox on the domain — that's the product. Don't reuse this for anything that needs real confidentiality.
- **Abuse mitigation is minimal by design here.** A public disposable-mail server is a magnet for spam probing and can get your IP blocklisted. In production consider: rate-limiting SMTP connections per source IP (fail2ban watching the log, or a firewall rule), a basic greylist, and monitoring disk usage on `mail.db`.
- **HTML email is sandboxed but not neutered.** The frontend renders HTML bodies inside a sandboxed `<iframe>` with no `allow-scripts`, so embedded `<script>` tags in incoming mail can't execute or touch the parent page. Remote images in HTML mail will still load (leaking the viewer's IP to whoever sent it) — that's normal email-client behavior, but you could strip `<img>` src attributes in `app.py` if you want to close that off too.
- **`/api/send` never leaves the server.** It's a same-domain, in-process DB write with no SMTP client involved, so it can't be abused to relay real mail out — but it does mean anyone who can reach the API can drop mail into any non-reserved mailbox on the domain.
- **SQLite is fine at moderate volume** (a hobby or small-team disposable-mail box). If you're expecting heavy traffic, swap `database.py` for Postgres — the interface is small (six functions).
- **Check your hosting provider's acceptable-use policy.** Some providers restrict running open mail-receiving services; a quick read of their ToS avoids a surprise account suspension.
