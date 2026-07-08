# Tearline — self-hosted disposable email

A Yopmail-style throwaway inbox: nobody signs up, anyone can type a name
and see whatever lands in `name@yourdomain.com`. This document is
everything you need to take the code in this folder and put it on a
real domain.

## How it works

```
Internet ──25/tcp──▶ smtp_server.py (aiosmtpd, catch-all)
                              │
                              ▼
                         mail.db (SQLite)
                              ▲
                              │
Browser ──443/tcp──▶ nginx ──▶ app.py (FastAPI)  ──▶ serves frontend/index.html
```

Two Python processes share one SQLite file:
- `smtp_server.py` accepts mail for any address `@yourdomain.com` and writes it to the database. It rejects anything addressed to a different domain, so it's never an open relay for other people's mail.
- `app.py` is the REST API the web page polls, and also serves the frontend itself, so only one process needs a public HTTPS endpoint.

A background sweep in `app.py` deletes messages older than `RETENTION_HOURS` (default 3h) every `CLEANUP_INTERVAL_MINUTES`.

## Prerequisites

- A domain name you control (a subdomain works fine, e.g. `mail.example.com`)
- A VPS with a public, static IP and root/sudo access (DigitalOcean, Hetzner, Linode, a plain EC2 box, etc.)
- Python 3.10+
- Inbound port 25 reachable from the internet. Most VPS providers allow **inbound** 25 by default — the restriction some clouds apply (AWS, GCP) is on **outbound** 25, which doesn't matter here since this service only receives.

## 1. DNS

Point mail at your server:

| Type | Host | Value |
|---|---|---|
| A | `mail.yourdomain.com` | your server's IP |
| MX | `yourdomain.com` (priority 10) | `mail.yourdomain.com` |

Optional but recommended — since this server never sends mail, tell the world not to trust mail claiming to be *from* your domain:

| Type | Host | Value |
|---|---|---|
| TXT | `yourdomain.com` | `v=spf1 -all` |

Give DNS a few minutes to propagate before testing (`dig MX yourdomain.com`).

## 2. Install

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

## 3. Let the API bind normally, let SMTP bind port 25

Port 25 is privileged. Two options:

**Simplest — run the SMTP process as root** (what the provided systemd unit does). aiosmtpd drops no privileges on its own, so if you want to avoid running Python as root long-term, use the setcap approach instead:

```bash
sudo setcap 'cap_net_bind_service=+ep' $(readlink -f /opt/disposable-mail/backend/venv/bin/python3.*)
```

Then change `User=root` to `User=disposable-mail` in `disposable-mail-smtp.service` before installing it.

## 4. systemd services

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

## 5. Web frontend over HTTPS

Put nginx + certbot in front of the API so the browser gets real TLS:

```bash
sudo apt install nginx certbot python3-certbot-nginx
sudo cp deploy/nginx.conf.example /etc/nginx/sites-available/disposable-mail
sudo ln -s /etc/nginx/sites-available/disposable-mail /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
sudo certbot --nginx -d yourdomain.com
```

(Edit `server_name` in that file to your actual domain first.)

## 6. Firewall

```bash
sudo ufw allow 25/tcp    # SMTP
sudo ufw allow 80/tcp    # HTTP (certbot renewal + redirect)
sudo ufw allow 443/tcp   # HTTPS
sudo ufw allow OpenSSH
sudo ufw enable
```

## 7. Test it

Visit `https://yourdomain.com`, type any name, then from a different mail account send a message to `thatname@yourdomain.com`. It should show up within a few seconds (the page polls every 5s).

From the command line, a quick synthetic test without a real mail account:

```bash
python3 -c "
import smtplib
smtplib.SMTP('yourdomain.com', 25).sendmail(
    'test@example.com', ['hello@yourdomain.com'],
    'Subject: it works\n\nfirst message'
)"
```

## Configuration reference (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `DOMAIN` | `example.com` | The only domain the SMTP server accepts mail for |
| `DB_PATH` | `./mail.db` | SQLite file location |
| `RETENTION_HOURS` | `3` | How long a message survives before auto-deletion |
| `CLEANUP_INTERVAL_MINUTES` | `15` | How often the expiry sweep runs |
| `MAX_MESSAGE_BYTES` | `10485760` (10MB) | Hard cap on one incoming message's size |
| `MAX_MESSAGES_PER_MAILBOX` | `100` | Oldest messages get dropped past this count |
| `SMTP_HOST` / `SMTP_PORT` | `0.0.0.0` / `25` | SMTP bind address |
| `API_HOST` / `API_PORT` | `0.0.0.0` / `8000` | API bind address (put nginx in front of this) |

## Security notes worth actually reading

- **This is intentionally an open system.** Anyone who can guess or type a mailbox name can read it — that's the product. Don't reuse this for anything that needs real confidentiality.
- **Abuse mitigation is minimal by design here.** A public disposable-mail server is a magnet for spam probing and can get your IP blocklisted. In production consider: rate-limiting SMTP connections per source IP (fail2ban watching the log, or a firewall rule), a basic greylist, and monitoring disk usage on `mail.db`.
- **HTML email is sandboxed but not neutered.** The frontend renders HTML bodies inside a sandboxed `<iframe>` with no `allow-scripts`, so embedded `<script>` tags in incoming mail can't execute or touch the parent page. Remote images in HTML mail will still load (leaking the viewer's IP to whoever sent it) — that's normal email-client behavior, but you could strip `<img>` src attributes in `app.py` if you want to close that off too.
- **SQLite is fine at moderate volume** (a hobby or small-team disposable-mail box). If you're expecting heavy traffic, swap `database.py` for Postgres — the interface is small (six functions).
- **Check your hosting provider's acceptable-use policy.** Some providers restrict running open mail-receiving services; a quick read of their ToS avoids a surprise account suspension.

## Project layout

```
disposable-mail/
├── backend/
│   ├── app.py            REST API + static frontend host
│   ├── smtp_server.py    catch-all SMTP receiver
│   ├── database.py       SQLite storage
│   ├── config.py         env-based settings
│   ├── requirements.txt
│   └── .env.example
├── frontend/
│   └── index.html        the whole web UI, no build step
└── deploy/
    ├── disposable-mail-api.service
    ├── disposable-mail-smtp.service
    └── nginx.conf.example
```
