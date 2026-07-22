# Apache Deployment Guide

This guide explains how to deploy the CRAUT application on an Apache server alongside other applications.

## Prerequisites

1. **Apache with mod_wsgi**: Install Apache and the WSGI module
   ```bash
   sudo apt update
   sudo apt install apache2 libapache2-mod-wsgi-py3
   ```

2. **Python Virtual Environment**: Set up a virtual environment
   ```bash
   cd /home/samuel/uibk/projects/craut
   python3 -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

3. **Enable Apache Modules**:
   ```bash
   sudo a2enmod wsgi ssl headers rewrite
   ```

## Deployment Options

### Option 1: Subdomain (Recommended)
Deploy at `craut.yourdomain.com`

1. **Copy configuration**:
   ```bash
   sudo cp apache-config-subdomain.conf /etc/apache2/sites-available/craut.conf
   ```

2. **Edit the configuration**:
   ```bash
   sudo nano /etc/apache2/sites-available/craut.conf
   ```
   - Replace `craut.yourdomain.com` with your actual subdomain
   - Update SSL certificate paths if using HTTPS
   - Adjust `python-home` path if your venv is elsewhere
   - Update file paths if your app is in a different location

3. **Set permissions**:
   ```bash
   # Allow Apache to read the application
   sudo chown -R www-data:www-data /home/samuel/uibk/projects/craut/uploads
   sudo chown -R www-data:www-data /home/samuel/uibk/projects/craut/data
   sudo chmod -R 755 /home/samuel/uibk/projects/craut
   ```

4. **Enable the site**:
   ```bash
   sudo a2ensite craut
   sudo systemctl reload apache2
   ```

### Option 2: Subpath
Deploy at `yourdomain.com/craut`

1. **Modify app.py** to add application root:
   ```python
   # Add this after app = Flask(__name__)
   app.config["APPLICATION_ROOT"] = "/craut"
   ```

2. **Edit your existing VirtualHost**:
   ```bash
   sudo nano /etc/apache2/sites-available/yourdomain.conf
   ```
   Add the content from `apache-config-subpath.conf` inside your existing `<VirtualHost>` block.

3. **Set permissions** (same as Option 1):
   ```bash
   sudo chown -R www-data:www-data /home/samuel/uibk/projects/craut/uploads
   sudo chown -R www-data:www-data /home/samuel/uibk/projects/craut/data
   sudo chmod -R 755 /home/samuel/uibk/projects/craut
   ```

4. **Reload Apache**:
   ```bash
   sudo systemctl reload apache2
   ```

## Environment Variables

Set environment variables for production in the Apache configuration by adding to the WSGI daemon process:

```apache
WSGIDaemonProcess craut \
    user=www-data \
    group=www-data \
    threads=5 \
    python-home=/home/samuel/uibk/projects/craut/venv \
    display-name=%{GROUP}
    
# Add environment variables:
SetEnv SECRET_KEY "your-secret-key-here"
SetEnv DATABASE_URL "postgresql://user:pass@localhost/dbname"
SetEnv REGISTRATION_TOKEN "your-registration-token"
SetEnv TRUST_PROXY_HEADERS "1"
SetEnv SESSION_COOKIE_SECURE "1"
```

Alternatively, create a `.env` file and load it in `wsgi.py`:
```python
from dotenv import load_dotenv
load_dotenv(APPLICATION_DIR / '.env')
```

## Database Setup

### Using PostgreSQL (Recommended for Production)
```bash
sudo apt install postgresql
sudo -u postgres psql
```

In PostgreSQL:
```sql
CREATE DATABASE craut;
CREATE USER crautuser WITH PASSWORD 'your-password';
GRANT ALL PRIVILEGES ON DATABASE craut TO crautuser;
\q
```

Set `DATABASE_URL` environment variable:
```
DATABASE_URL=postgresql://crautuser:your-password@localhost/craut
```

### Using SQLite (Development)
SQLite will be used by default if no `DATABASE_URL` is set.
Ensure the `data/` directory is writable by www-data.

## Troubleshooting

### Check Apache Error Log
```bash
sudo tail -f /var/log/apache2/craut-error.log
```

### Test Apache Configuration
```bash
sudo apache2ctl configtest
```

### Permission Issues
If you get permission errors:
```bash
# Make sure www-data can read the app
sudo chmod -R 755 /home/samuel/uibk/projects/craut

# Make sure www-data can write to data and uploads
sudo chown -R www-data:www-data /home/samuel/uibk/projects/craut/uploads
sudo chown -R www-data:www-data /home/samuel/uibk/projects/craut/data
sudo chmod -R 775 /home/samuel/uibk/projects/craut/uploads
sudo chmod -R 775 /home/samuel/uibk/projects/craut/data
```

### Module Not Found Errors
Ensure the virtual environment path is correct in the Apache config:
```apache
WSGIDaemonProcess craut python-home=/path/to/your/venv
```

### Application Not Reloading
After making changes to Python code:
```bash
# Touch the WSGI file to reload the app
touch /home/samuel/uibk/projects/craut/wsgi.py

# Or restart Apache
sudo systemctl restart apache2
```

## Security Checklist

- [ ] Set a strong `SECRET_KEY` environment variable
- [ ] Use HTTPS (SSL/TLS certificates)
- [ ] Set `SESSION_COOKIE_SECURE=1` when using HTTPS
- [ ] Use PostgreSQL instead of SQLite for production
- [ ] Restrict database permissions
- [ ] Keep all dependencies updated
- [ ] Set `REGISTRATION_TOKEN` if you want to control registrations
- [ ] Configure firewall to allow only necessary ports
- [ ] Regular backups of database and uploads

## Updating the Application

```bash
cd /home/samuel/uibk/projects/craut
git pull  # or however you update your code
source venv/bin/activate
pip install -r requirements.txt
touch wsgi.py  # Reload the app
```

## Multiple Applications on Same Server

Apache can run multiple WSGI applications simultaneously:

1. Each application gets its own `WSGIDaemonProcess` with a unique name
2. Use different process groups to isolate applications
3. Applications can share the same Apache instance but run in separate processes

Example:
```apache
# App 1
WSGIDaemonProcess app1 python-home=/path/to/app1/venv
WSGIScriptAlias /app1 /path/to/app1/wsgi.py

# App 2 (CRAUT)
WSGIDaemonProcess craut python-home=/path/to/craut/venv
WSGIScriptAlias /craut /path/to/craut/wsgi.py
```

Each application runs independently and won't interfere with others.
