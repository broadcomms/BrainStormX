#!/bin/bash

# =============================================================================
# BrainStormX Complete Deployment Example
# Version: 1.0.0
# Description: Example workflow for complete BrainStormX deployment with custom domain
# 
# This script demonstrates the typical deployment workflow but requires manual
# execution of each step. It's provided as a reference guide.
# =============================================================================

set -e

# Color codes for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
WHITE='\033[1;37m'
NC='\033[0m' # No Color

echo -e "${GREEN}=============================================================================="
echo "             BrainStormX Complete Deployment Workflow"
echo "=============================================================================="
echo -e "${NC}"

echo -e "${CYAN}This script demonstrates the complete workflow for deploying BrainStormX"
echo -e "with a custom domain and SSL certificate.${NC}"
echo ""
echo -e "${YELLOW}IMPORTANT: This is a reference guide. Each step should be executed manually.${NC}"
echo ""

echo -e "${BLUE}Step 1: Basic EC2 Deployment${NC}"
echo "=========================================="
echo -e "${WHITE}# Download and run the main deployment script${NC}"
echo 'wget "https://raw.githubusercontent.com/broadcomms/BrainStormX/main/scripts/ec2_auto_deploy.sh?$(date +%s)"'
echo "chmod +x ec2_auto_deploy.sh"
echo "sudo ./ec2_auto_deploy.sh"
echo ""
echo -e "${GREEN}This will:${NC}"
echo "  ✓ Install all system dependencies"
echo "  ✓ Set up Python environment and BrainStormX"
echo "  ✓ Configure Nginx with self-signed SSL"
echo "  ✓ Set up systemd services"
echo "  ✓ Install AI/speech components (Piper TTS, Vosk STT)"
echo "  ✓ Configure firewall and backups"
echo ""

echo -e "${BLUE}Step 2: Configure Basic Settings${NC}"
echo "========================================"
echo -e "${WHITE}# Configure AWS credentials and email settings${NC}"
echo "sudo nano /home/brainstormx/BrainStormX/.env"
echo ""
echo -e "${WHITE}# Restart services after configuration${NC}"
echo "sudo systemctl restart brainstormx nginx"
echo ""
echo -e "${GREEN}After this step:${NC}"
echo "  ✓ Application accessible via EC2 DNS with self-signed SSL"
echo "  ✓ AI features will work with proper AWS credentials"
echo "  ✓ Email features will work with proper SMTP settings"
echo ""

echo -e "${BLUE}Step 3: Domain DNS Configuration${NC}"
echo "========================================"
echo -e "${WHITE}# Configure DNS A records for your domain${NC}"
echo "# Example DNS configuration:"
echo "# Type: A"
echo "# Name: brainstormx.ca"
echo "# Value: [YOUR_EC2_PUBLIC_IP]"
echo "# TTL: 300"
echo ""
echo -e "${WHITE}# Optional: WWW subdomain${NC}"
echo "# Type: A"
echo "# Name: www.brainstormx.ca" 
echo "# Value: [YOUR_EC2_PUBLIC_IP]"
echo "# TTL: 300"
echo ""
echo -e "${GREEN}DNS propagation typically takes:${NC}"
echo "  ✓ 5-30 minutes (usually)"
echo "  ✓ Up to 48 hours (maximum)"
echo ""

echo -e "${BLUE}Step 4: Verify DNS Propagation${NC}"
echo "======================================"
echo -e "${WHITE}# Check DNS resolution${NC}"
echo "dig brainstormx.ca"
echo "nslookup brainstormx.ca"
echo ""
echo -e "${GREEN}Expected result:${NC}"
echo "  ✓ Domain should resolve to your EC2 public IP"
echo "  ✓ Both primary and www domains (if configured)"
echo ""

echo -e "${BLUE}Step 5: Custom Domain & SSL Setup${NC}"
echo "========================================"
echo -e "${WHITE}# Download and run domain setup script${NC}"
echo 'wget "https://raw.githubusercontent.com/broadcomms/BrainStormX/main/scripts/domain_ssl_setup.sh?$(date +%s)"'
echo "chmod +x domain_ssl_setup.sh"
echo "sudo ./domain_ssl_setup.sh"
echo ""
echo -e "${GREEN}This will:${NC}"
echo "  ✓ Validate DNS configuration"
echo "  ✓ Update Nginx for custom domain"
echo "  ✓ Obtain Let's Encrypt SSL certificates"
echo "  ✓ Configure automatic certificate renewal"
echo "  ✓ Set up HTTPS redirects"
echo "  ✓ Verify complete setup"
echo ""

echo -e "${BLUE}Step 6: Final Verification${NC}"
echo "================================="
echo -e "${WHITE}# Test all access methods${NC}"
echo "curl -I https://brainstormx.ca"
echo "curl -I https://www.brainstormx.ca  # if configured"
echo ""
echo -e "${WHITE}# Check SSL certificate${NC}"
echo 'echo | openssl s_client -connect brainstormx.ca:443 -servername brainstormx.ca'
echo ""
echo -e "${WHITE}# Check service status${NC}"
echo "sudo systemctl status brainstormx nginx certbot.timer"
echo ""

echo -e "${GREEN}=============================================================================="
echo "                        Deployment Complete!"
echo "=============================================================================="
echo -e "${NC}"

echo -e "${CYAN}Access Points After Completion:${NC}"
echo -e "  🌐 Custom Domain: ${WHITE}https://brainstormx.ca${NC}"
echo -e "  🌐 WWW Domain: ${WHITE}https://www.brainstormx.ca${NC} (if configured)"
echo -e "  🌐 Original EC2: ${WHITE}https://your-ec2-dns.amazonaws.com${NC} (still works)"
echo ""

echo -e "${CYAN}Key Features Enabled:${NC}"
echo -e "  ✅ Custom domain with valid SSL certificate"
echo -e "  ✅ Automatic HTTPS redirects"
echo -e "  ✅ Auto-renewing SSL certificates"
echo -e "  ✅ Full BrainStormX functionality"
echo -e "  ✅ AI/speech features (with proper credentials)"
echo -e "  ✅ Production-ready configuration"
echo ""

echo -e "${CYAN}Management Commands:${NC}"
echo -e "  📊 Check services: ${WHITE}sudo systemctl status brainstormx nginx${NC}"
echo -e "  🔒 Check SSL: ${WHITE}sudo certbot certificates${NC}"
echo -e "  📝 View logs: ${WHITE}sudo journalctl -u brainstormx -f${NC}"
echo -e "  🔄 Restart: ${WHITE}sudo systemctl restart brainstormx nginx${NC}"
echo ""

echo -e "${YELLOW}Important Notes:${NC}"
echo -e "  • SSL certificates auto-renew every 60 days"
echo -e "  • Daily backups are configured automatically"
echo -e "  • Services auto-start on reboot"
echo -e "  • Firewall is configured for security"
echo ""

echo -e "${GREEN}For support: patrick@broadcomms.net${NC}"
echo -e "${GREEN}Documentation: /home/brainstormx/domain_setup_report.txt${NC}"