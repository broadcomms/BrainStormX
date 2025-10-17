#!/bin/bash
 
# =============================================================================
# BrainStormX Deployment Validation Script
# =============================================================================
# This script validates that a BrainStormX deployment is working correctly
# Run this after the automated deployment completes
# =============================================================================

set -e


# Color codes
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# Configuration
APP_USER="brainstormx"
APP_DIR="/home/${APP_USER}/brainstorm_x"

# Get system info
if curl -s --max-time 3 http://169.254.169.254/latest/meta-data/public-hostname > /dev/null 2>&1; then
    PUBLIC_HOSTNAME=$(curl -s http://169.254.169.254/latest/meta-data/public-hostname)
    PUBLIC_IP=$(curl -s http://169.254.169.254/latest/meta-data/public-ipv4)
else
    PUBLIC_HOSTNAME="localhost"
    PUBLIC_IP="127.0.0.1"
fi

print_header() {
    echo -e "\n${BLUE}=== $1 ===${NC}"
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

# Test functions
test_services() {
    print_header "Service Status"
    
    if systemctl is-active --quiet brainstormx; then
        print_success "BrainStormX service is running"
    else
        print_error "BrainStormX service is not running"
        systemctl status brainstormx --no-pager -l
    fi
    
    if systemctl is-active --quiet nginx; then
        print_success "Nginx service is running"
    else
        print_error "Nginx service is not running"
        systemctl status nginx --no-pager -l
    fi
}

test_network() {
    print_header "Network Connectivity"
    
    # Test local application
    if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5001 | grep -q "200\|302"; then
        print_success "Application responding on localhost:5001"
    else
        print_error "Application not responding on localhost:5001"
    fi
    
    # Test HTTP redirect
    if curl -s -I "http://${PUBLIC_HOSTNAME}" | grep -q "301\|302"; then
        print_success "HTTP to HTTPS redirect working"
    else
        print_warning "HTTP redirect may not be working"
    fi
    
    # Test HTTPS
    if curl -k -s -o /dev/null -w "%{http_code}" "https://${PUBLIC_HOSTNAME}" | grep -q "200"; then
        print_success "HTTPS response working"
    else
        print_warning "HTTPS may not be responding correctly"
    fi
}

test_static_files() {
    print_header "Static File Access"
    
    # Test common static files
    if curl -k -s -I "https://${PUBLIC_HOSTNAME}/static/css/main.css" | grep -q "200\|404"; then
        print_success "Static file endpoint accessible"
    else
        print_warning "Static files may not be accessible"
    fi
}

test_permissions() {
    print_header "File Permissions"
    
    # Check critical directory permissions
    if [[ $(stat -c "%a" "/home/${APP_USER}") == "755" ]]; then
        print_success "Home directory permissions correct"
    else
        print_warning "Home directory permissions may be incorrect"
    fi
    
    if [[ $(stat -c "%a" "${APP_DIR}") == "755" ]]; then
        print_success "Application directory permissions correct"
    else
        print_warning "Application directory permissions may be incorrect"
    fi
    
    if [[ -r "${APP_DIR}/.env" ]]; then
        print_success "Environment file is readable"
    else
        print_error "Environment file is not readable"
    fi
}

test_ssl_certificate() {
    print_header "SSL Certificate"
    
    if openssl s_client -connect "${PUBLIC_HOSTNAME}:443" -servername "${PUBLIC_HOSTNAME}" </dev/null 2>/dev/null | grep -q "CONNECTED"; then
        print_success "SSL certificate is working"
        
        # Check certificate expiry
        CERT_EXPIRY=$(echo | openssl s_client -connect "${PUBLIC_HOSTNAME}:443" -servername "${PUBLIC_HOSTNAME}" 2>/dev/null | openssl x509 -noout -dates | grep notAfter | cut -d= -f2)
        print_success "Certificate expires: ${CERT_EXPIRY}"
    else
        print_error "SSL certificate test failed"
    fi
}

test_application_features() {
    print_header "Application Features"
    
    # Test Python environment
    if sudo -u "${APP_USER}" bash -c "cd ${APP_DIR} && source venv/bin/activate && python3 -c 'from app import create_app; create_app()'" 2>/dev/null; then
        print_success "Python application initializes correctly"
    else
        print_error "Python application initialization failed"
    fi
    
    # Test Piper TTS
    if sudo -u "${APP_USER}" bash -c "cd ${APP_DIR} && source venv/bin/activate && ./venv/bin/piper --version" >/dev/null 2>&1; then
        print_success "Piper TTS is working"
    else
        print_warning "Piper TTS may not be working"
    fi
    
    # Test Vosk model
    if sudo -u "${APP_USER}" test -f "${APP_DIR}/stt_models/vosk-model-en-us-0.22-lgraph/conf/model.conf"; then
        print_success "Vosk speech model is present"
    else
        print_warning "Vosk speech model not found"
    fi
    
    # Test TTS models
    if sudo -u "${APP_USER}" test -f "${APP_DIR}/tts_models/en_US-hfc_male-medium.onnx"; then
        print_success "TTS models are present"
    else
        print_warning "TTS models not found"
    fi
}

test_security() {
    print_header "Security Configuration"
    
    # Test firewall
    if ufw status | grep -q "Status: active"; then
        print_success "UFW firewall is active"
        echo "Firewall rules:"
        ufw status numbered | grep -E "(22|80|443)"
    else
        print_warning "UFW firewall is not active"
    fi
    
    # Test SSL configuration
    if curl -k -s -I "https://${PUBLIC_HOSTNAME}" | grep -q "Strict-Transport-Security"; then
        print_success "HSTS header is present"
    else
        print_warning "HSTS header not found"
    fi
    
    if curl -k -s -I "https://${PUBLIC_HOSTNAME}" | grep -q "X-Content-Type-Options"; then
        print_success "Security headers are present"
    else
        print_warning "Security headers may be missing"
    fi
}

test_backup_system() {
    print_header "Backup System"
    
    if sudo -u "${APP_USER}" test -x "/home/${APP_USER}/backup.sh"; then
        print_success "Backup script is present and executable"
    else
        print_warning "Backup script not found or not executable"
    fi
    
    if sudo -u "${APP_USER}" crontab -l | grep -q backup; then
        print_success "Backup cron job is configured"
    else
        print_warning "Backup cron job not found"
    fi
}

test_logs() {
    print_header "Logging System"
    
    if [[ -d "${APP_DIR}/instance/logs" ]]; then
        print_success "Application log directory exists"
    else
        print_warning "Application log directory not found"
    fi
    
    if [[ -f "/etc/logrotate.d/brainstormx" ]]; then
        print_success "Log rotation is configured"
    else
        print_warning "Log rotation not configured"
    fi
}

generate_report() {
    print_header "Validation Summary"
    
    echo -e "\n${BLUE}Deployment Validation Complete${NC}"
    echo -e "Tested at: $(date)"
    echo -e "Instance: ${PUBLIC_HOSTNAME} (${PUBLIC_IP})"
    echo ""
    echo -e "${GREEN}Access URLs:${NC}"
    echo -e "  HTTPS: https://${PUBLIC_HOSTNAME}"
    echo -e "  HTTP:  http://${PUBLIC_HOSTNAME}"
    echo ""
    echo -e "${YELLOW}Next Steps:${NC}"
    echo -e "  1. Configure AWS credentials in ${APP_DIR}/.env"
    echo -e "  2. Configure email settings for user registration"
    echo -e "  3. Test application functionality in browser"
    echo -e "  4. Set up monitoring and alerting"
    echo ""
    echo -e "${BLUE}For support: patrick@broadcomms.net${NC}"
}

# Main validation
main() {
    echo -e "${GREEN}"
    echo "=============================================================================="
    echo "                    BrainStormX Deployment Validation"
    echo "=============================================================================="
    echo -e "${NC}"
    
    test_services
    test_network
    test_static_files
    test_permissions
    test_ssl_certificate
    test_application_features
    test_security
    test_backup_system
    test_logs
    generate_report
}

# Check if running as root/sudo
if [[ $EUID -ne 0 ]]; then
    echo "This script requires sudo access for system checks."
    echo "Please run: sudo $0"
    exit 1
fi

main "$@"