#!/bin/bash

# =============================================================================
#!/bin/bash

# BrainStormX EC2 Auto Deployment Script
# Version: 1.3.1
# Description: Automated deployment of BrainStormX on Ubuntu 24.04 LTS
# 
# CHANGELOG:
# v1.3.1 - Fixed variable scope issue causing "unbound variable" error in email configuration
# v1.3.0 - Added EC2 instance reset functionality and enhanced user experience
# v1.2.0 - Enhanced with interactive configuration collection for AWS and email settings
# v1.1.0 - Added IMDSv2 support and improved error handling
# v1.0.0 - Initial release with basic deployment functionality
# =============================================================================
# This script fully automates the deployment of BrainStormX on Ubuntu EC2
# Usage: Run this script on a fresh Ubuntu 24.04 LTS EC2 instance
# Prerequisites: Instance should have internet access and correct security groups
# =============================================================================

set -e  # Exit on any error
set -u  # Exit on undefined variables

# Color codes for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
PURPLE='\033[0;35m'
CYAN='\033[0;36m'
WHITE='\033[1;37m'
NC='\033[0m' # No Color

# Configuration variables
SCRIPT_VERSION="1.3.1"
APP_USER="brainstormx"
APP_DIR="/home/${APP_USER}/BrainStormX"
REPO_URL="https://github.com/broadcomms/BrainStormX.git"
REPO_BRANCH="main"
LOG_FILE="/tmp/brainstormx_deploy.log"

# Auto-detect EC2 metadata (supports both IMDSv1 and IMDSv2)
detect_ec2_metadata() {
    # Try IMDSv2 first (token-based)
    local TOKEN
    TOKEN=$(curl -X PUT "http://169.254.169.254/latest/api/token" \
        -H "X-aws-ec2-metadata-token-ttl-seconds: 21600" \
        -s --max-time 3 2>/dev/null)
    
    if [[ -n "$TOKEN" ]]; then
        # IMDSv2 available, use token-based requests
        PUBLIC_HOSTNAME=$(curl -H "X-aws-ec2-metadata-token: $TOKEN" \
            -s --max-time 3 http://169.254.169.254/latest/meta-data/public-hostname 2>/dev/null)
        PUBLIC_IP=$(curl -H "X-aws-ec2-metadata-token: $TOKEN" \
            -s --max-time 3 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null)
        INSTANCE_ID=$(curl -H "X-aws-ec2-metadata-token: $TOKEN" \
            -s --max-time 3 http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null)
        AVAILABILITY_ZONE=$(curl -H "X-aws-ec2-metadata-token: $TOKEN" \
            -s --max-time 3 http://169.254.169.254/latest/meta-data/placement/availability-zone 2>/dev/null)
    else
        # Fall back to IMDSv1 (direct requests)
        if curl -s --max-time 3 http://169.254.169.254/latest/meta-data/ > /dev/null 2>&1; then
            PUBLIC_HOSTNAME=$(curl -s --max-time 3 http://169.254.169.254/latest/meta-data/public-hostname 2>/dev/null)
            PUBLIC_IP=$(curl -s --max-time 3 http://169.254.169.254/latest/meta-data/public-ipv4 2>/dev/null)
            INSTANCE_ID=$(curl -s --max-time 3 http://169.254.169.254/latest/meta-data/instance-id 2>/dev/null)
            AVAILABILITY_ZONE=$(curl -s --max-time 3 http://169.254.169.254/latest/meta-data/placement/availability-zone 2>/dev/null)
        fi
    fi
    
    # Validate that we got meaningful data
    if [[ -n "$PUBLIC_HOSTNAME" && -n "$PUBLIC_IP" && -n "$INSTANCE_ID" ]]; then
        echo -e "${GREEN}✓ Detected EC2 environment:${NC}"
        echo -e "  Instance ID: ${CYAN}${INSTANCE_ID}${NC}"
        echo -e "  Public IP: ${CYAN}${PUBLIC_IP}${NC}"
        echo -e "  Public DNS: ${CYAN}${PUBLIC_HOSTNAME}${NC}"
        echo -e "  AZ: ${CYAN}${AVAILABILITY_ZONE}${NC}"
        return 0
    else
        echo -e "${YELLOW}⚠ Warning: Not running on EC2 or metadata service unavailable${NC}"
        PUBLIC_HOSTNAME="localhost"
        PUBLIC_IP="127.0.0.1"
        INSTANCE_ID="unknown"
        AVAILABILITY_ZONE="unknown"
        return 1
    fi
}

# Call the metadata detection function
detect_ec2_metadata

# Logging function
log() {
    echo -e "$1" | tee -a "${LOG_FILE}"
}

# Progress tracking
print_header() {
    echo -e "\n${PURPLE}==============================================================================${NC}"
    echo -e "${WHITE}$1${NC}"
    echo -e "${PURPLE}==============================================================================${NC}\n"
}

print_step() {
    echo -e "${BLUE}➤ $1${NC}"
}

print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠ $1${NC}"
}

print_error() {
    echo -e "${RED}✗ $1${NC}"
}

# Error handler
error_exit() {
    print_error "Error occurred in deployment script at line $1"
    print_error "Check log file: ${LOG_FILE}"
    print_error "Last few log entries:"
    tail -10 "${LOG_FILE}"
    exit 1
}

trap 'error_exit $LINENO' ERR

# System info
get_system_info() {
    print_header "SYSTEM INFORMATION"
    log "${GREEN}Operating System:${NC} $(lsb_release -d | cut -f2)"
    log "${GREEN}Kernel Version:${NC} $(uname -r)"
    log "${GREEN}Architecture:${NC} $(uname -m)"
    log "${GREEN}Memory:${NC} $(free -h | grep Mem | awk '{print $2}')"
    log "${GREEN}Disk Space:${NC} $(df -h / | tail -1 | awk '{print $4}') available"
    log "${GREEN}CPU Cores:${NC} $(nproc)"
    echo ""
}

# Pre-flight checks
pre_flight_checks() {
    print_header "PRE-FLIGHT CHECKS"
    
    # Check if running as root or with sudo
    if [[ $EUID -eq 0 ]]; then
        print_warning "Running as root user. This is acceptable for initial setup."
    elif sudo -n true 2>/dev/null; then
        print_success "Sudo access confirmed"
    else
        print_error "This script requires sudo access. Please run with sudo or as root."
        exit 1
    fi
    
    # Check internet connectivity
    print_step "Checking internet connectivity..."
    if curl -s --max-time 5 https://www.google.com > /dev/null; then
        print_success "Internet connectivity confirmed"
    else
        print_error "No internet connectivity. Please check network settings."
        exit 1
    fi
    
    # Check available disk space (minimum 8GB)
    AVAILABLE_SPACE=$(df / | tail -1 | awk '{print $4}')
    if [[ $AVAILABLE_SPACE -lt 8388608 ]]; then  # 8GB in KB
        print_error "Insufficient disk space. Minimum 8GB required."
        exit 1
    fi
    print_success "Sufficient disk space available"
    
    # Check system compatibility
    if [[ $(lsb_release -rs | sed 's/\..*//') -lt 20 ]]; then
        print_warning "Ubuntu version may be too old. Ubuntu 20.04+ recommended."
    else
        print_success "Ubuntu version compatible"
    fi
    
    print_success "All pre-flight checks passed"
}

# System updates and dependencies
install_system_dependencies() {
    print_header "INSTALLING SYSTEM DEPENDENCIES"
    
    print_step "Updating package lists..."
    apt update >> "${LOG_FILE}" 2>&1
    
    print_step "Upgrading system packages..."
    DEBIAN_FRONTEND=noninteractive apt upgrade -y >> "${LOG_FILE}" 2>&1
    
    print_step "Installing core system packages..."
    DEBIAN_FRONTEND=noninteractive apt install -y \
        python3 python3-pip python3-venv python3-dev \
        nginx git curl wget unzip \
        build-essential libssl-dev libffi-dev \
        sqlite3 \
        tesseract-ocr libtesseract-dev \
        ffmpeg \
        certbot python3-certbot-nginx \
        htop nano vim \
        ufw \
        >> "${LOG_FILE}" 2>&1
    
    print_step "Installing additional libraries for TTS..."
    DEBIAN_FRONTEND=noninteractive apt install -y libespeak-ng1 >> "${LOG_FILE}" 2>&1
    
    print_success "System dependencies installed successfully"
}

# Create application user
create_app_user() {
    print_header "CREATING APPLICATION USER"
    
    if id "${APP_USER}" &>/dev/null; then
        print_warning "User ${APP_USER} already exists"
    else
        print_step "Creating user ${APP_USER}..."
        useradd -m -s /bin/bash "${APP_USER}"
        usermod -aG sudo "${APP_USER}"
        print_success "User ${APP_USER} created successfully"
    fi
    
    # Set user password (generate random password)
    TEMP_PASSWORD=$(openssl rand -base64 12)
    echo "${APP_USER}:${TEMP_PASSWORD}" | chpasswd
    print_success "Temporary password set for ${APP_USER}: ${TEMP_PASSWORD}"
    echo "Temporary password for ${APP_USER}: ${TEMP_PASSWORD}" >> "${LOG_FILE}"
}

# Download application code
download_application() {
    print_header "DOWNLOADING APPLICATION CODE"
    
    print_step "Cloning BrainStormX repository..."
    if [[ -d "${APP_DIR}" ]]; then
        print_warning "Application directory already exists, updating..."
        sudo -u "${APP_USER}" bash -c "cd ${APP_DIR} && git pull origin ${REPO_BRANCH}" >> "${LOG_FILE}" 2>&1
    else
        sudo -u "${APP_USER}" git clone -b "${REPO_BRANCH}" "${REPO_URL}" "${APP_DIR}" >> "${LOG_FILE}" 2>&1
    fi
    
    print_success "Application code downloaded successfully"
}

# Setup Python environment
setup_python_environment() {
    print_header "SETTING UP PYTHON ENVIRONMENT"
    
    print_step "Creating Python virtual environment..."
    sudo -u "${APP_USER}" bash -c "cd ${APP_DIR} && python3 -m venv venv" >> "${LOG_FILE}" 2>&1
    
    print_step "Upgrading pip..."
    sudo -u "${APP_USER}" bash -c "cd ${APP_DIR} && source venv/bin/activate && pip install --upgrade pip" >> "${LOG_FILE}" 2>&1
    
    print_step "Installing Python dependencies..."
    sudo -u "${APP_USER}" bash -c "cd ${APP_DIR} && source venv/bin/activate && pip install -r requirements.txt" >> "${LOG_FILE}" 2>&1
    
    print_step "Installing additional production dependencies..."
    sudo -u "${APP_USER}" bash -c "cd ${APP_DIR} && source venv/bin/activate && pip install gunicorn eventlet" >> "${LOG_FILE}" 2>&1
    
    print_success "Python environment setup completed"
}

# Install Piper TTS
install_piper_tts() {
    print_header "INSTALLING PIPER TTS ENGINE"
    
    print_step "Downloading Piper TTS binary..."
    sudo -u "${APP_USER}" bash -c "
        cd ${APP_DIR}
        wget -q https://github.com/rhasspy/piper/releases/latest/download/piper_linux_x86_64.tar.gz
        tar -xzf piper_linux_x86_64.tar.gz
        cp piper/piper venv/bin/
        chmod +x venv/bin/piper
        cp piper/lib*.so* venv/lib/ 2>/dev/null || true
    " >> "${LOG_FILE}" 2>&1
    
    print_step "Installing Piper system libraries..."
    mkdir -p /usr/share/espeak-ng-data
    cp -r "${APP_DIR}/piper/espeak-ng-data/"* /usr/share/espeak-ng-data/ 2>/dev/null || true
    cp "${APP_DIR}/piper/lib"*.so* /usr/local/lib/ 2>/dev/null || true
    cp "${APP_DIR}/piper/libespeak-ng.so"* /usr/local/lib/ 2>/dev/null || true
    ldconfig
    
    print_step "Cleaning up Piper installation files..."
    sudo -u "${APP_USER}" bash -c "cd ${APP_DIR} && rm -rf piper piper_linux_x86_64.tar.gz" >> "${LOG_FILE}" 2>&1
    
    print_step "Verifying Piper installation..."
    if sudo -u "${APP_USER}" bash -c "cd ${APP_DIR} && source venv/bin/activate && ./venv/bin/piper --version" >> "${LOG_FILE}" 2>&1; then
        print_success "Piper TTS installed successfully"
    else
        print_warning "Piper TTS installation may have issues"
    fi
}

# Install Vosk model
install_vosk_model() {
    print_header "INSTALLING VOSK SPEECH RECOGNITION MODEL"
    
    print_step "Creating STT models directory..."
    sudo -u "${APP_USER}" mkdir -p "${APP_DIR}/stt_models"
    
    print_step "Downloading Vosk model (this may take a few minutes)..."
    sudo -u "${APP_USER}" bash -c "
        cd ${APP_DIR}/stt_models
        wget -q https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip
        unzip -q vosk-model-en-us-0.22-lgraph.zip
        rm vosk-model-en-us-0.22-lgraph.zip
    " >> "${LOG_FILE}" 2>&1
    
    print_step "Verifying Vosk model installation..."
    if sudo -u "${APP_USER}" test -f "${APP_DIR}/stt_models/vosk-model-en-us-0.22-lgraph/conf/model.conf"; then
        print_success "Vosk model installed successfully"
    else
        print_warning "Vosk model installation may have issues"
    fi
}

# Install TTS models
install_tts_models() {
    print_header "INSTALLING TTS MODELS"
    
    print_step "Creating TTS models directory..."
    sudo -u "${APP_USER}" mkdir -p "${APP_DIR}/tts_models"
    
    print_step "Downloading Piper TTS models..."
    sudo -u "${APP_USER}" bash -c "
        cd ${APP_DIR}/tts_models
        wget -q https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/hfc_male/medium/en_US-hfc_male-medium.onnx
        wget -q https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/hfc_male/medium/en_US-hfc_male-medium.onnx.json
    " >> "${LOG_FILE}" 2>&1
    
    print_step "Verifying TTS model installation..."
    if sudo -u "${APP_USER}" test -f "${APP_DIR}/tts_models/en_US-hfc_male-medium.onnx"; then
        print_success "TTS models installed successfully"
    else
        print_warning "TTS model installation may have issues"
    fi
}

# Collect user configuration
collect_user_configuration() {
    print_header "USER CONFIGURATION SETUP"
    
    # Declare variables as global to ensure they persist outside function scope
    declare -g AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY MAIL_SERVER MAIL_USERNAME MAIL_PASSWORD MAIL_SUPPRESS_SEND
    declare -g AGENTCORE_MEMORY_ENABLED AGENTCORE_MEMORY_ID AGENTCORE_MEMORY_ARN
    
    echo -e "${CYAN}BrainStormX requires AWS Bedrock credentials to provide AI features.${NC}"
    echo -e "${CYAN}You'll also have the option to configure email settings for user registration.${NC}"
    echo ""
    
    # AWS Credentials (Mandatory)
    print_step "Configuring AWS Bedrock credentials..."
    echo -e "${YELLOW}AWS credentials are REQUIRED for AI functionality.${NC}"
    echo ""
    
    while true; do
        echo -e "${WHITE}Please enter your AWS Access Key ID:${NC}"
        read -r AWS_ACCESS_KEY_ID
        
        if [[ -n "$AWS_ACCESS_KEY_ID" ]]; then
            break
        else
            echo -e "${RED}AWS Access Key ID cannot be empty. Please try again.${NC}"
        fi
    done
    
    while true; do
        echo -e "${WHITE}Please enter your AWS Secret Access Key:${NC}"
        read -rs AWS_SECRET_ACCESS_KEY  # -s flag hides input
        echo ""  # Add newline since input was hidden
        
        if [[ -n "$AWS_SECRET_ACCESS_KEY" ]]; then
            break
        else
            echo -e "${RED}AWS Secret Access Key cannot be empty. Please try again.${NC}"
        fi
    done
    
    print_success "AWS credentials configured"
    
    # Mail Configuration (Optional)
    print_step "Configuring email settings..."
    echo -e "${YELLOW}Email configuration is OPTIONAL but recommended for user registration and notifications.${NC}"
    echo -e "${CYAN}Press Enter to skip any field to use default settings.${NC}"
    echo ""
    
    echo -e "${WHITE}Enter SMTP server (or press Enter for default: server108.web-hosting.com):${NC}"
    read -r MAIL_SERVER_INPUT
    MAIL_SERVER="${MAIL_SERVER_INPUT:-server108.web-hosting.com}"
    
    echo -e "${WHITE}Enter SMTP username (or press Enter for default: no-reply@broadcomms.net):${NC}"
    read -r MAIL_USERNAME_INPUT
    MAIL_USERNAME="${MAIL_USERNAME_INPUT:-no-reply@broadcomms.net}"
    
    if [[ -n "$MAIL_USERNAME_INPUT" ]]; then
        echo -e "${WHITE}Enter SMTP password:${NC}"
        read -rs MAIL_PASSWORD
        echo ""
        MAIL_SUPPRESS_SEND="False"
    else
        MAIL_PASSWORD="5i-8v@S4y$Y?"
        MAIL_SUPPRESS_SEND="True"
    fi
    
    print_success "Email configuration completed"
    
    # AgentCore Configuration (Optional)
    echo ""
    echo -e "${WHITE}Do you have AWS Bedrock AgentCore Memory configured? (y/N):${NC}"
    read -n 1 -r AGENTCORE_RESPONSE
    echo ""
    
    if [[ $AGENTCORE_RESPONSE =~ ^[Yy]$ ]]; then
        echo -e "${WHITE}Enter AgentCore Memory ID:${NC}"
        read -r AGENTCORE_MEMORY_ID
        
        echo -e "${WHITE}Enter AgentCore Memory ARN:${NC}"
        read -r AGENTCORE_MEMORY_ARN
        
        AGENTCORE_MEMORY_ENABLED="true"
    else
        AGENTCORE_MEMORY_ID=""
        AGENTCORE_MEMORY_ARN=""
        AGENTCORE_MEMORY_ENABLED="false"
    fi
    
    print_success "Configuration collection completed"
    
    # Display configuration summary
    echo ""
    echo -e "${CYAN}Configuration Summary:${NC}"
    echo -e "  AWS Region: ${WHITE}us-east-1${NC}"
    echo -e "  AWS Access Key: ${WHITE}${AWS_ACCESS_KEY_ID}${NC}"
    echo -e "  Mail Server: ${WHITE}${MAIL_SERVER}${NC}"
    echo -e "  Mail Username: ${WHITE}${MAIL_USERNAME}${NC}"
    echo -e "  AgentCore Memory: ${WHITE}${AGENTCORE_MEMORY_ENABLED}${NC}"
    echo ""
}

# Setup environment configuration
setup_environment_config() {
    print_header "CREATING APPLICATION ENVIRONMENT FILE"
    
    print_step "Generating production environment configuration..."
    
    # Generate a secure secret key
    SECRET_KEY=$(openssl rand -hex 32)
    
    # Create the .env file with collected configuration
    cat > /tmp/brainstormx_env << EOF
# Production Configuration
FLASK_ENV=production
SECRET_KEY=${SECRET_KEY}
DEBUG=false

# AWS Bedrock Configuration
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=${AWS_ACCESS_KEY_ID}
AWS_SECRET_ACCESS_KEY=${AWS_SECRET_ACCESS_KEY}
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
MAIL_SERVER=${MAIL_SERVER}
MAIL_PORT=587
MAIL_USE_TLS=True
MAIL_USE_SSL=False
MAIL_USERNAME=${MAIL_USERNAME}
MAIL_PASSWORD=${MAIL_PASSWORD}
MAIL_DEFAULT_SENDER=${MAIL_USERNAME}
MAIL_SUPPRESS_SEND=${MAIL_SUPPRESS_SEND}
MAIL_USE_RELAXED_SSL=True

# AgentCore settings
AGENTCORE_MEMORY_ENABLED=${AGENTCORE_MEMORY_ENABLED}
EOF
    
    # Add AgentCore settings conditionally
    if [[ -n "$AGENTCORE_MEMORY_ID" ]]; then
        echo "AGENTCORE_MEMORY_ID=${AGENTCORE_MEMORY_ID}" >> /tmp/brainstormx_env
    else
        echo "# AGENTCORE_MEMORY_ID=your-memory-id" >> /tmp/brainstormx_env
    fi
    
    if [[ -n "$AGENTCORE_MEMORY_ARN" ]]; then
        echo "AGENTCORE_MEMORY_ARN=${AGENTCORE_MEMORY_ARN}" >> /tmp/brainstormx_env
    else
        echo "# AGENTCORE_MEMORY_ARN=your-memory-arn" >> /tmp/brainstormx_env
    fi
    
    # Add the rest of the configuration
    cat >> /tmp/brainstormx_env << 'EOF'
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

# TTS and STT Configuration
TRANSCRIPTION_PROVIDER=vosk
TTS_PROVIDER=piper
STT_PROVIDER=vosk

# AWS Transcribe settings
AWS_TRANSCRIBE_LANGUAGE_CODE=en-US
AWS_TRANSCRIBE_SAMPLE_RATE=16000
AWS_TRANSCRIBE_MEDIA_ENCODING=pcm

# Tool Configuration
TOOL_TIMEOUT_SECONDS=12
TOOL_MAX_WORKERS=4
CIRCUIT_BREAKER_THRESHOLD=3
CIRCUIT_BREAKER_RESET_SECONDS=60

# Assistant Configuration
ASSISTANT_THREADS_ENABLED=true
ASSISTANT_STRICT_JSON=true
EOF
    
    # Add dynamic paths that need to be resolved at runtime
    echo "VOSK_MODEL_PATH=${APP_DIR}/stt_models/vosk-model-en-us-0.22-lgraph" >> /tmp/brainstormx_env
    echo "PIPER_BIN=${APP_DIR}/venv/bin/piper" >> /tmp/brainstormx_env
    echo "PIPER_MODEL=${APP_DIR}/tts_models/en_US-hfc_male-medium.onnx" >> /tmp/brainstormx_env
    
    # Move the file to the application directory with proper ownership
    sudo cp /tmp/brainstormx_env "${APP_DIR}/.env"
    rm /tmp/brainstormx_env
    
    chmod 600 "${APP_DIR}/.env"
    chown "${APP_USER}:${APP_USER}" "${APP_DIR}/.env"
    
    print_success "Production environment file created with your configuration"
    print_success "AWS Bedrock AI features are now ready to use"
    
    if [[ "$MAIL_SUPPRESS_SEND" == "False" ]]; then
        print_success "Email notifications are enabled with your SMTP settings"
    else
        print_warning "Using default email settings - user registration emails will be suppressed"
    fi
}

# Test application
test_application() {
    print_header "TESTING APPLICATION SETUP"
    
    print_step "Testing Python application initialization..."
    if sudo -u "${APP_USER}" bash -c "cd ${APP_DIR} && source venv/bin/activate && python3 -c 'from app import create_app; app = create_app(); print(\"App creation successful\")'"; then
        print_success "Application initializes correctly"
    else
        print_warning "Application initialization test failed - check dependencies"
    fi
    
    print_step "Testing Piper TTS..."
    if sudo -u "${APP_USER}" bash -c "cd ${APP_DIR} && source venv/bin/activate && ./venv/bin/piper --version" >> "${LOG_FILE}" 2>&1; then
        print_success "Piper TTS working"
    else
        print_warning "Piper TTS test failed"
    fi
    
    print_step "Testing Vosk model..."
    if sudo -u "${APP_USER}" test -f "${APP_DIR}/stt_models/vosk-model-en-us-0.22-lgraph/conf/model.conf"; then
        print_success "Vosk model accessible"
    else
        print_warning "Vosk model not found"
    fi
}

# Configure Gunicorn
configure_gunicorn() {
    print_header "CONFIGURING GUNICORN WSGI SERVER"
    
    print_step "Creating Gunicorn configuration..."
    sudo -u "${APP_USER}" bash -c "cat > ${APP_DIR}/gunicorn.conf.py << 'EOF'
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

# Logging
accesslog = '-'
errorlog = '-'
loglevel = 'info'
EOF"
    
    print_success "Gunicorn configuration created"
}

# Configure systemd service
configure_systemd_service() {
    print_header "CONFIGURING SYSTEMD SERVICE"
    
    print_step "Creating systemd service file..."
    cat > /etc/systemd/system/brainstormx.service << EOF
[Unit]
Description=BrainStormX Flask Application
After=network.target

[Service]
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${APP_DIR}
Environment=PATH=${APP_DIR}/venv/bin
ExecStart=${APP_DIR}/venv/bin/gunicorn -c gunicorn.conf.py run:app
ExecReload=/bin/kill -s HUP \$MAINPID
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
    
    print_step "Enabling systemd service..."
    systemctl daemon-reload
    systemctl enable brainstormx
    
    print_success "SystemD service configured and enabled"
}

# Configure Nginx
configure_nginx() {
    print_header "CONFIGURING NGINX WEB SERVER"
    
    print_step "Setting directory permissions for Nginx access..."
    chmod 755 /home/"${APP_USER}"
    chmod 755 "${APP_DIR}"
    chmod -R 755 "${APP_DIR}/app/static" 2>/dev/null || true
    chmod -R 755 "${APP_DIR}/instance" 2>/dev/null || true
    
    print_step "Creating Nginx site configuration..."
    cat > /etc/nginx/sites-available/brainstormx << EOF
server {
    listen 80;
    server_name ${PUBLIC_HOSTNAME} ${PUBLIC_IP};
    
    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        
        # Socket.IO support
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_cache_bypass \$http_upgrade;
        
        # Timeouts
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
    
    # Static files
    location /static/ {
        alias ${APP_DIR}/app/static/;
        expires 1d;
        add_header Cache-Control "public, immutable";
        access_log off;
    }
    
    # Media files
    location /media/ {
        alias ${APP_DIR}/instance/uploads/;
        expires 1d;
        access_log off;
    }
    
    # Security headers
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;
    add_header X-XSS-Protection "1; mode=block";
    
    # Enable gzip compression
    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml application/xml+rss text/javascript;
}
EOF
    
    print_step "Enabling Nginx site..."
    ln -sf /etc/nginx/sites-available/brainstormx /etc/nginx/sites-enabled/
    rm -f /etc/nginx/sites-enabled/default
    
    print_step "Testing Nginx configuration..."
    if nginx -t >> "${LOG_FILE}" 2>&1; then
        print_success "Nginx configuration is valid"
    else
        print_error "Nginx configuration test failed"
        exit 1
    fi
}

# Setup SSL certificate
setup_ssl_certificate() {
    print_header "SETTING UP SSL CERTIFICATE"
    
    print_step "Creating self-signed SSL certificate..."
    openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
        -keyout /etc/ssl/private/brainstormx-selfsigned.key \
        -out /etc/ssl/certs/brainstormx-selfsigned.crt \
        -subj "/C=US/ST=State/L=City/O=BrainStormX/OU=IT/CN=${PUBLIC_HOSTNAME}" \
        >> "${LOG_FILE}" 2>&1
    
    print_step "Updating Nginx configuration for SSL..."
    cat > /etc/nginx/sites-available/brainstormx << EOF
server {
    listen 80;
    server_name ${PUBLIC_HOSTNAME} ${PUBLIC_IP};
    return 301 https://\$server_name\$request_uri;
}

server {
    listen 443 ssl http2;
    server_name ${PUBLIC_HOSTNAME} ${PUBLIC_IP};
    
    ssl_certificate /etc/ssl/certs/brainstormx-selfsigned.crt;
    ssl_certificate_key /etc/ssl/private/brainstormx-selfsigned.key;
    
    # SSL settings
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers ECDHE-RSA-AES256-GCM-SHA512:DHE-RSA-AES256-GCM-SHA512:ECDHE-RSA-AES256-GCM-SHA384:DHE-RSA-AES256-GCM-SHA384;
    ssl_prefer_server_ciphers off;
    ssl_session_cache shared:SSL:10m;
    ssl_session_timeout 10m;
    
    location / {
        proxy_pass http://127.0.0.1:5001;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        
        # Socket.IO support
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_cache_bypass \$http_upgrade;
        
        # Timeouts
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
    
    # Static files
    location /static/ {
        alias ${APP_DIR}/app/static/;
        expires 1d;
        add_header Cache-Control "public, immutable";
        access_log off;
    }
    
    # Media files
    location /media/ {
        alias ${APP_DIR}/instance/uploads/;
        expires 1d;
        access_log off;
    }
    
    # Security headers
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;
    add_header X-XSS-Protection "1; mode=block";
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    
    # Enable gzip compression
    gzip on;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml application/xml+rss text/javascript;
}
EOF
    
    print_step "Testing updated Nginx configuration..."
    if nginx -t >> "${LOG_FILE}" 2>&1; then
        print_success "SSL-enabled Nginx configuration is valid"
    else
        print_error "SSL Nginx configuration test failed"
        exit 1
    fi
    
    print_success "Self-signed SSL certificate created and configured"
    print_warning "Browsers will show security warning for self-signed certificates"
}

# Setup firewall
setup_firewall() {
    print_header "CONFIGURING FIREWALL"
    
    print_step "Configuring UFW firewall..."
    ufw --force reset >> "${LOG_FILE}" 2>&1
    ufw default deny incoming >> "${LOG_FILE}" 2>&1
    ufw default allow outgoing >> "${LOG_FILE}" 2>&1
    ufw allow ssh >> "${LOG_FILE}" 2>&1
    ufw allow 'Nginx Full' >> "${LOG_FILE}" 2>&1
    ufw --force enable >> "${LOG_FILE}" 2>&1
    
    print_success "Firewall configured and enabled"
}

# Setup backup script
setup_backup_script() {
    print_header "SETTING UP BACKUP SYSTEM"
    
    print_step "Creating backup script..."
    sudo -u "${APP_USER}" bash -c "cat > /home/${APP_USER}/backup.sh << 'EOF'
#!/bin/bash
BACKUP_DIR=/home/${APP_USER}/backups
DATE=\$(date +%Y%m%d_%H%M%S)
mkdir -p \$BACKUP_DIR

# Backup database
if [ -f ${APP_DIR}/instance/app_database.sqlite ]; then
    cp ${APP_DIR}/instance/app_database.sqlite \$BACKUP_DIR/database_\$DATE.sqlite
fi

# Backup uploads
if [ -d ${APP_DIR}/instance/uploads ]; then
    tar -czf \$BACKUP_DIR/uploads_\$DATE.tar.gz -C ${APP_DIR}/instance uploads/
fi

# Keep only last 7 days
find \$BACKUP_DIR -name '*.sqlite' -mtime +7 -delete
find \$BACKUP_DIR -name '*.tar.gz' -mtime +7 -delete

echo \"Backup completed: \$DATE\"
EOF"
    
    chmod +x "/home/${APP_USER}/backup.sh"
    chown "${APP_USER}:${APP_USER}" "/home/${APP_USER}/backup.sh"
    
    print_step "Setting up daily backup cron job..."
    sudo -u "${APP_USER}" bash -c "(crontab -l 2>/dev/null; echo '0 2 * * * /home/${APP_USER}/backup.sh >> /home/${APP_USER}/backup.log 2>&1') | crontab -"
    
    print_success "Backup system configured"
}

# Setup log rotation
setup_log_rotation() {
    print_header "CONFIGURING LOG ROTATION"
    
    print_step "Creating log rotation configuration..."
    cat > /etc/logrotate.d/brainstormx << EOF
${APP_DIR}/instance/logs/*.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    create 644 ${APP_USER} ${APP_USER}
    su ${APP_USER} ${APP_USER}
}
EOF
    
    print_success "Log rotation configured"
}

# Start services
start_services() {
    print_header "STARTING SERVICES"
    
    print_step "Starting BrainStormX application..."
    systemctl start brainstormx
    
    print_step "Restarting Nginx..."
    systemctl restart nginx
    
    print_step "Checking service status..."
    sleep 5
    
    if systemctl is-active --quiet brainstormx; then
        print_success "BrainStormX service is running"
    else
        print_error "BrainStormX service failed to start"
        systemctl status brainstormx
        exit 1
    fi
    
    if systemctl is-active --quiet nginx; then
        print_success "Nginx service is running"
    else
        print_error "Nginx service failed to start"
        systemctl status nginx
        exit 1
    fi
}

# Post-deployment verification
post_deployment_verification() {
    print_header "POST-DEPLOYMENT VERIFICATION"
    
    print_step "Testing local application response..."
    sleep 10  # Give services time to fully start
    
    if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5001 | grep -q "200\|302"; then
        print_success "Application responding locally"
    else
        print_warning "Application may not be responding correctly on port 5001"
    fi
    
    print_step "Testing HTTP to HTTPS redirect..."
    if curl -s -I "http://${PUBLIC_HOSTNAME}" | grep -q "301\|302"; then
        print_success "HTTP to HTTPS redirect working"
    else
        print_warning "HTTP redirect may not be working"
    fi
    
    print_step "Testing HTTPS response..."
    if curl -k -s -o /dev/null -w "%{http_code}" "https://${PUBLIC_HOSTNAME}" | grep -q "200"; then
        print_success "HTTPS response working"
    else
        print_warning "HTTPS may not be responding correctly"
    fi
    
    print_step "Testing static file access..."
    if curl -k -s -I "https://${PUBLIC_HOSTNAME}/static/css/main.css" | grep -q "200"; then
        print_success "Static files accessible"
    else
        print_warning "Static files may not be accessible"
    fi
}

# Generate deployment report
generate_deployment_report() {
    print_header "DEPLOYMENT REPORT"
    
    local REPORT_FILE="/home/${APP_USER}/deployment_report.txt"
    
    cat > "${REPORT_FILE}" << EOF
# BrainStormX Deployment Report
Generated: $(date)
Script Version: ${SCRIPT_VERSION}

## Instance Information
Instance ID: ${INSTANCE_ID}
Public IP: ${PUBLIC_IP}
Public DNS: ${PUBLIC_HOSTNAME}
Availability Zone: ${AVAILABILITY_ZONE}

## Access URLs
HTTP (redirects to HTTPS): http://${PUBLIC_HOSTNAME}
HTTPS: https://${PUBLIC_HOSTNAME}
Direct Application: http://127.0.0.1:5001 (local only)

## Service Status
$(systemctl is-active brainstormx >/dev/null && echo "✓ BrainStormX Service: Active" || echo "✗ BrainStormX Service: Inactive")
$(systemctl is-active nginx >/dev/null && echo "✓ Nginx Service: Active" || echo "✗ Nginx Service: Inactive")
$(systemctl is-active ufw >/dev/null && echo "✓ Firewall: Active" || echo "✗ Firewall: Inactive")

## Configuration Files
- Application Code: ${APP_DIR}
- Environment File: ${APP_DIR}/.env
- Nginx Config: /etc/nginx/sites-available/brainstormx
- SystemD Service: /etc/systemd/system/brainstormx.service
- SSL Certificate: /etc/ssl/certs/brainstormx-selfsigned.crt
- SSL Private Key: /etc/ssl/private/brainstormx-selfsigned.key

## Log Locations
- Application Logs: ${APP_DIR}/instance/logs/
- System Logs: sudo journalctl -u brainstormx
- Nginx Logs: /var/log/nginx/
- Deployment Log: ${LOG_FILE}

## Important Notes
1. SSL certificate is self-signed - browsers will show security warnings
2. AWS credentials need to be configured in ${APP_DIR}/.env
3. Mail settings need to be configured for user registration
4. Default admin user will be created on first run
5. Backup script runs daily at 2 AM via cron

## Quick Commands
- Check service status: sudo systemctl status brainstormx nginx
- View logs: sudo journalctl -u brainstormx -f
- Restart services: sudo systemctl restart brainstormx nginx
- View application logs: tail -f ${APP_DIR}/instance/logs/app.log

## Security Recommendations
1. Change default passwords
2. Configure proper AWS IAM roles instead of access keys
3. Set up proper domain name and real SSL certificate
4. Configure fail2ban for additional security
5. Regular security updates

## Troubleshooting
If services don't start:
1. Check logs: sudo journalctl -u brainstormx
2. Verify environment: sudo -u ${APP_USER} bash -c "cd ${APP_DIR} && source venv/bin/activate && python3 -c 'from app import create_app; create_app()'"
3. Check permissions: ls -la ${APP_DIR}
4. Test directly: sudo -u ${APP_USER} bash -c "cd ${APP_DIR} && source venv/bin/activate && python run.py"

For support, contact: patrick@broadcomms.net
EOF
    
    chown "${APP_USER}:${APP_USER}" "${REPORT_FILE}"
    
    print_success "Deployment report generated: ${REPORT_FILE}"
}

# Reset function to clean up previous installations
reset_instance() {
    print_header "RESETTING EC2 INSTANCE"
    
    print_step "Checking for existing BrainStormX installation..."
    
    # Stop and disable services if they exist
    if systemctl is-active --quiet brainstormx 2>/dev/null; then
        print_step "Stopping BrainStormX service..."
        systemctl stop brainstormx || true
        systemctl disable brainstormx || true
        print_success "BrainStormX service stopped"
    fi
    
    if systemctl is-active --quiet nginx 2>/dev/null; then
        print_step "Stopping Nginx service..."
        systemctl stop nginx || true
        print_success "Nginx service stopped"
    fi
    
    # Remove systemd service files
    if [[ -f /etc/systemd/system/brainstormx.service ]]; then
        print_step "Removing systemd service files..."
        rm -f /etc/systemd/system/brainstormx.service
        systemctl daemon-reload
        print_success "SystemD service files removed"
    fi
    
    # Remove Nginx configuration
    if [[ -f /etc/nginx/sites-available/brainstormx ]]; then
        print_step "Removing Nginx configuration..."
        rm -f /etc/nginx/sites-available/brainstormx
        rm -f /etc/nginx/sites-enabled/brainstormx
        # Restore default Nginx site if it doesn't exist
        if [[ ! -f /etc/nginx/sites-enabled/default ]] && [[ -f /etc/nginx/sites-available/default ]]; then
            ln -s /etc/nginx/sites-available/default /etc/nginx/sites-enabled/default
        fi
        # Test Nginx configuration and restart if valid
        if nginx -t > /dev/null 2>&1; then
            systemctl restart nginx > /dev/null 2>&1 || true
        fi
        print_success "Nginx configuration removed and service restored"
    fi
    
    # Remove SSL certificates
    if [[ -f /etc/ssl/certs/brainstormx-selfsigned.crt ]]; then
        print_step "Removing SSL certificates..."
        rm -f /etc/ssl/certs/brainstormx-selfsigned.crt
        rm -f /etc/ssl/private/brainstormx-selfsigned.key
        print_success "SSL certificates removed"
    fi
    
    # Remove application directory and user
    if [[ -d "${APP_DIR}" ]]; then
        print_step "Removing application directory..."
        rm -rf "${APP_DIR}"
        print_success "Application directory removed"
    fi
    
    if id "${APP_USER}" &>/dev/null; then
        print_step "Removing application user..."
        # Remove user's cron jobs
        sudo -u "${APP_USER}" crontab -r 2>/dev/null || true
        # Remove user and home directory
        userdel -r "${APP_USER}" 2>/dev/null || true
        print_success "Application user removed"
    fi
    
    # Remove log rotation configuration
    if [[ -f /etc/logrotate.d/brainstormx ]]; then
        print_step "Removing log rotation configuration..."
        rm -f /etc/logrotate.d/brainstormx
        print_success "Log rotation configuration removed"
    fi
    
    # Reset firewall to default state
    print_step "Resetting firewall to default state..."
    ufw --force reset > /dev/null 2>&1 || true
    ufw default deny incoming > /dev/null 2>&1 || true
    ufw default allow outgoing > /dev/null 2>&1 || true
    ufw allow ssh > /dev/null 2>&1 || true
    print_success "Firewall reset to default state"
    
    # Clean up temporary files
    print_step "Cleaning up temporary files..."
    rm -f /tmp/brainstormx_* 2>/dev/null || true
    rm -f "${LOG_FILE}" 2>/dev/null || true
    print_success "Temporary files cleaned"
    
    # Remove any leftover Python packages (optional - keeps system packages)
    # This is commented out to avoid breaking system Python installations
    # print_step "Cleaning Python package cache..."
    # pip3 cache purge 2>/dev/null || true
    
    print_success "Instance reset completed - ready for fresh installation"
    echo ""
}

# Check for existing installation
check_existing_installation() {
    local has_existing=false
    
    # Check for various signs of existing installation
    if [[ -d "${APP_DIR}" ]] || \
       [[ -f /etc/systemd/system/brainstormx.service ]] || \
       [[ -f /etc/nginx/sites-available/brainstormx ]] || \
       id "${APP_USER}" &>/dev/null; then
        has_existing=true
    fi
    
    if [[ "$has_existing" == true ]]; then
        echo -e "${YELLOW}"
        echo "=============================================================================="
        echo "             EXISTING BRAINSTORMX INSTALLATION DETECTED"
        echo "=============================================================================="
        echo -e "${NC}"
        echo "An existing BrainStormX installation has been found on this instance."
        echo "You have the following options:"
        echo ""
        echo -e "${WHITE}1) Reset and reinstall${NC} - Remove existing installation and start fresh"
        echo -e "${WHITE}2) Continue anyway${NC} - Attempt installation over existing setup (not recommended)"
        echo -e "${WHITE}3) Exit${NC} - Cancel installation"
        echo ""
        
        while true; do
            read -p "Please select an option (1/2/3): " -n 1 -r EXISTING_CHOICE
            echo ""
            
            case $EXISTING_CHOICE in
                1)
                    echo -e "${CYAN}Resetting instance for fresh installation...${NC}"
                    reset_instance
                    return 0
                    ;;
                2)
                    echo -e "${YELLOW}⚠ Warning: Continuing with existing installation may cause conflicts.${NC}"
                    read -p "Are you sure you want to continue? (y/N): " -n 1 -r CONFIRM
                    echo ""
                    if [[ $CONFIRM =~ ^[Yy]$ ]]; then
                        return 0
                    fi
                    ;;
                3)
                    echo "Installation cancelled."
                    exit 0
                    ;;
                *)
                    echo -e "${RED}Invalid option. Please select 1, 2, or 3.${NC}"
                    ;;
            esac
        done
    fi
}

# Main deployment function
main() {
    echo -e "${GREEN}"
    echo "=============================================================================="
    echo "             BrainStormX EC2 Automated Deployment Script v${SCRIPT_VERSION}"
    echo "=============================================================================="
    echo -e "${NC}"
    echo "This script will completely set up BrainStormX on this Ubuntu EC2 instance."
    echo "The process will take approximately 10-15 minutes depending on network speed."
    echo ""
    
    # Check for existing installation first
    check_existing_installation
    
    echo "What this script will do:"
    echo "  ✓ Collect your AWS and email configuration"
    echo "  ✓ Install all system dependencies"
    echo "  ✓ Create application user and environment"
    echo "  ✓ Download and configure BrainStormX"
    echo "  ✓ Install Piper TTS and Vosk STT"
    echo "  ✓ Configure Gunicorn, Nginx, and SSL"
    echo "  ✓ Set up systemd services and firewall"
    echo "  ✓ Configure backups and log rotation"
    echo ""
    read -p "Do you want to proceed with the installation? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Installation cancelled."
        exit 0
    fi
    
    # Start deployment
    echo "Starting deployment at $(date)" > "${LOG_FILE}"
    
    get_system_info
    pre_flight_checks
    collect_user_configuration
    install_system_dependencies
    create_app_user
    download_application
    setup_python_environment
    install_piper_tts
    install_vosk_model
    install_tts_models
    setup_environment_config
    test_application
    configure_gunicorn
    configure_systemd_service
    configure_nginx
    setup_ssl_certificate
    setup_firewall
    setup_backup_script
    setup_log_rotation
    start_services
    post_deployment_verification
    generate_deployment_report
    
    # Final success message
    print_header "DEPLOYMENT COMPLETED SUCCESSFULLY!"
    
    echo -e "${GREEN}🎉 BrainStormX has been successfully deployed!${NC}\n"
    
    echo -e "${CYAN}Access your application at:${NC}"
    echo -e "  🌐 HTTPS: ${WHITE}https://${PUBLIC_HOSTNAME}${NC}"
    echo -e "  🌐 HTTP:  ${WHITE}http://${PUBLIC_HOSTNAME}${NC} (redirects to HTTPS)"
    echo ""
    
    echo -e "${YELLOW}⚠️  Important Notes:${NC}"
    echo -e "  • Browsers will show security warning for self-signed SSL certificate"
    echo -e "  • AWS Bedrock AI features are configured and ready to use"
    if [[ "$MAIL_SUPPRESS_SEND" == "False" ]]; then
        echo -e "  • Email notifications are enabled with your SMTP settings"
    else
        echo -e "  • Email notifications are using default settings (suppressed)"
    fi
    echo -e "  • Deployment report saved to: ${WHITE}/home/${APP_USER}/deployment_report.txt${NC}"
    echo ""
    
    echo -e "${BLUE}Quick Commands:${NC}"
    echo -e "  • Check services: ${WHITE}sudo systemctl status brainstormx nginx${NC}"
    echo -e "  • View logs: ${WHITE}sudo journalctl -u brainstormx -f${NC}"
    echo -e "  • Restart: ${WHITE}sudo systemctl restart brainstormx nginx${NC}"
    echo -e "  • Reset instance: ${WHITE}sudo ./ec2_auto_deploy.sh --reset${NC}"
    echo ""
    
    echo -e "${GREEN}For support, contact: patrick@broadcomms.net${NC}"
    echo -e "${GREEN}Deployment completed at: $(date)${NC}"
}

# Parse command line arguments
case "${1:-}" in
    --reset|reset|-r)
        echo -e "${GREEN}"
        echo "=============================================================================="
        echo "             BrainStormX EC2 Instance Reset Tool v${SCRIPT_VERSION}"
        echo "=============================================================================="
        echo -e "${NC}"
        echo "This will completely remove any existing BrainStormX installation."
        echo -e "${YELLOW}⚠ WARNING: This action cannot be undone!${NC}"
        echo ""
        echo "The following will be removed:"
        echo "  • BrainStormX application and data"
        echo "  • Application user and home directory"
        echo "  • Nginx and SSL configuration"
        echo "  • SystemD services and cron jobs"
        echo "  • Firewall rules (reset to default)"
        echo ""
        read -p "Are you sure you want to reset this instance? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            detect_ec2_metadata
            reset_instance
            echo -e "${GREEN}✅ Instance reset completed successfully!${NC}"
            echo -e "${CYAN}You can now run the deployment script again for a fresh installation.${NC}"
        else
            echo "Reset cancelled."
        fi
        exit 0
        ;;
    --help|help|-h)
        echo -e "${GREEN}BrainStormX EC2 Deployment Script v${SCRIPT_VERSION}${NC}"
        echo ""
        echo "Usage:"
        echo "  $0                 - Run full deployment"
        echo "  $0 --reset         - Reset instance (remove existing installation)"
        echo "  $0 --help          - Show this help message"
        echo ""
        echo "Examples:"
        echo "  sudo $0                    # Normal installation"
        echo "  sudo $0 --reset           # Reset instance before reinstalling"
        echo ""
        echo "For support, contact: patrick@broadcomms.net"
        exit 0
        ;;
    "")
        # No arguments, run main deployment
        main "$@"
        ;;
    *)
        echo -e "${RED}Error: Unknown argument '$1'${NC}"
        echo "Use '$0 --help' for usage information."
        exit 1
        ;;
esac