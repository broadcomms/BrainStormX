# BrainStormX Production Deployment with DOMAIN & SSL Certificate

## Overview

This document outlines the complete deployment process for BrainStormX to AWS EC2 with domain `brainstormx.ca` and SSL certificate.

## Production Details

**NAME** `BrainStomX_EC2`

**VERSION** `1.0.0`

**Target Environment:**

- **Domain Name:** `brainstormx.ca`
- **Server type:** `AWS EC2`
- **OS Image** `Ubuntu Server 24.04 LTS (HVM), SSD Volume Type, Canonical amd64 noble image`
- **Instance type** `t3.small, 2 vCPU, 2 Gib Memory`
- **Security Group** `Allow HTTPS/HTTPS Anywhere, Allow SSH/From IP`
- **Boot Storage** `64 GiB gp3`
- **Public IP Address:** `54.90.225.8`
- **Public DNS:** `ec2-54-90-225-8.compute-1.amazonaws.com`
- **SSH Access:** `ssh -i "instance/brainstorm_x_vm_sshkey.pem" ubuntu@ec2-54-90-225-8.compute-1.amazonaws.com`
- **Admin SSH:** `ssh -i "instance/brainstorm_x_vm_sshkey.pem" brainstormx@54.90.225.8 -p 22`

## Architecture Analysis

### Technology Stack

- **Framework:** Flask application server with Socket.IO for real-time features
- **Database:** SQLite (production ready for current scale, supports PostgreSQL through SQLAlchemy)
- **AI Integration:** AWS Bedrock (Nova models, AgentCore Memory)
- **Python Version:** 3.10+ required
- **Web Server:** Nginx (reverse proxy) + Gunicorn (WSGI server)
- **SSL Certification:** Let's Encrypt via Certbot
- **Monitoring:** Prometheus (optional, included by default in docker-compose)

### Key Components

1. **Real-time Workshop Automation** - Socket.IO powered sequencing/asynchronous features
2. **AI Integration** - AWS Bedrock SDK/ Langchain (Nova models) for content generation
3. **File Uploads** - Profile photos, workshop reports, session transcripts
4. **Background Tasks/Jobs** - Transcription, document processing (minimal synchronous for current scale)
5. **Database Content** - Workshop data, user accounts, session state

## System Requirements

### Server Prerequisites

- **OS:** Ubuntu 24.04+ LTS
- **RAM:** Minimum 2GB (recommended 4GB+ for production)
- **Storage:** 64GB SSD (recommended 32GB + S3 Blob storage for uploads)
- **Python:** 3.10+
- **Network:** Port 80, 443, 22 open
- **Domain:** DNS pointing to server IP
- **Application Username:** `brainstormx`

### System Dependencies

```bash
# Core system packages
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv nginx git curl wget unzip
sudo apt install -y build-essential libssl-dev libffi-dev python3-dev
sudo apt install -y sqlite3 # For SQLite database support
sudo apt install -y tesseract-ocr libtesseract-dev # For OCR functionality
sudo apt install -y ffmpeg # For audio processing (Vosk transcription)
sudo apt install -y certbot python3-certbot-nginx # SSL certificates  
```

### Python Dependencies

See `requirements.txt` - Key packages include:

- Flask, Flask-SocketIO, eventlet
- SQLAlchemy, Flask-SQLAlchemy
- boto3 (AWS SDK), langchain-aws
- Pillow, opencv-python (image processing)
- vosk (speech recognition)
- bedrock-agentcore (AI integration)

## Pre-Deployment Checklist

### 1. Domain Configuration

- [X] Point `brainstormx.ca` DNS A record to `54.90.225.8`
- [X] Verify DNS propagation: `nslookup brainstormx.ca` points to `54.90.225.8`
- [X] Recommended: Configure www subdomain `www.brainstormx.ca`

### 2. AWS Configuration

- [X] Verify AWS credentials for Bedrock access (ACCESS KEY ID, SECRET ACCESS KEY) and add in .env.server file
- [X] Test Required Bedrock model access (Nova Lite - amazon.nova-lite-v1:0, Nova Pro - amazon.nova-pro-v1:0)
- [X] Configure IAM permissions for required AWS services (Bedrock, AgentCore, S3 Blob Storage)

### 3. Environment Setup

- [X] Create production `.env.server` file
- [X] Set secure SECRET_KEY
- [X] Configure mail settings
- [X] Set database path

### 4. Security Groups (AWS EC2)

- [X] Port 22 (SSH) - Your IP only
- [X] Port 80 (HTTP) - 0.0.0.0/0
- [X] Port 443 (HTTPS) - 0.0.0.0/0
- [X] Port 5001 (App) - Local only (127.0.0.1)

## Deployment Steps

### Phase 1: Server Preparation

1. **Connect to server:**

**Note** replace with your EC2 `path_to_your/public_key_file_name.pem` path and `ubuntu@your_ec2_public_dns_name`

First, ensure the SSH key has correct permissions:

```bash
# Set secure permissions for SSH key (required for SSH to work)
chmod 400 instance/brainstorm_x_vm_sshkey.pem

# Verify permissions are correct
ls -la instance/brainstorm_x_vm_sshkey.pem

# Test the SSH connection
ssh -i "instance/brainstorm_x_vm_sshkey.pem" ubuntu@ec2-54-90-225-8.compute-1.amazonaws.com echo "SSH connection successful"

# Connect to EC2 instance
ssh -i "instance/brainstorm_x_vm_sshkey.pem" ubuntu@ec2-54-90-225-8.compute-1.amazonaws.com
```

2. **Install system dependencies:**

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv nginx git curl wget unzip
sudo apt install -y build-essential libssl-dev libffi-dev python3-dev
sudo apt install -y sqlite3 # For SQLite database support
sudo apt install -y tesseract-ocr libtesseract-dev
sudo apt install -y ffmpeg
sudo apt install -y certbot python3-certbot-nginx
```

3. **Create application user:**

```bash
# Add application user account (brainstormx)
sudo useradd -m -s /bin/bash brainstormx
sudo usermod -aG sudo brainstormx

# Set password for brainstormx user (Optional)
sudo passwd brainstormx
# If you set password, switch to brainstormx user to work with application
su brainstormx # Enter the password

# Start Interactivive session as brainstormx user
sudo -u brainstormx bash
# Navigate to the application user root
cd ~brainstormx

```

### Phase 2: Application Deployment

1. **Download application code to Server:**

```bash
# Option A: Git clone production code (recommended, Generate secret key token for access)
sudo -u brainstormx git clone -b production https://github.com/broadcomms/brainstorm_x.git /home/brainstormx/brainstorm_x

# Username for 'https://github.com'
# broadcomms

git config --global user.name 'broadcomms'
# Personal access for password
# copy from brainstorm_x_git_access_token.txt

# Option B: SCP to transfre files directly from your local machine to server
scp -i "instance/brainstorm_x_vm_sshkey.pem" -r . ubuntu@ec2-54-90-225-8.compute-1.amazonaws.com:/tmp/brainstorm_x
sudo mv /tmp/brainstorm_x /home/brainstormx/
sudo chown -R brainstormx:brainstormx /home/brainstormx/brainstorm_x
```

2. **Setup Python environment:**

```bash
sudo -u brainstormx bash -c "
cd /home/brainstormx/brainstorm_x
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
"
```

2a. **Install Piper TTS Engine:**

Piper is an open-source text-to-speech engine for the AI voice features.
The `.env.server` file expects Piper to be installed, so follow these steps to install Piper TTS engine:

```bash
sudo -u brainstormx bash -c "
cd /home/brainstormx/brainstorm_x

# Download latest Piper release (see https://github.com/rhasspy/piper/releases)
wget https://github.com/rhasspy/piper/releases/latest/download/piper_linux_x86_64.tar.gz

# Extract and install the binary
tar -xzf piper_linux_x86_64.tar.gz

# Copy binary and libraries
cp piper/piper venv/bin
chmod +x venv/bin/piper

# Copy all required libraries
cp piper/lib*.so* venv/lib/
"

# Copy espeak-ng data and libraries (requires sudo)
sudo mkdir -p /usr/share/espeak-ng-data
sudo cp -r /home/brainstormx/brainstorm_x/piper/espeak-ng-data/* /usr/share/espeak-ng-data/
sudo cp /home/brainstormx/brainstorm_x/piper/lib*.so* /usr/local/lib/
sudo cp /home/brainstormx/brainstorm_x/piper/libespeak-ng.so* /usr/local/lib/

# Update library cache
sudo ldconfig

# Verify piper installation
sudo -u brainstormx bash -c "
cd /home/brainstormx/brainstorm_x
./venv/bin/piper --version
"

# Clean up the compressed binary
sudo -u brainstormx bash -c "
cd /home/brainstormx/brainstorm_x
rm piper_linux_x86_64.tar.gz
"
```

**Note:** Piper TTS requires system-level libraries to be installed. The above commands copy the necessary libraries to `/usr/local/lib/` and espeak-ng data to `/usr/share/espeak-ng-data/` for proper functionality.

2b. **Install Vosk Speech Recognition Model:**

The `.env.server` file expects Vosk model to be installed for transcription features:

```bash
sudo -u brainstormx bash -c "
cd /home/brainstormx/brainstorm_x

# Create STT models directory
mkdir -p stt_models

# Download and install Vosk model (matches .env.server path)
cd stt_models
wget https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip
unzip vosk-model-en-us-0.22-lgraph.zip
rm vosk-model-en-us-0.22-lgraph.zip

# Verify model installation
ls -la /home/brainstormx/brainstorm_x/stt_models/vosk-model-en-us-0.22-lgraph/
"
```

2c. **Install Piper TTS Models:**

Download the TTS model specified in `.env.server`:

```bash
sudo -u brainstormx bash -c "
cd /home/brainstormx/brainstorm_x

# Create TTS models directory
mkdir -p tts_models

# Download Piper TTS model (matches .env.server configuration)
cd tts_models
wget https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/hfc_male/medium/en_US-hfc_male-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/hfc_male/medium/en_US-hfc_male-medium.onnx.json

# Verify model installation
ls -la /home/brainstormx/brainstorm_x/tts_models/
"
```

3. **Use production environment file:**

Copy the production environment file from your local machine to the server:

```bash
# Option A: Copy .env.server from local machine to server (recommended)
# Run this from your LOCAL machine where .env.server exists:
scp -i "instance/brainstorm_x_vm_sshkey.pem" .env.server ubuntu@ec2-54-90-225-8.compute-1.amazonaws.com:/tmp/.env.server

# Then on the SERVER, move it to the correct location:
sudo mv /tmp/.env.server /home/brainstormx/brainstorm_x/.env
sudo chown brainstormx:brainstormx /home/brainstormx/brainstorm_x/.env
sudo chmod 600 /home/brainstormx/brainstorm_x/.env

# Option B: If .env.server is not available locally, set the variables below and create it on the server:
#
sudo -u brainstormx bash -c "
cd /home/brainstormx/brainstorm_x
cat > .env << 'EOF'
# Production Configuration
FLASK_ENV=production
SECRET_KEY=c8654b5539a6bf245f221ced45f080b8d2ffb7e4b601378d8c5b5e95615cffdc
DEBUG=false

# AWS Bedrock Configuration
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=AKIATQVRHFBJ2RQZDAEP
AWS_SECRET_ACCESS_KEY=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
BEDROCK_MODEL_ID=amazon.nova-lite-v1:0
BEDROCK_NOVA_MICRO=amazon.nova-micro-v1:0
BEDROCK_NOVA_LITE=amazon.nova-lite-v1:0
BEDROCK_NOVA_PRO=amazon.nova-pro-v1:0
BEDROCK_NOVA_IMAGE_GEN=amazon.nova-canvas-v1:0
BEDROCK_IMAGE_MODEL_ID=amazon.nova-canvas-v1:0
BEDROCK_NOVA_VIDEO_GEN=amazon.nova-reel-v1:1
BEDROCK_NOVA_SPEECH=amazon.nova-sonic-v1:0
BEDROCK_TITAN_TEXT=amazon.titan-embed-text-v2:0
BEDROCK_TITAN_IMAGE=amazon.titan-embed-image-v1:0

# Server Configuration
PORT=5001
DEFAULT_TIMEZONE=America/Toronto

# Mail settings
MAIL_SERVER=server108.web-hosting.com
MAIL_PORT=587
MAIL_USE_TLS=True
MAIL_USE_SSL=False
MAIL_USERNAME=no-reply@broadcomms.net
MAIL_PASSWORD=5i-8v@S4y$Y?
MAIL_DEFAULT_SENDER=no-reply@broadcomms.net
MAIL_SUPPRESS_SEND=False
MAIL_USE_RELAXED_SSL=True

# AgentCore settings
AGENTCORE_MEMORY_ENABLED=true
AGENTCORE_MEMORY_ID=BrainStormXMemory-OqNur7Gojc
AGENTCORE_MEMORY_ARN=arn:aws:bedrock-agentcore:us-east-1:241963575379:memory/BrainStormXMemory-OqNur7Gojc
AGENTCORE_MEMORY_TOP_K=5
AGENTCORE_MEMORY_TIMEOUT_SECONDS=3
AGENTCORE_MEMORY_DEBUG_LOG=true
AGENTCORE_MEMORY_STORE_BACKGROUND=true

# Assistant Configuration
ASSISTANT_UI_STRICT_LLM_ONLY=true
ASSISTANT_MEMORY_BADGE=true

# Workshop features
WORKSHOP_CONFERENCE_ENABLED=True
WORKSHOP_TRANSCRIPTION_ENABLED=True
WORKSHOP_RECORDING_ENABLED=True

# Transcription and TTS configuration
TRANSCRIPTION_PROVIDER=vosk
VOSK_MODEL_PATH=/home/brainstormx/brainstorm_x/stt_models/vosk-model-en-us-0.22-lgraph
TTS_PROVIDER=piper
PIPER_BIN=/home/brainstormx/brainstorm_x/venv/bin/piper
PIPER_MODEL=/home/brainstormx/brainstorm_x/tts_models/en_US-hfc_male-medium.onnx
STT_PROVIDER=vosk

# AWS Transcribe settings
AWS_TRANSCRIBE_LANGUAGE_CODE=en-US
AWS_TRANSCRIBE_SAMPLE_RATE=16000
AWS_TRANSCRIBE_MEDIA_ENCODING=pcm
AWS_TRANSCRIBE_VOCABULARY_NAME=optional_custom_vocab
AWS_TRANSCRIBE_VOCABULARY_FILTER_NAME=optional_filter

# Tool Configuration
TOOL_TIMEOUT_SECONDS=12
TOOL_MAX_WORKERS=4
CIRCUIT_BREAKER_THRESHOLD=3
CIRCUIT_BREAKER_RESET_SECONDS=60

# Assistant Configuration
ASSISTANT_THREADS_ENABLED=true
ASSISTANT_STRICT_JSON=true
EOF
"
```

**Verification Step:** Before proceeding, verify all installations:

```bash
# Verify environment file exists and is readable
sudo -u brainstormx bash -c "
cd /home/brainstormx/brainstorm_x
ls -la .env
head -5 .env
"

# Test Python application can start
sudo -u brainstormx bash -c "
cd /home/brainstormx/brainstorm_x
source venv/bin/activate
python3 -c 'from app import create_app; app = create_app(); print(\"App creation successful\")'
"

# Test Piper and Vosk installations
sudo -u brainstormx bash -c "
cd /home/brainstormx/brainstorm_x
source venv/bin/activate
./venv/bin/piper --version || echo 'Piper not found - check installation'
test -f stt_models/vosk-model-en-us-0.22-lgraph/conf/model.conf && echo 'Vosk model OK' || echo 'Vosk model missing'
test -f tts_models/en_US-hfc_male-medium.onnx && echo 'Piper model OK' || echo 'Piper model missing'
"
```

**Note:**
Environment file setup is completed in step 3 above using the production-ready `.env.server` file.
The server environment variables are not avaible in the `.env` file.
If you have to commit remove all access keys from `.env.server`.

### Phase 3: Web Server Configuration

1. **Create Gunicorn configuration:**

```bash
sudo -u brainstormx bash -c "
cat > /home/brainstormx/brainstorm_x/gunicorn.conf.py << 'EOF'

import multiprocessing

bind = '127.0.0.1:5001'

# Socket.IO requires single worker with eventlet
workers = 1
worker_class = 'eventlet'

worker_connections = 1000
max_requests = 1000
max_requests_jitter = 50
timeout = 120
keepalive = 2
preload_app = True

EOF
"
```

2. **Create systemd service:**

```bash
sudo tee /etc/systemd/system/brainstormx.service > /dev/null << 'EOF'
[Unit]
Description=BrainStormX Flask Application
After=network.target

[Service]
User=brainstormx
Group=brainstormx
WorkingDirectory=/home/brainstormx/brainstorm_x
Environment=PATH=/home/brainstormx/brainstorm_x/venv/bin
ExecStart=/home/brainstormx/brainstorm_x/venv/bin/gunicorn -c gunicorn.conf.py run:app
ExecReload=/bin/kill -s HUP $MAINPID
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target

EOF
```

3. **Configure Nginx:**

```bash
# First, ensure directory permissions for static files (CRITICAL FOR STATIC FILE ACCESS)
sudo chmod 755 /home/brainstormx
sudo chmod 755 /home/brainstormx/brainstorm_x
sudo chmod -R 755 /home/brainstormx/brainstorm_x/app/static
sudo chmod -R 755 /home/brainstormx/brainstorm_x/instance

# Create Nginx configuration
sudo tee /etc/nginx/sites-available/brainstormx > /dev/null << 'EOF'
server {
    listen 80;
    server_name brainstormx.ca www.brainstormx.ca;
  
    # Redirect HTTP to HTTPS (will be uncommented after SSL setup)
    # return 301 https://$server_name$request_uri;
  
    # Temporary direct proxy (for initial SSL setup)
    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
  
        # Socket.IO support
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_cache_bypass $http_upgrade;
  
        # Timeouts
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
  
    # Static files
    location /static/ {
        alias /home/brainstormx/brainstorm_x/app/static/;
        expires 1d;
        add_header Cache-Control "public, immutable";
    }
  
    # Media files
    location /media/ {
        alias /home/brainstormx/brainstorm_x/instance/uploads/;
        expires 1d;
    }
  
    # Security headers
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;
    add_header X-XSS-Protection "1; mode=block";
}
EOF

# Enable site
sudo ln -sf /etc/nginx/sites-available/brainstormx /etc/nginx/sites-enabled/
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
```

### Phase 4: SSL Certificate Setup

1. **Start services:**

```bash
sudo systemctl daemon-reload
sudo systemctl enable brainstormx
sudo systemctl start brainstormx
sudo systemctl restart nginx
```

2. **Verify application is running:**

```bash
sudo systemctl status brainstormx
curl -I http://127.0.0.1:5001
curl -I http://54.90.225.8
curl -I http://brainstormx.ca
```

3. **Obtain SSL certificate:**

```bash
sudo certbot --nginx -d brainstormx.ca -d www.brainstormx.ca --email patrick@broadcomms.net --agree-tos --no-eff-email
```

4. **Update Nginx for HTTPS redirect:**

```bash
# sudo nano /etc/nginx/sites-available/brainstormx
# Uncomment the HTTPS redirect line:
# return 301 https://$server_name$request_uri;
# cat /etc/nginx/sites-available/brainstormx

sudo nginx -t
sudo systemctl reload nginx
```

5. **Setup auto-renewal:**

```bash
sudo systemctl enable certbot.timer
sudo systemctl start certbot.timer
```

### Phase 5: Monitoring & Maintenance

1. **Setup log rotation:**

```bash
sudo tee /etc/logrotate.d/brainstormx > /dev/null << 'EOF'
/home/brainstormx/brainstorm_x/instance/logs/*.log {
    daily
    missingok
    rotate 7
    compress
    delaycompress
    notifempty
    copytruncate
    su brainstormx brainstormx
}
EOF
```

2. **Create backup script:**

```bash
# Create backup script (avoiding history expansion issues)
sudo -u brainstormx tee /home/brainstormx/backup.sh > /dev/null << 'EOF'
#!/bin/bash
BACKUP_DIR=/home/brainstormx/backups
DATE=$(date +%Y%m%d_%H%M%S)
mkdir -p $BACKUP_DIR

# Backup database
cp /home/brainstormx/brainstorm_x/instance/app_database.sqlite $BACKUP_DIR/database_$DATE.sqlite

# Backup uploads
tar -czf $BACKUP_DIR/uploads_$DATE.tar.gz -C /home/brainstormx/brainstorm_x/instance uploads/

# Keep only last 7 days
find $BACKUP_DIR -name '*.sqlite' -mtime +7 -delete
find $BACKUP_DIR -name '*.tar.gz' -mtime +7 -delete
EOF

# Make backup script executable
sudo chmod +x /home/brainstormx/backup.sh
sudo chown brainstormx:brainstormx /home/brainstormx/backup.sh

# Setup daily backup cron
sudo -u brainstormx bash -c "
(crontab -l 2>/dev/null; echo '0 2 * * * /home/brainstormx/backup.sh') | crontab -
"

# Verify cron was set up
sudo -u brainstormx crontab -l
```

## Post-Deployment Verification

### 1. Health Checks

```bash
# Service status
sudo systemctl status brainstormx nginx

# Application logs
sudo journalctl -u brainstormx -f

# Nginx logs
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log

# Application health
curl -I https://brainstormx.ca
```

### 2. SSL Verification

```bash
# Test SSL certificate
openssl s_client -connect brainstormx.ca:443 -servername brainstormx.ca

# Check certificate expiry
echo | openssl s_client -connect brainstormx.ca:443 -servername brainstormx.ca 2>/dev/null | openssl x509 -noout -dates
```

### 3. Application Testing

- [ ] Homepage loads correctly
- [ ] Socket.IO connections work
- [ ] User registration/login
- [ ] Email verification
- [ ] Workshop creation and joining
- [ ] File uploads function
- [ ] AI features respond

## Troubleshooting

### Common Issues

1. **Application won't start:**

```bash
# Check logs
sudo journalctl -u brainstormx --no-pager
# Check Python path and dependencies
sudo -u brainstormx bash -c "cd /home/brainstormx/brainstorm_x && source venv/bin/activate && python -c 'import app'"
```

2. **Static files return 403 Forbidden:**

This is a common issue that occurs when Nginx doesn't have proper permissions to access files in user home directories.

```bash
# Fix directory permissions (most common solution)
sudo chmod 755 /home/brainstormx
sudo chmod 755 /home/brainstormx/brainstorm_x  
sudo chmod -R 755 /home/brainstormx/brainstorm_x/app/static
sudo chmod -R 755 /home/brainstormx/brainstorm_x/instance

# Verify permissions are correct
ls -la /home/brainstormx
ls -la /home/brainstormx/brainstorm_x/app/static

# Test static file access directly
curl -I https://brainstormx.ca/static/styles.css

# Check Nginx error logs for permission errors
sudo tail -f /var/log/nginx/error.log
```

3. **SSL certificate issues:**

```bash
# Check certificate status
sudo certbot certificates

# Renew certificate manually if needed
sudo certbot renew --dry-run

# Check certificate expiry
echo | openssl s_client -connect brainstormx.ca:443 2>/dev/null | openssl x509 -noout -dates
```

### Log Locations

- **Application:** `/home/brainstormx/brainstorm_x/instance/logs/`
- **Gunicorn:** `sudo journalctl -u brainstormx`
- **Nginx:** `/var/log/nginx/`
- **Certbot:** `/var/log/letsencrypt/`

## Security Hardening

1. **Firewall Configuration:**

```bash
sudo ufw enable
sudo ufw allow ssh
sudo ufw allow 'Nginx Full'
sudo ufw status
```

2. **File Permissions:**

```bash
# Secure application files
sudo chown -R brainstormx:brainstormx /home/brainstormx/brainstorm_x
chmod 600 /home/brainstormx/brainstorm_x/.env
chmod 755 /home/brainstormx/brainstorm_x/instance
```

3. **Regular Updates:**

```bash
# System updates
sudo apt update && sudo apt upgrade
# Python package updates
sudo -u brainstormx bash -c "cd /home/brainstormx/brainstorm_x && source venv/bin/activate && pip list --outdated"
```

## Maintenance Tasks

### Daily

- [ ] Check application status
- [ ] Review error logs
- [ ] Monitor disk space

### Weekly

- [ ] Review backup integrity
- [ ] Check SSL certificate status
- [ ] Update system packages

### Monthly

- [ ] Review Python dependencies for updates
- [ ] Clean old log files
- [ ] Performance analysis

## Rollback Plan

1. **Stop services:**

```bash
sudo systemctl stop brainstormx nginx
```

2. **Restore from backup:**

```bash
# Restore database
cp /home/brainstormx/backups/database_YYYYMMDD_HHMMSS.sqlite /home/brainstormx/brainstorm_x/instance/app_database.sqlite

# Restore uploads
tar -xzf /home/brainstormx/backups/uploads_YYYYMMDD_HHMMSS.tar.gz -C /home/brainstormx/brainstorm_x/instance/
```

3. **Restart services:**

```bash
sudo systemctl start brainstormx nginx
```

## Update files on Server

```bash
# SSH into your server
ssh -i "instance/brainstorm_x_vm_sshkey.pem" ubuntu@ec2-54-90-225-8.compute-1.amazonaws.com

# Switch to brainstormx user
sudo -u brainstormx bash

# Navigate to the application directory
cd /home/brainstormx/brainstorm_x

# Pull the latest changes from the production branch
git pull origin production

# Username for 'https://github.com'
# broadcomms

# Password
# Copy from brainstorm_x_git_access_token.txt

#

# Restart the application to pick up changes
sudo systemctl restart brainstormx

# Check that the service restarted successfully
sudo systemctl status brainstormx

# Verify the update was successful
echo "Checking application health..."
curl -I https://brainstormx.ca

# Check recent application logs
sudo journalctl -u brainstormx -n 10 --no-pager

# Verify updated files are in place
ls -la /home/brainstormx/brainstorm_x/app/account/templates/account_create.html
```

### comprehensive synchronization of server to production branch

```bash
# 1. Set the pull strategy to merge (creates a merge commit)
git config pull.rebase false

# 2. Pull with merge strategy using your GitHub credentials
git pull origin production
# Username: broadcomms
# Password: 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

# 3. After successful merge, restart the application
sudo systemctl restart brainstormx

# 4. Verify service is running
sudo systemctl status brainstormx

```

### Complete Server-Production Synchronizaton Verification

```bash
# 1. Check git status to confirm everything is synchronized
git status

# 2. Verify you're on the latest commit
git log --oneline -5

# 3. Compare with remote branch
git fetch origin production
git log --oneline --graph --decorate origin/production..HEAD

# 4. If the above shows no commits, you're fully synchronized
# If it shows commits, run: git push origin production

# 5. Clean up any temporary files
rm -f tts_models/en_US-hfc_male-medium.onnx.1
rm -f tts_models/en_US-hfc_male-medium.onnx.json.1

# 6. Verify application is working with latest changes
curl -I https://brainstormx.ca
curl -I https://brainstormx.ca/auth/register
```

## Update database on server

### Accessing SQLite Database

```bash
# SSH into server and switch to application user
ssh -i "instance/brainstorm_x_vm_sshkey.pem" ubuntu@ec2-54-90-225-8.compute-1.amazonaws.com
sudo -u brainstormx bash
cd /home/brainstormx/brainstorm_x

# Connect to SQLite database
sqlite3 instance/app_database.sqlite
```

### Common Database Operations

```sql
-- View all tables
.tables

-- View table structure
.schema users

-- View current users and their roles
SELECT user_id, email, first_name, last_name, role FROM users;

-- Make a user admin
UPDATE users SET role = 'admin' WHERE email = 'patrick@broadcomms.net';

-- Verify changes
SELECT user_id, email, first_name, last_name, role FROM users WHERE email = 'patrick@broadcomms.net';

-- Exit SQLite
.quit
```

### Database Backup Before Changes

```bash
# Always backup before making changes
cp instance/app_database.sqlite instance/app_database_backup_$(date +%Y%m%d_%H%M%S).sqlite

# Verify backup was created
ls -la instance/app_database_backup_*
```

### Python Script for Complex Operations

```bash
# Create a database management script
sudo -u brainstormx tee /home/brainstormx/db_operations.py > /dev/null << 'EOF'
import sqlite3
import sys
import os
from datetime import datetime

os.chdir('/home/brainstormx/brainstorm_x')

def connect_db():
    return sqlite3.connect('instance/app_database.sqlite')

def show_users():
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, email, first_name, last_name, role, is_active, created_at FROM users ORDER BY created_at DESC")
    users = cursor.fetchall()
    print("\n=== Current Users ===")
    print("ID | Email | Name | Role | Active | Created")
    print("-" * 80)
    for user in users:
        name = f"{user[2] or ''} {user[3] or ''}".strip()
        print(f"{user[0]} | {user[1]} | {name} | {user[4]} | {user[5]} | {user[6]}")
    conn.close()

def make_admin(email):
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET role = ? WHERE email = ?", ('admin', email))
    if cursor.rowcount > 0:
        conn.commit()
        print(f"✅ Updated {email} to admin role")
    else:
        print(f"❌ No user found with email: {email}")
    conn.close()

def show_workshops():
    conn = connect_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, created_by, status, created_at FROM workshops ORDER BY created_at DESC LIMIT 10")
    workshops = cursor.fetchall()
    print("\n=== Recent Workshops ===")
    print("ID | Title | Created By | Status | Created")
    print("-" * 60)
    for workshop in workshops:
        print(f"{workshop[0]} | {workshop[1][:30]} | {workshop[2]} | {workshop[3]} | {workshop[4]}")
    conn.close()

if __name__ == "__main__":
    print("BrainStormX Database Operations")
    print("=" * 40)
  
    if len(sys.argv) > 1:
        if sys.argv[1] == "make_admin" and len(sys.argv) > 2:
            make_admin(sys.argv[2])
        elif sys.argv[1] == "users":
            show_users()
        elif sys.argv[1] == "workshops":
            show_workshops()
    else:
        print("Usage:")
        print("  python3 db_operations.py users          # Show all users")
        print("  python3 db_operations.py workshops      # Show recent workshops")
        print("  python3 db_operations.py make_admin <email>  # Make user admin")
EOF

# Make it executable
chmod +x /home/brainstormx/db_operations.py

# Examples of usage:
# Show users: python3 /home/brainstormx/db_operations.py users
# Make admin: python3 /home/brainstormx/db_operations.py make_admin patrick@broadcomms.net
# Show workshops: python3 /home/brainstormx/db_operations.py workshops
```

### Quick Admin Setup Commands

```bash
# Quick one-liner to make yourself admin
sudo -u brainstormx bash -c "cd /home/brainstormx/brainstorm_x && echo \"UPDATE users SET role = 'admin' WHERE email = 'patrick@broadcomms.net';\" | sqlite3 instance/app_database.sqlite"

# Verify the change
sudo -u brainstormx bash -c "cd /home/brainstormx/brainstorm_x && echo \"SELECT email, role FROM users WHERE email = 'patrick@broadcomms.net';\" | sqlite3 instance/app_database.sqlite"
```

## Performance Optimization

### Gunicorn Tuning

- Adjust worker count based on CPU cores
- Monitor memory usage per worker
- Configure appropriate timeouts

### Nginx Optimization

```nginx
# Add to nginx config for better performance
gzip on;
gzip_types text/plain text/css application/json application/javascript text/xml application/xml;
client_max_body_size 10M;
```

### Database Optimization

- Regular VACUUM for SQLite
- Monitor database size growth
- Consider PostgreSQL for high-traffic scenarios

## Deployment Summary

### Quick Deployment Overview

This deployment guide has been updated and validated based on real-world deployment experience. Following these steps will result in:

✅ **Working Application:** BrainStormX running on port 5001 via Gunicorn with proper domain SSL
✅ **SSL/HTTPS:** Let's Encrypt certificate with auto-renewal for full SSL functionality
✅ **Static Files:** CSS, JavaScript, and images loading correctly with proper directory permissions
✅ **Nginx Proxy:** HTTP to HTTPS redirect with proper headers
✅ **AI Features:** AWS Bedrock integration with Piper TTS and Vosk STT properly installed
✅ **Systemd Services:** Auto-start on boot and crash recovery
✅ **Domain Access:** Full production access via `https://brainstormx.ca`

### Key Configuration Files Created:

- `/etc/nginx/sites-available/brainstormx` - Nginx configuration with SSL
- `/etc/systemd/system/brainstormx.service` - SystemD service configuration
- `/home/brainstormx/brainstorm_x/.env` - Production environment variables
- `/etc/letsencrypt/live/brainstormx.ca/` - Let's Encrypt SSL certificates

### Access URLs:

- **Production Site:** `https://brainstormx.ca`
- **Direct Application:** `http://127.0.0.1:5001` (local only)

### Critical Success Factors:

1. **Domain DNS:** A record pointing `brainstormx.ca` to `54.90.225.8`
2. **Directory Permissions:** `chmod 755` on home directories for Nginx access
3. **SSL Certificate:** Let's Encrypt certificate with auto-renewal for production SSL
4. **AI Models:** Piper TTS and Vosk properly installed and configured
5. **Environment File:** Production `.env.server` configuration with working credentials

---

**Deployment Contact:** patrick@broadcomms.net
**Last Updated:** October 13, 2025
**Version:** 1.1
