# Apache Deployment Guide

This guide explains how to deploy LITRA on an Apache server alongside other
applications.

## Prerequisites

1. Install Apache and mod_wsgi:

   ```bash
   sudo apt update
   sudo apt install apache2 libapache2-mod-wsgi-py3
   ```

2. Set up a Python virtual environment:

   ```bash
   cd /home/samuel/uibk/projects/litra
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. Enable Apache modules:

   ```bash
   sudo a2enmod wsgi ssl headers rewrite
   ```

## Deployment Options

### Option 1: Subdomain

Deploy at `litra.yourdomain.com`.

```bash
sudo cp apache-config-subdomain.conf /etc/apache2/sites-available/litra.conf
sudo nano /etc/apache2/sites-available/litra.conf
sudo a2ensite litra
sudo systemctl reload apache2
```

Update the server name, SSL certificate paths, virtual environment path, and
application path in the copied Apache config.

### Option 2: Subpath

Deploy at `yourdomain.com/litra`.

Set the application root in the environment or in `app.py`:

```bash
APPLICATION_ROOT=/litra
```

Then add the content from `apache-config-subpath.conf` inside your existing
`<VirtualHost>` block and reload Apache.

## Environment Variables

Set production environment variables in Apache with `SetEnv`, in your process
manager, or in a local `.env` loaded by `wsgi.py`.

Minimum production configuration:

```apache
WSGIDaemonProcess litra \
    user=www-data \
    group=www-data \
    threads=5 \
    python-home=/home/samuel/uibk/projects/litra/venv \
    display-name=%{GROUP}

SetEnv APP_ENV "production"
SetEnv REQUIRE_POSTGRES "1"
SetEnv SECRET_KEY "replace-with-a-long-random-secret"
SetEnv DATABASE_URL "postgresql://litrauser:your-password@localhost/litra"
SetEnv REGISTRATION_TOKEN "replace-with-random-registration-token"
SetEnv SESSION_COOKIE_SECURE "1"
SetEnv TRUST_PROXY_HEADERS "1"
```

## Database Setup

Production deployments should use PostgreSQL.

```bash
sudo apt install postgresql
sudo -u postgres psql
```

In PostgreSQL:

```sql
CREATE DATABASE litra;
CREATE USER litrauser WITH PASSWORD 'your-password';
GRANT ALL PRIVILEGES ON DATABASE litra TO litrauser;
\q
```

Set:

```text
DATABASE_URL=postgresql://litrauser:your-password@localhost/litra
```

SQLite is suitable for local development only. If no `DATABASE_URL` is set,
LITRA writes to `data/app.sqlite3`.

## File Permissions

Apache must be able to read the app and write runtime data:

```bash
sudo chown -R www-data:www-data /home/samuel/uibk/projects/litra/uploads
sudo chown -R www-data:www-data /home/samuel/uibk/projects/litra/data
sudo chmod -R 755 /home/samuel/uibk/projects/litra
sudo chmod -R 775 /home/samuel/uibk/projects/litra/uploads
sudo chmod -R 775 /home/samuel/uibk/projects/litra/data
```

## Security Checklist

- [ ] Set a strong `SECRET_KEY`.
- [ ] Use HTTPS.
- [ ] Set `SESSION_COOKIE_SECURE=1` when using HTTPS.
- [ ] Use PostgreSQL with restricted database permissions.
- [ ] Set `REGISTRATION_TOKEN` if registrations should be invite-only.
- [ ] Configure upload limits and rate limits.
- [ ] Set `TRUST_PROXY_HEADERS=1` only behind a trusted reverse proxy.
- [ ] Back up the database and uploads directory.
- [ ] Keep dependencies and the base OS updated.

## Troubleshooting

Check Apache logs:

```bash
sudo tail -f /var/log/apache2/litra-error.log
```

Test Apache configuration:

```bash
sudo apache2ctl configtest
```

Reload the app after code changes:

```bash
touch /home/samuel/uibk/projects/litra/wsgi.py
```

## Updating The Application

```bash
cd /home/samuel/uibk/projects/litra
git pull
source venv/bin/activate
pip install -r requirements.txt
touch wsgi.py
```

## Multiple Applications On One Server

Use a unique `WSGIDaemonProcess`, process group, URL path, and log file for
each application.

```apache
WSGIDaemonProcess app1 python-home=/path/to/app1/venv
WSGIScriptAlias /app1 /path/to/app1/wsgi.py

WSGIDaemonProcess litra python-home=/home/samuel/uibk/projects/litra/venv
WSGIScriptAlias /litra /home/samuel/uibk/projects/litra/wsgi.py
```
