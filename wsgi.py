#!/usr/bin/env python3
"""
WSGI entry point for Apache mod_wsgi deployment
"""
import sys
import os
from pathlib import Path

# Add your application directory to the Python path
APPLICATION_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APPLICATION_DIR))

# Load environment variables from .env file
env_file = APPLICATION_DIR / '.env'
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())

# Import the Flask application
from app import app as application

# Optional: activate virtual environment if not using system Python
# VENV_PATH = APPLICATION_DIR / 'venv'
# activate_this = VENV_PATH / 'bin' / 'activate_this.py'
# if activate_this.exists():
#     exec(open(activate_this).read(), {'__file__': str(activate_this)})
