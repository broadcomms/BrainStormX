#!/bin/bash

# =============================================================================
# BrainStormX Domain & SSL Configuration Script
# Version: 1.0.0
# Description: Configure custom domain and SSL certificate for deployed BrainStormX
# 
# CHANGELOG:
# v1.0.0 - Initial release with domain and SSL configuration
# =============================================================================
# This script configures a custom domain and SSL certificate for an existing
# BrainStormX deployment. Run this AFTER ec2_auto_deploy.sh has completed.
# Prerequisites: 
# - BrainStormX already deployed with ec2_auto_deploy.sh
# - Domain DNS A record pointing to this server's IP
# - Valid email address for Let's Encrypt notifications
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
SCRIPT_VERSION="1.0.0"
APP_USER="brainstormx"
APP_DIR="/home/${APP_USER}/BrainStormX"
LOG_FILE="/tmp/brainstormx_domain_setup.log"

# Global variables for user configuration
DOMAIN_NAME=""
WWW_DOMAIN=""
EMAIL_ADDRESS=""
CURRENT_DOMAIN=""

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
        echo -e "  Current DNS: ${CYAN}${PUBLIC_HOSTNAME}${NC}"
        echo -e "  AZ: ${CYAN}${AVAILABILITY_ZONE}${NC}"
        CURRENT_DOMAIN="${PUBLIC_HOSTNAME}"
        return 0
    else
        echo -e "${YELLOW}⚠ Warning: Not running on EC2 or metadata service unavailable${NC}"
        PUBLIC_HOSTNAME="localhost"
        PUBLIC_IP="127.0.0.1"
        INSTANCE_ID="unknown"
        AVAILABILITY_ZONE="unknown"
        CURRENT_DOMAIN="localhost"
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
    print_error "Error occurred in domain setup script at line $1"
    print_error "Check log file: ${LOG_FILE}"
    print_error "Last few log entries:"
    tail -10 "${LOG_FILE}"
    exit 1
}

trap 'error_exit $LINENO' ERR

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
    
    # Check if BrainStormX is deployed
    if [[ ! -d "$APP_DIR" ]]; then
        print_error "BrainStormX application directory not found at $APP_DIR"
        print_error "Please run ec2_auto_deploy.sh first to deploy BrainStormX"
        exit 1
    fi
    
    # Check if brainstormx user exists
    if ! id "$APP_USER" &>/dev/null; then
        print_error "User '$APP_USER' not found"
        print_error "Please run ec2_auto_deploy.sh first to deploy BrainStormX"
        exit 1
    fi
    
    # Check if services are running
    if ! systemctl is-active --quiet brainstormx; then
        print_warning "BrainStormX service is not running"
        print_step "Attempting to start BrainStormX service..."
        systemctl start brainstormx
        sleep 3
        if systemctl is-active --quiet brainstormx; then
            print_success "BrainStormX service started successfully"
        else
            print_error "Failed to start BrainStormX service"
            exit 1
        fi
    else
        print_success "BrainStormX service is running"
    fi
    
    # Check if nginx is installed and running
    if ! command -v nginx &> /dev/null; then
        print_error "Nginx is not installed"
        print_error "Please run ec2_auto_deploy.sh first to deploy BrainStormX"
        exit 1
    fi
    
    if ! systemctl is-active --quiet nginx; then
        print_warning "Nginx service is not running"
        print_step "Attempting to start Nginx service..."
        systemctl start nginx
        sleep 2
        if systemctl is-active --quiet nginx; then
            print_success "Nginx service started successfully"
        else
            print_error "Failed to start Nginx service"
            exit 1
        fi
    else
        print_success "Nginx service is running"
    fi
    
    # Check if certbot is installed
    if ! command -v certbot &> /dev/null; then
        print_step "Installing certbot..."
        apt update >> "${LOG_FILE}" 2>&1
        DEBIAN_FRONTEND=noninteractive apt install -y certbot python3-certbot-nginx >> "${LOG_FILE}" 2>&1
        print_success "Certbot installed successfully"
    else
        print_success "Certbot is already installed"
    fi
    
    print_success "All prerequisites checked"
}

# Validate domain name format
validate_domain() {
    local domain="$1"
    
    # Basic domain validation regex
    if [[ $domain =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$ ]]; then
        return 0
    else
        return 1
    fi
}

# Validate email format
validate_email() {
    local email="$1"
    
    # Basic email validation regex
    if [[ $email =~ ^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$ ]]; then
        return 0
    else
        return 1
    fi
}

# Check DNS resolution
check_dns_resolution() {
    local domain="$1"
    local expected_ip="$2"
    
    print_step "Checking DNS resolution for $domain..."
    
    # Get IP address from DNS
    local resolved_ip
    resolved_ip=$(dig +short "$domain" 2>/dev/null | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | head -1)
    
    if [[ -z "$resolved_ip" ]]; then
        print_warning "DNS lookup failed for $domain"
        return 1
    fi
    
    if [[ "$resolved_ip" == "$expected_ip" ]]; then
        print_success "DNS resolution correct: $domain → $resolved_ip"
        return 0
    else
        print_warning "DNS mismatch: $domain → $resolved_ip (expected: $expected_ip)"
        return 1
    fi
}

# Collect domain configuration from user
collect_domain_configuration() {
    print_header "DOMAIN CONFIGURATION SETUP"
    
    echo -e "${CYAN}This script will configure a custom domain and SSL certificate for your BrainStormX installation.${NC}"
    echo -e "${CYAN}Currently accessible at: ${WHITE}https://$CURRENT_DOMAIN${NC}"
    echo ""
    
    # Domain name input
    while true; do
        echo -e "${WHITE}Enter your domain name (e.g., brainstormx.ca):${NC}"
        read -r DOMAIN_INPUT
        
        if [[ -z "$DOMAIN_INPUT" ]]; then
            print_error "Domain name cannot be empty. Please try again."
            continue
        fi
        
        # Remove protocol and trailing slash if present
        DOMAIN_INPUT=$(echo "$DOMAIN_INPUT" | sed -e 's|^https\?://||' -e 's|/$||')
        
        if validate_domain "$DOMAIN_INPUT"; then
            DOMAIN_NAME="$DOMAIN_INPUT"
            break
        else
            print_error "Invalid domain format. Please enter a valid domain (e.g., brainstormx.ca)"
        fi
    done
    
    # WWW subdomain option
    echo ""
    echo -e "${WHITE}Do you want to include www subdomain? (Y/n):${NC}"
    read -n 1 -r WWW_RESPONSE
    echo ""
    
    if [[ "${WWW_RESPONSE:-}" =~ ^[Nn]$ ]]; then
        WWW_DOMAIN=""
        print_success "Will configure SSL for: $DOMAIN_NAME only"
    else
        WWW_DOMAIN="www.$DOMAIN_NAME"
        print_success "Will configure SSL for: $DOMAIN_NAME and $WWW_DOMAIN"
    fi
    
    # Email address for Let's Encrypt
    while true; do
        echo ""
        echo -e "${WHITE}Enter email address for Let's Encrypt notifications:${NC}"
        read -r EMAIL_INPUT
        
        if [[ -z "$EMAIL_INPUT" ]]; then
            print_error "Email address cannot be empty. Please try again."
            continue
        fi
        
        if validate_email "$EMAIL_INPUT"; then
            EMAIL_ADDRESS="$EMAIL_INPUT"
            break
        else
            print_error "Invalid email format. Please enter a valid email address."
        fi
    done
    
    print_success "Domain configuration collected"
    
    # Display configuration summary
    echo ""
    echo -e "${CYAN}Configuration Summary:${NC}"
    echo -e "  Primary Domain: ${WHITE}$DOMAIN_NAME${NC}"
    if [[ -n "$WWW_DOMAIN" ]]; then
        echo -e "  WWW Domain: ${WHITE}$WWW_DOMAIN${NC}"
    fi
    echo -e "  Email: ${WHITE}$EMAIL_ADDRESS${NC}"
    echo -e "  Current IP: ${WHITE}$PUBLIC_IP${NC}"
    echo ""
}

# Verify DNS configuration
verify_dns_configuration() {
    print_header "VERIFYING DNS CONFIGURATION"
    
    local dns_check_passed=true
    
    # Check primary domain
    if ! check_dns_resolution "$DOMAIN_NAME" "$PUBLIC_IP"; then
        dns_check_passed=false
    fi
    
    # Check www domain if configured
    if [[ -n "$WWW_DOMAIN" ]]; then
        if ! check_dns_resolution "$WWW_DOMAIN" "$PUBLIC_IP"; then
            dns_check_passed=false
        fi
    fi
    
    if [[ "$dns_check_passed" == false ]]; then
        echo ""
        print_warning "DNS configuration issues detected!"
        echo -e "${YELLOW}Please ensure the following DNS records are configured:${NC}"
        echo -e "  ${WHITE}$DOMAIN_NAME${NC} → A record → ${WHITE}$PUBLIC_IP${NC}"
        if [[ -n "$WWW_DOMAIN" ]]; then
            echo -e "  ${WHITE}$WWW_DOMAIN${NC} → A record → ${WHITE}$PUBLIC_IP${NC}"
        fi
        echo ""
        echo -e "${YELLOW}DNS propagation can take up to 48 hours, but usually takes 5-30 minutes.${NC}"
        echo ""
        
        while true; do
            echo -e "${WHITE}Do you want to continue anyway? (y/N):${NC}"
            read -n 1 -r CONTINUE_ANYWAY
            echo ""
            
            if [[ "${CONTINUE_ANYWAY:-}" =~ ^[Yy]$ ]]; then
                print_warning "Continuing with SSL setup despite DNS issues..."
                print_warning "SSL certificate generation may fail if DNS is not properly configured"
                break
            else
                print_error "Please configure DNS records and run this script again"
                exit 1
            fi
        done
    else
        print_success "DNS configuration verified successfully"
    fi
}

# Update Nginx configuration for new domain
update_nginx_configuration() {
    print_header "UPDATING NGINX CONFIGURATION"
    
    print_step "Creating new Nginx configuration for $DOMAIN_NAME..."
    
    # Build server_name directive
    local server_names="$DOMAIN_NAME"
    if [[ -n "$WWW_DOMAIN" ]]; then
        server_names="$DOMAIN_NAME $WWW_DOMAIN"
    fi
    
    # Create temporary HTTP-only configuration for certbot validation
    cat > /etc/nginx/sites-available/brainstormx << EOF
server {
    listen 80;
    server_name $server_names;
    
    # Allow certbot to access .well-known directory
    location /.well-known/acme-challenge/ {
        root /var/www/html;
    }
    
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
    
    # Test nginx configuration
    print_step "Testing Nginx configuration..."
    if nginx -t >> "${LOG_FILE}" 2>&1; then
        print_success "Nginx configuration is valid"
    else
        print_error "Nginx configuration test failed"
        cat "${LOG_FILE}" | tail -10
        exit 1
    fi
    
    # Reload nginx
    print_step "Reloading Nginx..."
    systemctl reload nginx
    print_success "Nginx reloaded successfully"
}

# Obtain SSL certificate using certbot
obtain_ssl_certificate() {
    print_header "OBTAINING SSL CERTIFICATE"
    
    print_step "Creating directory for certbot challenges..."
    mkdir -p /var/www/html/.well-known/acme-challenge
    chown -R www-data:www-data /var/www/html
    
    # Build domain arguments for certbot
    local domain_args="-d $DOMAIN_NAME"
    if [[ -n "$WWW_DOMAIN" ]]; then
        domain_args="$domain_args -d $WWW_DOMAIN"
    fi
    
    print_step "Requesting SSL certificate from Let's Encrypt..."
    print_warning "This may take a few minutes..."
    
    # Run certbot with nginx plugin
    if certbot --nginx $domain_args \
        --email "$EMAIL_ADDRESS" \
        --agree-tos \
        --no-eff-email \
        --redirect \
        --non-interactive >> "${LOG_FILE}" 2>&1; then
        
        print_success "SSL certificate obtained successfully!"
    else
        print_error "Failed to obtain SSL certificate"
        print_error "Common issues:"
        print_error "1. DNS records not properly configured"
        print_error "2. Domain not pointing to this server"
        print_error "3. Firewall blocking port 80/443"
        print_error "4. Rate limiting from Let's Encrypt"
        echo ""
        print_error "Check the log for details:"
        tail -20 "${LOG_FILE}"
        exit 1
    fi
}

# Configure SSL auto-renewal
configure_ssl_renewal() {
    print_header "CONFIGURING SSL AUTO-RENEWAL"
    
    print_step "Setting up automatic certificate renewal..."
    
    # Enable and start certbot timer
    systemctl enable certbot.timer >> "${LOG_FILE}" 2>&1
    systemctl start certbot.timer >> "${LOG_FILE}" 2>&1
    
    print_step "Testing certificate renewal..."
    if certbot renew --dry-run >> "${LOG_FILE}" 2>&1; then
        print_success "Certificate renewal test passed"
    else
        print_warning "Certificate renewal test failed, but certificate is still valid"
    fi
    
    print_success "SSL auto-renewal configured"
    print_success "Certificates will be automatically renewed before expiry"
}

# Update application configuration if needed
update_application_config() {
    print_header "UPDATING APPLICATION CONFIGURATION"
    
    # Check if application needs domain-specific configuration
    if [[ -f "${APP_DIR}/.env" ]]; then
        print_step "Checking application environment configuration..."
        
        # Add domain-specific configurations if needed
        if ! grep -q "DOMAIN_NAME" "${APP_DIR}/.env" 2>/dev/null; then
            print_step "Adding domain configuration to application..."
            echo "" >> "${APP_DIR}/.env"
            echo "# Domain Configuration" >> "${APP_DIR}/.env"
            echo "DOMAIN_NAME=$DOMAIN_NAME" >> "${APP_DIR}/.env"
            echo "PUBLIC_URL=https://$DOMAIN_NAME" >> "${APP_DIR}/.env"
            
            # Set proper ownership
            chown "$APP_USER:$APP_USER" "${APP_DIR}/.env"
            chmod 600 "${APP_DIR}/.env"
            
            print_success "Domain configuration added to application"
        else
            print_success "Application domain configuration already exists"
        fi
    else
        print_warning "Application environment file not found"
    fi
}

# Restart services
restart_services() {
    print_header "RESTARTING SERVICES"
    
    print_step "Restarting BrainStormX application..."
    systemctl restart brainstormx
    sleep 3
    
    if systemctl is-active --quiet brainstormx; then
        print_success "BrainStormX service restarted successfully"
    else
        print_error "Failed to restart BrainStormX service"
        print_error "Check service logs: sudo journalctl -u brainstormx --no-pager"
        exit 1
    fi
    
    print_step "Reloading Nginx configuration..."
    nginx -t >> "${LOG_FILE}" 2>&1
    systemctl reload nginx
    
    if systemctl is-active --quiet nginx; then
        print_success "Nginx service reloaded successfully"
    else
        print_error "Failed to reload Nginx service"
        exit 1
    fi
}

# Verify domain and SSL setup
verify_domain_setup() {
    print_header "VERIFYING DOMAIN AND SSL SETUP"
    
    print_step "Waiting for services to fully start..."
    sleep 10
    
    # Test HTTP to HTTPS redirect
    print_step "Testing HTTP to HTTPS redirect..."
    if curl -s -I "http://$DOMAIN_NAME" | grep -q "301\|302"; then
        print_success "HTTP to HTTPS redirect working"
    else
        print_warning "HTTP redirect may not be working correctly"
    fi
    
    # Test HTTPS response
    print_step "Testing HTTPS response..."
    local https_status
    https_status=$(curl -s -o /dev/null -w "%{http_code}" "https://$DOMAIN_NAME" --max-time 10)
    
    if [[ "$https_status" == "200" ]]; then
        print_success "HTTPS response working (Status: $https_status)"
    else
        print_warning "HTTPS response status: $https_status"
    fi
    
    # Test WWW domain if configured
    if [[ -n "$WWW_DOMAIN" ]]; then
        print_step "Testing WWW domain..."
        local www_status
        www_status=$(curl -s -o /dev/null -w "%{http_code}" "https://$WWW_DOMAIN" --max-time 10)
        
        if [[ "$www_status" == "200" ]]; then
            print_success "WWW domain working (Status: $www_status)"
        else
            print_warning "WWW domain response status: $www_status"
        fi
    fi
    
    # Check SSL certificate
    print_step "Checking SSL certificate..."
    local cert_info
    cert_info=$(echo | openssl s_client -connect "$DOMAIN_NAME:443" -servername "$DOMAIN_NAME" 2>/dev/null | openssl x509 -noout -dates 2>/dev/null)
    
    if [[ -n "$cert_info" ]]; then
        print_success "SSL certificate is valid"
        echo -e "${GREEN}Certificate details:${NC}"
        echo "$cert_info" | while read -r line; do
            echo -e "  ${WHITE}$line${NC}"
        done
    else
        print_warning "Could not verify SSL certificate details"
    fi
}

# Generate domain setup report
generate_domain_report() {
    print_header "GENERATING DOMAIN SETUP REPORT"
    
    local REPORT_FILE="/home/${APP_USER}/domain_setup_report.txt"
    
    cat > "${REPORT_FILE}" << EOF
# BrainStormX Domain & SSL Setup Report
Generated: $(date)
Script Version: ${SCRIPT_VERSION}

## Domain Configuration
Primary Domain: ${DOMAIN_NAME}
$(if [[ -n "$WWW_DOMAIN" ]]; then echo "WWW Domain: ${WWW_DOMAIN}"; fi)
Email: ${EMAIL_ADDRESS}

## Server Information
Instance ID: ${INSTANCE_ID}
Public IP: ${PUBLIC_IP}
Original DNS: ${CURRENT_DOMAIN}
Availability Zone: ${AVAILABILITY_ZONE}

## Access URLs
Primary HTTPS: https://${DOMAIN_NAME}
$(if [[ -n "$WWW_DOMAIN" ]]; then echo "WWW HTTPS: https://${WWW_DOMAIN}"; fi)
HTTP (redirects): http://${DOMAIN_NAME}

## SSL Certificate Information
Certificate obtained from: Let's Encrypt
Auto-renewal: Enabled (certbot.timer)
Certificate locations:
- Certificate: /etc/letsencrypt/live/${DOMAIN_NAME}/fullchain.pem
- Private Key: /etc/letsencrypt/live/${DOMAIN_NAME}/privkey.pem

## Service Status
$(systemctl is-active brainstormx >/dev/null && echo "✓ BrainStormX Service: Active" || echo "✗ BrainStormX Service: Inactive")
$(systemctl is-active nginx >/dev/null && echo "✓ Nginx Service: Active" || echo "✗ Nginx Service: Inactive")
$(systemctl is-active certbot.timer >/dev/null && echo "✓ SSL Auto-Renewal: Active" || echo "✗ SSL Auto-Renewal: Inactive")

## Configuration Files Updated
- Nginx Config: /etc/nginx/sites-available/brainstormx
- Application Config: ${APP_DIR}/.env

## SSL Certificate Commands
Check certificate status: sudo certbot certificates
Renew certificates manually: sudo certbot renew
Test renewal: sudo certbot renew --dry-run

## Verification Commands
Test HTTPS: curl -I https://${DOMAIN_NAME}
Check SSL: openssl s_client -connect ${DOMAIN_NAME}:443 -servername ${DOMAIN_NAME}
Nginx status: sudo systemctl status nginx
Application status: sudo systemctl status brainstormx

## Troubleshooting
If domain doesn't work:
1. Check DNS: dig ${DOMAIN_NAME}
2. Check Nginx: sudo nginx -t
3. Check logs: sudo tail -f /var/log/nginx/error.log
4. Restart services: sudo systemctl restart brainstormx nginx

SSL Issues:
1. Check certificate: sudo certbot certificates
2. Test renewal: sudo certbot renew --dry-run
3. Check logs: sudo tail -f /var/log/letsencrypt/letsencrypt.log

For support, contact: patrick@broadcomms.net
EOF
    
    chown "${APP_USER}:${APP_USER}" "${REPORT_FILE}"
    
    print_success "Domain setup report generated: ${REPORT_FILE}"
}

# Main function
main() {
    echo -e "${GREEN}"
    echo "=============================================================================="
    echo "             BrainStormX Domain & SSL Configuration Script v${SCRIPT_VERSION}"
    echo "=============================================================================="
    echo -e "${NC}"
    echo "This script will configure a custom domain and SSL certificate for your"
    echo "existing BrainStormX deployment."
    echo ""
    
    # Start logging
    echo "Starting domain configuration at $(date)" > "${LOG_FILE}"
    
    check_prerequisites
    collect_domain_configuration
    verify_dns_configuration
    update_nginx_configuration
    obtain_ssl_certificate
    configure_ssl_renewal
    update_application_config
    restart_services
    verify_domain_setup
    generate_domain_report
    
    # Final success message
    print_header "DOMAIN CONFIGURATION COMPLETED SUCCESSFULLY!"
    
    echo -e "${GREEN}🎉 Your BrainStormX deployment now has a custom domain with SSL!${NC}\n"
    
    echo -e "${CYAN}Access your application at:${NC}"
    echo -e "  🌐 Primary: ${WHITE}https://$DOMAIN_NAME${NC}"
    if [[ -n "$WWW_DOMAIN" ]]; then
        echo -e "  🌐 WWW: ${WHITE}https://$WWW_DOMAIN${NC}"
    fi
    echo -e "  🔒 SSL Certificate: ${WHITE}Valid and Auto-Renewing${NC}"
    echo ""
    
    echo -e "${YELLOW}📋 Important Notes:${NC}"
    echo -e "  • SSL certificate will auto-renew before expiry"
    echo -e "  • Domain report saved to: ${WHITE}/home/${APP_USER}/domain_setup_report.txt${NC}"
    echo -e "  • Original EC2 DNS (${CURRENT_DOMAIN}) is still accessible"
    echo ""
    
    echo -e "${BLUE}🔧 Management Commands:${NC}"
    echo -e "  • Check SSL status: ${WHITE}sudo certbot certificates${NC}"
    echo -e "  • Test SSL renewal: ${WHITE}sudo certbot renew --dry-run${NC}"
    echo -e "  • View services: ${WHITE}sudo systemctl status brainstormx nginx${NC}"
    echo ""
    
    echo -e "${GREEN}For support, contact: patrick@broadcomms.net${NC}"
    echo -e "${GREEN}Domain configuration completed at: $(date)${NC}"
}

# Parse command line arguments
case "${1:-}" in
    --help|help|-h)
        echo -e "${GREEN}BrainStormX Domain & SSL Configuration Script v${SCRIPT_VERSION}${NC}"
        echo ""
        echo "Usage:"
        echo "  $0                 - Configure domain and SSL certificate"
        echo "  $0 --help          - Show this help message"
        echo ""
        echo "Prerequisites:"
        echo "  • BrainStormX deployed with ec2_auto_deploy.sh"
        echo "  • Domain DNS A record pointing to this server"
        echo "  • Valid email address for Let's Encrypt"
        echo ""
        echo "Example:"
        echo "  sudo $0            # Configure brainstormx.ca with SSL"
        echo ""
        echo "For support, contact: patrick@broadcomms.net"
        exit 0
        ;;
    "")
        # No arguments, run main configuration
        main "$@"
        ;;
    *)
        echo -e "${RED}Error: Unknown argument '$1'${NC}"
        echo "Use '$0 --help' for usage information."
        exit 1
        ;;
esac