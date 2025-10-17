#!/bin/bash

# =============================================================================
# BrainStormX SSL Certificate Cleanup and Fix Script
# Version: 1.0.0
# Description: Fixes SSL certificate issues and clears old self-signed certificates
# =============================================================================

set -e  # Exit on any error

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
SCRIPT_VERSION="1.0.0"
DOMAIN_NAME="brainstormx.ca"
WWW_DOMAIN="www.brainstormx.ca"

# Logging function
log() {
    echo -e "$1" | tee -a "/tmp/ssl_fix.log"
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

# Check prerequisites
check_prerequisites() {
    print_header "CHECKING PREREQUISITES"
    
    # Check if running as root or with sudo
    if [[ $EUID -eq 0 ]]; then
        print_success "Running as root user"
    elif sudo -n true 2>/dev/null; then
        print_success "Sudo access confirmed"
    else
        print_error "This script requires sudo access. Please run with sudo or as root."
        exit 1
    fi
    
    # Check if domain is accessible
    if curl -s --max-time 5 "https://$DOMAIN_NAME" > /dev/null; then
        print_success "Domain is accessible"
    else
        print_warning "Domain may have connectivity issues"
    fi
}

# Remove old self-signed certificates
remove_old_certificates() {
    print_header "REMOVING OLD SELF-SIGNED CERTIFICATES"
    
    # Remove old self-signed certificate files
    if [[ -f "/etc/ssl/certs/brainstormx-selfsigned.crt" ]]; then
        print_step "Removing old self-signed certificate..."
        rm -f "/etc/ssl/certs/brainstormx-selfsigned.crt"
        print_success "Old certificate removed"
    else
        print_success "No old certificate found"
    fi
    
    if [[ -f "/etc/ssl/private/brainstormx-selfsigned.key" ]]; then
        print_step "Removing old self-signed private key..."
        rm -f "/etc/ssl/private/brainstormx-selfsigned.key"
        print_success "Old private key removed"
    else
        print_success "No old private key found"
    fi
}

# Verify Let's Encrypt certificates
verify_certificates() {
    print_header "VERIFYING LET'S ENCRYPT CERTIFICATES"
    
    print_step "Checking certificate files..."
    if [[ -f "/etc/letsencrypt/live/$DOMAIN_NAME/fullchain.pem" ]]; then
        print_success "Let's Encrypt certificate found"
    else
        print_error "Let's Encrypt certificate not found"
        exit 1
    fi
    
    if [[ -f "/etc/letsencrypt/live/$DOMAIN_NAME/privkey.pem" ]]; then
        print_success "Let's Encrypt private key found"
    else
        print_error "Let's Encrypt private key not found"
        exit 1
    fi
    
    # Check certificate validity
    print_step "Checking certificate validity..."
    if openssl x509 -in "/etc/letsencrypt/live/$DOMAIN_NAME/fullchain.pem" -noout -checkend 86400 > /dev/null; then
        print_success "Certificate is valid"
    else
        print_error "Certificate is expired or invalid"
        exit 1
    fi
    
    # Check certificate issuer
    local issuer
    issuer=$(openssl x509 -in "/etc/letsencrypt/live/$DOMAIN_NAME/fullchain.pem" -noout -issuer | grep -o "Let's Encrypt" || echo "")
    if [[ "$issuer" == "Let's Encrypt" ]]; then
        print_success "Certificate issued by Let's Encrypt"
    else
        print_warning "Certificate not issued by Let's Encrypt"
    fi
}

# Fix Nginx configuration
fix_nginx_configuration() {
    print_header "FIXING NGINX CONFIGURATION"
    
    print_step "Creating clean Nginx configuration..."
    
    # Create a clean Nginx configuration
    cat > /etc/nginx/sites-available/brainstormx << EOF
# HTTP redirect to HTTPS
server {
    listen 80;
    server_name $DOMAIN_NAME $WWW_DOMAIN;
    
    # Allow certbot to access .well-known directory
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }
    
    # Redirect all other HTTP requests to HTTPS
    location / {
        return 301 https://\$server_name\$request_uri;
    }
}

# HTTPS server configuration
server {
    listen 443 ssl http2;
    server_name $DOMAIN_NAME $WWW_DOMAIN;
    
    # SSL Configuration
    ssl_certificate /etc/letsencrypt/live/$DOMAIN_NAME/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DOMAIN_NAME/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;
    
    # Security headers
    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Content-Type-Options nosniff;
    add_header X-Frame-Options DENY;
    add_header X-XSS-Protection "1; mode=block";
    add_header Referrer-Policy "strict-origin-when-cross-origin";
    
    # Application proxy
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
        alias /home/brainstormx/BrainStormX/app/static/;
        expires 1d;
        add_header Cache-Control "public, immutable";
        access_log off;
    }
    
    # Media files
    location /media/ {
        alias /home/brainstormx/BrainStormX/instance/uploads/;
        expires 1d;
        access_log off;
    }
    
    # Enable gzip compression
    gzip on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_types text/plain text/css application/json application/javascript text/xml application/xml application/xml+rss text/javascript;
}
EOF
    
    print_success "Nginx configuration updated"
}

# Test and reload Nginx
reload_nginx() {
    print_header "RELOADING NGINX"
    
    print_step "Testing Nginx configuration..."
    if nginx -t; then
        print_success "Nginx configuration is valid"
    else
        print_error "Nginx configuration test failed"
        exit 1
    fi
    
    print_step "Reloading Nginx..."
    systemctl reload nginx
    
    if systemctl is-active --quiet nginx; then
        print_success "Nginx reloaded successfully"
    else
        print_error "Failed to reload Nginx"
        exit 1
    fi
}

# Clear browser cache instructions
print_cache_instructions() {
    print_header "BROWSER CACHE CLEARING INSTRUCTIONS"
    
    echo -e "${CYAN}The SSL certificate should now be working correctly.${NC}"
    echo -e "${CYAN}If you still see certificate warnings, clear your browser cache:${NC}"
    echo ""
    
    echo -e "${WHITE}Chrome/Edge:${NC}"
    echo -e "  1. Press Ctrl+Shift+Delete (or Cmd+Shift+Delete on Mac)"
    echo -e "  2. Select 'All time' for time range"
    echo -e "  3. Check 'Cached images and files'"
    echo -e "  4. Click 'Clear data'"
    echo ""
    
    echo -e "${WHITE}Firefox:${NC}"
    echo -e "  1. Press Ctrl+Shift+Delete (or Cmd+Shift+Delete on Mac)"
    echo -e "  2. Select 'Everything' for time range"
    echo -e "  3. Check 'Cache'"
    echo -e "  4. Click 'Clear Now'"
    echo ""
    
    echo -e "${WHITE}Safari:${NC}"
    echo -e "  1. Press Cmd+Option+E"
    echo -e "  2. Or Safari menu → Develop → Empty Caches"
    echo ""
    
    echo -e "${WHITE}Alternative (all browsers):${NC}"
    echo -e "  1. Try opening in private/incognito mode"
    echo -e "  2. Or hard refresh with Ctrl+F5 (Cmd+Shift+R on Mac)"
    echo ""
}

# Verify SSL setup
verify_ssl_setup() {
    print_header "VERIFYING SSL SETUP"
    
    print_step "Testing SSL certificate for $DOMAIN_NAME..."
    local cert_info
    cert_info=$(echo | openssl s_client -connect "$DOMAIN_NAME:443" -servername "$DOMAIN_NAME" 2>/dev/null | openssl x509 -noout -subject -issuer 2>/dev/null)
    
    if echo "$cert_info" | grep -q "Let's Encrypt"; then
        print_success "SSL certificate correctly served by Let's Encrypt"
        echo -e "${GREEN}Certificate details:${NC}"
        echo "$cert_info" | while read -r line; do
            echo -e "  ${WHITE}$line${NC}"
        done
    else
        print_warning "SSL certificate may still have issues"
        echo "$cert_info"
    fi
    
    print_step "Testing HTTPS access..."
    local http_status
    http_status=$(curl -s -o /dev/null -w "%{http_code}" "https://$DOMAIN_NAME" --max-time 10)
    
    if [[ "$http_status" == "200" ]]; then
        print_success "HTTPS access working (Status: $http_status)"
    else
        print_warning "HTTPS access status: $http_status"
    fi
    
    print_step "Testing HTTP to HTTPS redirect..."
    local redirect_status
    redirect_status=$(curl -s -o /dev/null -w "%{http_code}" "http://$DOMAIN_NAME" --max-time 10)
    
    if [[ "$redirect_status" == "301" || "$redirect_status" == "302" ]]; then
        print_success "HTTP to HTTPS redirect working (Status: $redirect_status)"
    else
        print_warning "HTTP redirect status: $redirect_status"
    fi
}

# Force certificate refresh
force_certificate_refresh() {
    print_header "FORCING CERTIFICATE REFRESH"
    
    print_step "Stopping Nginx temporarily..."
    systemctl stop nginx
    
    print_step "Testing certificate renewal..."
    if certbot renew --dry-run --quiet; then
        print_success "Certificate renewal test passed"
    else
        print_warning "Certificate renewal test failed"
    fi
    
    print_step "Starting Nginx..."
    systemctl start nginx
    
    if systemctl is-active --quiet nginx; then
        print_success "Nginx started successfully"
    else
        print_error "Failed to start Nginx"
        exit 1
    fi
}

# Main function
main() {
    echo -e "${GREEN}"
    echo "=============================================================================="
    echo "               BrainStormX SSL Certificate Fix Script v${SCRIPT_VERSION}"
    echo "=============================================================================="
    echo -e "${NC}"
    echo "This script will fix SSL certificate issues and ensure proper Let's Encrypt"
    echo "certificate configuration."
    echo ""
    
    check_prerequisites
    remove_old_certificates
    verify_certificates
    fix_nginx_configuration
    reload_nginx
    force_certificate_refresh
    verify_ssl_setup
    print_cache_instructions
    
    # Final success message
    print_header "SSL CERTIFICATE FIX COMPLETED!"
    
    echo -e "${GREEN}🎉 SSL certificate configuration has been fixed!${NC}\n"
    
    echo -e "${CYAN}Access your application at:${NC}"
    echo -e "  🌐 Primary: ${WHITE}https://$DOMAIN_NAME${NC}"
    echo -e "  🌐 WWW: ${WHITE}https://$WWW_DOMAIN${NC}"
    echo ""
    
    echo -e "${YELLOW}📋 Important Notes:${NC}"
    echo -e "  • Clear your browser cache if you still see warnings"
    echo -e "  • Try accessing in private/incognito mode"
    echo -e "  • Hard refresh with Ctrl+F5 (Cmd+Shift+R on Mac)"
    echo ""
    
    echo -e "${BLUE}🔧 Verification Commands:${NC}"
    echo -e "  • Test certificate: ${WHITE}echo | openssl s_client -connect $DOMAIN_NAME:443${NC}"
    echo -e "  • Check status: ${WHITE}sudo systemctl status nginx${NC}"
    echo -e "  • View logs: ${WHITE}sudo tail -f /var/log/nginx/error.log${NC}"
    echo ""
    
    echo -e "${GREEN}SSL certificate fix completed at: $(date)${NC}"
}

# Run main function
main "$@"