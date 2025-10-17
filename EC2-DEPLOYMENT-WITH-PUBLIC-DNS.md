# BrainStormX Production Deployment on EC2 with PUBLIC DNS and SSL Certificate

## Overview

This document outlines the complete deployment process for BrainStormX to AWS EC2 with Public DNS and SSL certificate.

## Production Details
**NAME**`BrainStomX_EC2`

**VERSION**`1.0.0`


**Target Environment:**


- **Server Type:** `AWS EC2`
- **OS Image** `Ubuntu Server 24.04 LTS (HVM), SSD Volume Type, Canonical amd64 noble image`
- **Instance type** `t3.small, 2 vCPU, 2 Gib Memory`
- **Security Group** `Allow HTTPS/HTTPS Anywhere, Allow SSH/From IP`
- **Boot Storage** `32 GiB gp3`
- **Public IP Address:** `13.222.58.210`
- **Public DNS:** `ec2-13-222-58-210.compute-1.amazonaws.com`
- **SSH Access:** `ssh -i <public_key_path> ubuntu@ec2-13-222-58-210.compute-1.amazonaws.com`
- **Admin SSH:** `ssh -i "instance/brainstorm_x_vm_sshkey.pem" brainstormx@13.222.58.210 -p 22`


## Architecture Analysis

### Technology Stack

- **Framework:** Flask application server with Socket.IO for real-time features
- **Database:** SQLite (production ready for current scale, supports PostgreSQL through SQLAlchemy)
- **AI Integration:** AWS Bedrock (Nova models, AgentCore Memory)
- **Python Version:** 3.10+ required
- **Web Server:** Nginx (reverse proxy) + Gunicorn (WSGI server)
- **SSL Certification:** Self-signed certificate (Let's Encrypt not available for EC2 default domains)
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
- **RAM:** Minimum 2GB (recommended 4GB+)
- **Storage:** 32GB SSD (recommended 50GB+)
- **Python:** 3.10+
- **Network:** Port 80, 443, 22 open
- **Domain:** DNS pointing to server IP
- **Application Admin:** `brainstormx`



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

### 2. AWS Configuration

- [ ] Verify AWS credentials for Bedrock access (ACCESS KEY ID, SECRET ACCESS KEY)
- [ ] Test Required Bedrock model access (Nova Lite - amazon.nova-lite-v1:0, Nova Pro - amazon.nova-pro-v1:0)
- [ ] Configure IAM permissions for required services

### 3. Environment Setup

- [ ] Use production `.env.server` file (already configured with production values)
- [ ] Verify secure SECRET_KEY is set
- [ ] Verify mail settings are configured
- [ ] Verify AWS credentials and Bedrock access
- [ ] Verify Piper TTS and Vosk paths match server deployment

### 4. Security Groups (AWS EC2)

- [ ] Port 22 (SSH) - Your IP only
- [ ] Port 80 (HTTP) - 0.0.0.0/0
- [ ] Port 443 (HTTPS) - 0.0.0.0/0
- [ ] Port 5001 (App) - Local only (127.0.0.1)

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
ssh -i "instance/brainstorm_x_vm_sshkey.pem" ubuntu@ec2-13-222-58-210.compute-1.amazonaws.com echo "SSH connection successful"

# Connect to EC2 instance
ssh -i "instance/brainstorm_x_vm_sshkey.pem" ubuntu@ec2-13-222-58-210.compute-1.amazonaws.com
```

2. **Install system dependencies:**

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv nginx git curl wget unzip
sudo apt install -y build-essential libssl-dev libffi-dev python3-dev
sudo apt install -y sqlite3 # For SQLite database support
sudo apt install -y tesseract-ocr libtesseract-dev
sudo apt install -y ffmpeg # For audio processing (Vosk transcription)
sudo apt install -y certbot python3-certbot-nginx # SSL certificates
```

3. **Create application user:**

```bash
sudo useradd -m -s /bin/bash brainstormx
sudo usermod -aG sudo brainstormx
# Set password for brainstormx user
sudo passwd brainstormx

# Start interactive session as brainstormx user
sudo -u brainstormx bash

# Or to run specific commands as brainstormx user, use:
# sudo -u brainstormx bash -c "command"

```

Once you are in the brainstormx user session, you can proceed with the application deployment.

### Phase 2: Application Deployment

1. **Download application code to Server:**

```bash
# Option A: Git clone production code (recommended, Generate secret key token for access)
sudo -u brainstormx git clone -b production https://github.com/broadcomms/brainstorm_x.git /home/brainstormx/brainstorm_x

# Option B: SCP to transfre files directly from your local machine to server
scp -i "instance/brainstorm_x_vm_sshkey.pem" -r . ubuntu@ec2-13-222-58-210.compute-1.amazonaws.com:/tmp/brainstorm_x
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
pip install gunicorn eventlet
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
scp -i "instance/brainstorm_x_vm_sshkey.pem" .env.server ubuntu@ec2-13-222-58-210.compute-1.amazonaws.com:/tmp/.env.server

# Then on the SERVER, move it to the correct location:
sudo mv /tmp/.env.server /home/brainstormx/brainstorm_x/.env
sudo chown brainstormx:brainstormx /home/brainstormx/brainstorm_x/.env
sudo chmod 600 /home/brainstormx/brainstorm_x/.env

# Option B: If .env.server is not available locally, create it on server
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

3. **Configure Nginx (Initial HTTP Configuration):**

```bash
# First, ensure directory permissions for static files (CRITICAL FOR STATIC FILE ACCESS)
sudo chmod 755 /home/brainstormx
sudo chmod 755 /home/brainstormx/brainstorm_x
sudo chmod -R 755 /home/brainstormx/brainstorm_x/app/static
sudo chmod -R 755 /home/brainstormx/brainstorm_x/instance

# Create initial Nginx configuration (HTTP only - will be updated for SSL in Phase 4)
sudo tee /etc/nginx/sites-available/brainstormx > /dev/null << 'EOF'
server {
    listen 80;
    server_name ec2-13-222-58-210.compute-1.amazonaws.com;
  
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
  
    # Static files - Fixed permissions ensure proper access
    location /static/ {
        alias /home/brainstormx/brainstorm_x/app/static/;
        expires 1d;
        add_header Cache-Control "public, immutable";
        
        # Ensure proper file access
        try_files $uri $uri/ =404;
        
        # Security for static files
        location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot)$ {
            expires 1y;
            add_header Cache-Control "public, immutable";
            add_header Access-Control-Allow-Origin "*";
        }
    }
  
    # Media files
    location /media/ {
        alias /home/brainstormx/brainstorm_x/instance/uploads/;
        expires 1d;
        try_files $uri $uri/ =404;
    }
  
    # Security headers
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;
    add_header X-XSS-Protection "1; mode=block";
}
EOF

# Enable site and test configuration
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
curl -I http://13.222.58.210
curl -I ec2-13-222-58-210.compute-1.amazonaws.com
```

3. **Setup SSL Certificate (Self-Signed):**

**Important:** Let's Encrypt has policy restrictions that prevent issuing certificates for AWS EC2 default domain names (like `ec2-*.amazonaws.com`) because AWS owns these domains, not you. Therefore, we use a self-signed certificate.

**Self-Signed Certificate Setup (Required for WebRTC Features):**

```bash
# Create self-signed certificate
sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /etc/ssl/private/brainstormx-selfsigned.key \
  -out /etc/ssl/certs/brainstormx-selfsigned.crt \
  -subj "/C=US/ST=State/L=City/O=Organization/OU=OrgUnit/CN=ec2-13-222-58-210.compute-1.amazonaws.com"

# Update Nginx configuration to use self-signed certificate
sudo tee /etc/nginx/sites-available/brainstormx > /dev/null << 'EOF'
server {
    listen 80;
    server_name ec2-13-222-58-210.compute-1.amazonaws.com;
    return 301 https://$server_name$request_uri;
}

server {
    listen 443 ssl http2;
    server_name ec2-13-222-58-210.compute-1.amazonaws.com;

    ssl_certificate /etc/ssl/certs/brainstormx-selfsigned.crt;
    ssl_certificate_key /etc/ssl/private/brainstormx-selfsigned.key;
    
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;
    ssl_prefer_server_ciphers on;

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

    # Static files - Ensure permissions and access
    location /static/ {
        alias /home/brainstormx/brainstorm_x/app/static/;
        expires 1d;
        add_header Cache-Control "public, immutable";
        
        # Ensure proper file access
        try_files $uri $uri/ =404;
        
        # Security for static files
        location ~* \.(js|css|png|jpg|jpeg|gif|ico|svg|woff|woff2|ttf|eot)$ {
            expires 1y;
            add_header Cache-Control "public, immutable";
            add_header Access-Control-Allow-Origin "*";
        }
    }

    # Media files
    location /media/ {
        alias /home/brainstormx/brainstorm_x/instance/uploads/;
        expires 1d;
        try_files $uri $uri/ =404;
    }

    # Security headers
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;
    add_header X-XSS-Protection "1; mode=block";
}
EOF

# Test and reload Nginx
sudo nginx -t && sudo systemctl reload nginx
```

4. **Verify SSL Setup:**

```bash
# Test HTTPS access (ignore certificate warnings - this is expected for self-signed)
curl -I -k https://ec2-13-222-58-210.compute-1.amazonaws.com

# Test HTTP redirect to HTTPS
curl -I http://ec2-13-222-58-210.compute-1.amazonaws.com

# Verify static files work over HTTPS
curl -I -k https://ec2-13-222-58-210.compute-1.amazonaws.com/static/styles.css
```

**Note for Users:** When accessing the site via HTTPS, browsers will show a security warning for the self-signed certificate. This is normal and expected. Users should:
1. Click "Advanced" or "More Information" 
2. Click "Proceed to ec2-13-222-58-210.compute-1.amazonaws.com (unsafe)" or similar option
3. The site will then load normally with full HTTPS functionality

**Alternative: Use Your Own Domain (Optional)**
If you have your own domain name, point it to your EC2 instance for proper SSL:

```bash
# First, point your domain DNS A record to your EC2 IP: 13.222.58.210
# Then request certificate for your domain:
# sudo certbot --nginx -d yourdomain.com --email patrick@broadcomms.net --agree-tos --no-eff-email
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
sudo -u brainstormx bash -c "
cat > /home/brainstormx/backup.sh << 'EOF'
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

chmod +x /home/brainstormx/backup.sh
"

# Setup daily backup cron
sudo -u brainstormx bash -c "
(crontab -l 2>/dev/null; echo '0 2 * * * /home/brainstormx/backup.sh') | crontab -
"
```

## Deployment Summary

### Quick Deployment Overview

This deployment guide has been tested and validated. Following these steps will result in:

✅ **Working Application:** BrainStormX running on port 5001 via Gunicorn  
✅ **SSL/HTTPS:** Self-signed certificate enabling WebRTC features  
✅ **Static Files:** CSS, JavaScript, and images loading correctly  
✅ **Nginx Proxy:** HTTP to HTTPS redirect with proper headers  
✅ **AI Features:** AWS Bedrock integration with Piper TTS and Vosk STT  
✅ **Systemd Services:** Auto-start on boot and crash recovery  

### Key Configuration Files Created:
- `/etc/nginx/sites-available/brainstormx` - Nginx configuration with SSL
- `/etc/systemd/system/brainstormx.service` - SystemD service configuration
- `/home/brainstormx/brainstorm_x/.env` - Production environment variables
- `/etc/ssl/certs/brainstormx-selfsigned.crt` - SSL certificate
- `/etc/ssl/private/brainstormx-selfsigned.key` - SSL private key

### Access URLs:
- **Production Site:** `https://ec2-13-222-58-210.compute-1.amazonaws.com`
- **Direct Application:** `http://127.0.0.1:5001` (local only)

### Critical Success Factors:
1. **Directory Permissions:** `chmod 755` on home directories for Nginx access
2. **Static File Permissions:** `chmod -R 755` on static and instance directories
3. **SSL Certificate:** Self-signed certificate required for WebRTC camera access
4. **Service Dependencies:** Application must start before Nginx can proxy requests

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
curl -I https://ec2-13-222-58-210.compute-1.amazonaws.com
```

### 2. SSL Verification

```bash
# Test SSL certificate
openssl s_client -connect ec2-13-222-58-210.compute-1.amazonaws.com:443 -servername ec2-13-222-58-210.compute-1.amazonaws.com

# Check certificate expiry
echo | openssl s_client -connect ec2-13-222-58-210.compute-1.amazonaws.com:443 -servername ec2-13-222-58-210.compute-1.amazonaws.com 2>/dev/null | openssl x509 -noout -dates
```

### 3. Application Testing

```bash
# Verify all components are working
sudo -u brainstormx bash -c "
cd /home/brainstormx/brainstorm_x
source venv/bin/activate

# Test Piper TTS installation
./venv/bin/piper --version
echo 'Hello from Piper TTS' | ./venv/bin/piper --model tts_models/en_US-hfc_male-medium.onnx --output_file /tmp/test.wav

# Test Vosk model is present
ls -la stt_models/vosk-model-en-us-0.22-lgraph/conf/model.conf

# Test environment variables are loaded
python3 -c 'import os; print(f\"TTS Provider: {os.environ.get(\"TTS_PROVIDER\")}\"); print(f\"Transcription Provider: {os.environ.get(\"TRANSCRIPTION_PROVIDER\")}\")'

# Test AWS Bedrock connection
python3 -c 'import boto3; client = boto3.client(\"bedrock-runtime\", region_name=\"us-east-1\"); print(\"AWS Bedrock connection successful\")'
"
```

**Manual Testing Checklist:**
- [ ] Homepage loads correctly at `https://ec2-13-222-58-210.compute-1.amazonaws.com` (accept certificate warning)
- [ ] Static files load (CSS styling, JavaScript functionality, profile images)
- [ ] Socket.IO connections work (real-time features)
- [ ] User registration/login functions
- [ ] Email verification works
- [ ] File uploads function properly
- [ ] Workshop creation and joining works
- [ ] WebRTC camera access works (requires HTTPS)
- [ ] AI features respond (test with Bedrock models)
- [ ] Workshop begins and task advancement works
- [ ] Piper TTS voice features work in workshops
- [ ] Vosk transcription works during workshops

## Troubleshooting

### Common Issues

1. **Application won't start:**

```bash
# Check logs
sudo journalctl -u brainstormx --no-pager -n 50

# Check Python path and dependencies
sudo -u brainstormx bash -c "cd /home/brainstormx/brainstorm_x && source venv/bin/activate && python -c 'import app'"

# Check environment file
sudo -u brainstormx bash -c "cd /home/brainstormx/brainstorm_x && ls -la .env && grep -E '^(PIPER_BIN|VOSK_MODEL_PATH|TTS_PROVIDER)' .env"
```

2. **Piper TTS Issues:**

```bash
# Test Piper installation
sudo -u brainstormx bash -c "cd /home/brainstormx/brainstorm_x && source venv/bin/activate && ./venv/bin/piper --version"

# Check Piper model exists
sudo -u brainstormx bash -c "ls -la /home/brainstormx/brainstorm_x/tts_models/en_US-hfc_male-medium.onnx"

# Test espeak-ng libraries
ldconfig -p | grep espeak
ls -la /usr/share/espeak-ng-data/
```

3. **Vosk Transcription Issues:**

```bash
# Check Vosk model installation
sudo -u brainstormx bash -c "ls -la /home/brainstormx/brainstorm_x/stt_models/vosk-model-en-us-0.22-lgraph/"
sudo -u brainstormx bash -c "test -f /home/brainstormx/brainstorm_x/stt_models/vosk-model-en-us-0.22-lgraph/conf/model.conf && echo 'Vosk model OK'"

# Test Python vosk module
sudo -u brainstormx bash -c "cd /home/brainstormx/brainstorm_x && source venv/bin/activate && python -c 'import vosk; print(\"Vosk import successful\")'"
```

4. **Static Files 403 Forbidden Issues (MOST COMMON ISSUE):**

**Root Cause:** Nginx (www-data user) cannot access files in user home directories due to restrictive permissions.

**Solution:**
```bash
# Fix directory permissions (CRITICAL - must be done in order)
sudo chmod 755 /home/brainstormx                                    # Allow Nginx to traverse to home dir
sudo chmod 755 /home/brainstormx/brainstorm_x                      # Allow access to app directory  
sudo chmod -R 755 /home/brainstormx/brainstorm_x/app/static        # Make static files readable
sudo chmod -R 755 /home/brainstormx/brainstorm_x/instance          # Make upload directory accessible

# Verify permissions are correct
ls -la /home/brainstormx/                                           # Should show drwxr-xr-x
ls -la /home/brainstormx/brainstorm_x/app/static/                  # Should show drwxr-xr-x

# Test static file access (should return 200 OK)
curl -I https://ec2-13-222-58-210.compute-1.amazonaws.com/static/styles.css
curl -I https://ec2-13-222-58-210.compute-1.amazonaws.com/static/scripts.js

# Check if Nginx user can access files directly
sudo -u www-data ls -la /home/brainstormx/brainstorm_x/app/static/

# Monitor Nginx error logs for permission issues
sudo tail -f /var/log/nginx/error.log | grep -E "(static|403|permission)"
```

**Prevention:** Always run permission commands during initial deployment (included in Phase 3, Step 3).

5. **SSL Certificate Issues:**

```bash
# Check certificate status
sudo certbot certificates

# Test certificate manually
openssl s_client -connect ec2-13-222-58-210.compute-1.amazonaws.com:443 -servername ec2-13-222-58-210.compute-1.amazonaws.com < /dev/null

# Renew certificate if needed
sudo certbot renew --dry-run
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
sudo chmod 600 /home/brainstormx/brainstorm_x/.env
sudo chmod 755 /home/brainstormx/brainstorm_x/instance
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

## Rollback/Recovery Plan

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
- Monitor database file size growth
- Switch to PostgreSQL via SQLAlchemy for high-traffic scenarios

## Quick Reference Commands

### Service Management
```bash
# Check service status
sudo systemctl status brainstormx nginx

# Restart services
sudo systemctl restart brainstormx nginx

# View application logs
sudo journalctl -u brainstormx -f

# View Nginx logs
sudo tail -f /var/log/nginx/error.log
```

### Fix Common Issues
```bash
# Fix static file permissions (403 errors)
sudo chmod 755 /home/brainstormx
sudo chmod 755 /home/brainstormx/brainstorm_x
sudo chmod -R 755 /home/brainstormx/brainstorm_x/app/static

# Test application directly
curl -I http://127.0.0.1:5001

# Test SSL certificate
curl -I -k https://ec2-13-222-58-210.compute-1.amazonaws.com
```

### Important File Locations
- **Application Code:** `/home/brainstormx/brainstorm_x/`
- **Environment File:** `/home/brainstormx/brainstorm_x/.env`
- **Nginx Config:** `/etc/nginx/sites-available/brainstormx`
- **SystemD Service:** `/etc/systemd/system/brainstormx.service`
- **SSL Certificate:** `/etc/ssl/certs/brainstormx-selfsigned.crt`
- **Application Logs:** `/home/brainstormx/brainstorm_x/instance/logs/`

---
**Publisher:** BroadComms (https://www.broadcomms.net)
**Deployment Support Contact:** patrick@broadcomms.net
**Last Updated:** October 13, 2025
**Version:** 1.1 (Updated with tested configurations)
