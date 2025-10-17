# gunicorn_ssl.conf.py - SSL-enabled Gunicorn configuration
import os

# Server socket
bind = "0.0.0.0:5001"
backlog = 2048

# Worker processes
workers = 1
worker_class = "eventlet"
worker_connections = 1000
timeout = 30
keepalive = 2

# SSL Configuration
ssl_cert = os.environ.get('SSL_CERT_PATH')
ssl_key = os.environ.get('SSL_KEY_PATH')

if ssl_cert and ssl_key and os.path.exists(ssl_cert) and os.path.exists(ssl_key):
    certfile = ssl_cert
    keyfile = ssl_key
    print(f"SSL enabled: cert={ssl_cert}, key={ssl_key}")
else:
    print("SSL certificates not found, running HTTP only")

# Logging
accesslog = '-'
errorlog = '-'
loglevel = 'info'

# Process naming
proc_name = 'brainstormx_ssl'

# Server mechanics
daemon = False
pidfile = '/tmp/brainstormx_ssl.pid'
user = None
group = None
tmp_upload_dir = None

# SSL Security
ssl_version = 2  # SSLv23
ciphers = 'TLSv1'