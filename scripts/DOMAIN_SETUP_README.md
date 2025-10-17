# BrainStormX Domain & SSL Configuration

This script configures a custom domain and SSL certificate for an existing BrainStormX deployment. It's designed to be run **after** the initial deployment with `ec2_auto_deploy.sh`.

## 🎯 Overview

The `domain_ssl_setup.sh` script automates the process of:
- Configuring a custom domain name
- Setting up proper SSL certificates via Let's Encrypt
- Updating Nginx configuration
- Enabling automatic certificate renewal
- Verifying the complete setup

## 📋 Prerequisites

Before running this script, ensure:

1. ✅ **BrainStormX Already Deployed**: Run `ec2_auto_deploy.sh` first
2. ✅ **Domain DNS Configured**: Your domain's A record must point to your server's IP
3. ✅ **Valid Email**: You need an email address for Let's Encrypt notifications
4. ✅ **Ports Open**: Ensure ports 80 and 443 are accessible from the internet

## 🚀 Quick Start

### Step 1: Download the Script

```bash
# SSH into your EC2 instance
ssh -i "your-key.pem" ubuntu@your-ec2-instance.amazonaws.com

# Download the domain setup script
wget https://raw.githubusercontent.com/broadcomms/BrainStormX/main/scripts/domain_ssl_setup.sh

# Make it executable
chmod +x domain_ssl_setup.sh
```

### Step 2: Configure DNS Records

**IMPORTANT**: Before running the script, configure your DNS:

```
# Example DNS Configuration
Type: A
Name: brainstormx.ca
Value: 54.90.225.8 (your EC2 public IP)
TTL: 300

# Optional: WWW subdomain
Type: A  
Name: www.brainstormx.ca
Value: 54.90.225.8 (your EC2 public IP)
TTL: 300
```

### Step 3: Verify DNS Propagation

```bash
# Check if DNS is working (replace with your domain)
dig brainstormx.ca
nslookup brainstormx.ca

# The result should show your EC2 instance's public IP
```

### Step 4: Run the Domain Setup Script

```bash
# Run the script with sudo
sudo ./domain_ssl_setup.sh
```

The script will prompt you for:
- **Domain name** (e.g., `brainstormx.ca`)
- **WWW subdomain** preference (include `www.brainstormx.ca` or not)  
- **Email address** for Let's Encrypt notifications

## 📊 What the Script Does

### DNS & Prerequisites Check
- ✅ Verifies BrainStormX is already deployed
- ✅ Checks that required services are running
- ✅ Validates DNS resolution
- ✅ Installs certbot if needed

### Domain Configuration
- ✅ Prompts for domain name with validation
- ✅ Optional WWW subdomain configuration
- ✅ Email address collection for SSL notifications
- ✅ DNS verification before proceeding

### Nginx Configuration
- ✅ Updates Nginx to serve the new domain
- ✅ Configures proxy settings for BrainStormX
- ✅ Sets up static file serving
- ✅ Adds security headers

### SSL Certificate Setup
- ✅ Obtains certificates from Let's Encrypt
- ✅ Configures HTTPS with automatic HTTP redirect
- ✅ Sets up automatic certificate renewal
- ✅ Verifies SSL configuration

### Verification & Reporting
- ✅ Tests HTTPS access and redirects
- ✅ Validates SSL certificate
- ✅ Generates comprehensive setup report

## 🌐 Access Your Application

After successful completion:

### Primary Access
- **HTTPS**: `https://brainstormx.ca` (your domain)
- **HTTP**: `http://brainstormx.ca` (redirects to HTTPS)
- **WWW**: `https://www.brainstormx.ca` (if configured)

### Original Access Still Works
- **EC2 DNS**: `https://ec2-xx-xx-xx-xx.compute-1.amazonaws.com`

## 🔧 Management Commands

### SSL Certificate Management
```bash
# Check certificate status
sudo certbot certificates

# Test automatic renewal
sudo certbot renew --dry-run

# Manual renewal (if needed)
sudo certbot renew

# View certificate details
openssl x509 -in /etc/letsencrypt/live/brainstormx.ca/fullchain.pem -text -noout
```

### Service Management
```bash
# Check service status
sudo systemctl status brainstormx nginx certbot.timer

# Restart services
sudo systemctl restart brainstormx nginx

# View application logs  
sudo journalctl -u brainstormx -f

# View Nginx logs
sudo tail -f /var/log/nginx/access.log
sudo tail -f /var/log/nginx/error.log
```

### Configuration Files
```bash
# Nginx configuration
sudo nano /etc/nginx/sites-available/brainstormx

# Application environment
sudo -u brainstormx nano /home/brainstormx/BrainStormX/.env

# SSL certificate locations
ls -la /etc/letsencrypt/live/brainstormx.ca/
```

## 📈 Monitoring & Maintenance

### Automatic Features
- ✅ **SSL Auto-Renewal**: Certificates renew automatically before expiry
- ✅ **Service Auto-Start**: Services restart on reboot
- ✅ **Health Monitoring**: Built-in service health checks

### Manual Monitoring
```bash
# Check SSL expiry
echo | openssl s_client -connect brainstormx.ca:443 2>/dev/null | openssl x509 -noout -dates

# Test website health
curl -I https://brainstormx.ca

# Monitor disk space (certificates need space)
df -h
```

## 🐛 Troubleshooting

### Common Issues

#### 1. DNS Not Propagating
```bash
# Check current DNS resolution
dig brainstormx.ca

# If wrong IP, wait for DNS propagation (5-30 minutes typically)
# Or check with your DNS provider
```

#### 2. SSL Certificate Failed
```bash
# Check certbot logs
sudo tail -f /var/log/letsencrypt/letsencrypt.log

# Common causes:
# - DNS not pointing to server
# - Firewall blocking port 80/443  
# - Rate limiting (try again later)
# - Domain validation failed
```

#### 3. Nginx Configuration Error
```bash
# Test Nginx configuration
sudo nginx -t

# Check for syntax errors in config
sudo nano /etc/nginx/sites-available/brainstormx

# Reload if configuration is fixed
sudo systemctl reload nginx
```

#### 4. Domain Not Accessible
```bash
# Check if services are running
sudo systemctl status brainstormx nginx

# Check firewall
sudo ufw status

# Test local access first
curl -I http://127.0.0.1:5001

# Check DNS resolution
nslookup brainstormx.ca
```

#### 5. Mixed Content Errors
If you see mixed content warnings in browser:
- Clear browser cache
- Check that all resources load via HTTPS
- Verify application generates HTTPS URLs

### Emergency Recovery

If something goes wrong and you need to revert:

```bash
# Restore original Nginx configuration
sudo cp /etc/nginx/sites-available/brainstormx.backup /etc/nginx/sites-available/brainstormx
sudo nginx -t
sudo systemctl reload nginx

# Remove SSL certificate (if needed)
sudo certbot delete --cert-name brainstormx.ca

# The original EC2 DNS will still work for access
```

## 📊 Performance & Security

### Security Features Added
- ✅ **Valid SSL Certificate**: Industry-standard encryption
- ✅ **HTTP to HTTPS Redirect**: All traffic secured
- ✅ **Security Headers**: XSS protection, content type validation
- ✅ **HSTS Ready**: Can be enabled for enhanced security

### Performance Optimizations
- ✅ **Gzip Compression**: Faster page loads
- ✅ **Static File Caching**: Improved performance
- ✅ **Proper Proxy Configuration**: Optimized for Flask/Socket.IO

### Additional Security Recommendations
```bash
# Enable HTTP Strict Transport Security (optional)
# Add to Nginx config: add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;

# Set up fail2ban for additional protection
sudo apt install fail2ban

# Configure automatic security updates
sudo dpkg-reconfigure -plow unattended-upgrades
```

## 📞 Support

- **Email**: patrick@broadcomms.net
- **GitHub Issues**: https://github.com/broadcomms/BrainStormX/issues
- **Documentation**: See generated report at `/home/brainstormx/domain_setup_report.txt`

## 🔄 Script Updates

To get the latest version of the script:

```bash
# Download latest version
wget -O domain_ssl_setup.sh https://raw.githubusercontent.com/broadcomms/BrainStormX/main/scripts/domain_ssl_setup.sh

# Make executable
chmod +x domain_ssl_setup.sh

# Run updated script
sudo ./domain_ssl_setup.sh
```

## ✅ Success Checklist

After running the script, verify:

- [ ] ✅ Domain resolves to your server IP: `dig brainstormx.ca`
- [ ] ✅ HTTPS works: `https://brainstormx.ca` loads without warnings
- [ ] ✅ HTTP redirects: `http://brainstormx.ca` redirects to HTTPS
- [ ] ✅ WWW works (if configured): `https://www.brainstormx.ca`
- [ ] ✅ SSL certificate valid: Green lock in browser
- [ ] ✅ Auto-renewal enabled: `sudo systemctl status certbot.timer`
- [ ] ✅ Services running: `sudo systemctl status brainstormx nginx`

---

**Deployment Time**: ~5-10 minutes  
**Prerequisites**: Domain DNS configured, BrainStormX already deployed  
**SSL Certificate**: Let's Encrypt (90-day validity, auto-renewing)  
**Supported Domains**: Any valid domain name with DNS access