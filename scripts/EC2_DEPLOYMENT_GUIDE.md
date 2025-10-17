# BrainStormX EC2 Automated Deployment Guide

## Overview
This guide provides a fully automated deployment script for BrainStormX on AWS EC2. The script handles everything from system dependencies to SSL configuration, requiring minimal user interaction.

## 🚀 Quick Start

### Step 1: Launch EC2 Instance
1. Create an Ubuntu 24.04 LTS EC2 instance (t3.small or larger)
2. Configure Security Groups:
   - Port 22 (SSH) - Your IP only
   - Port 80 (HTTP) - 0.0.0.0/0
   - Port 443 (HTTPS) - 0.0.0.0/0
3. Create/assign a key pair for SSH access

### Step 2: Connect to Instance
```bash
# Replace with your key and instance details
ssh -i "instance/brainstorm_x_vm_sshkey.pem" ubuntu@ec2-13-222-58-210.compute-1.amazonaws.com
```

### Step 3: Download and Run Deployment Script
```bash
# Download the deployment script
wget https://raw.githubusercontent.com/broadcomms/BrainStormX/scripts/ec2_auto_deploy.sh

# Make it executable
chmod +x ec2_auto_deploy.sh

# Run the deployment (takes 10-15 minutes)
sudo ./ec2_auto_deploy.sh
```

That's it! The script handles everything automatically.

## 📋 What the Script Does

### System Setup
- ✅ Updates Ubuntu packages
- ✅ Installs Python 3.10+, Nginx, Git, and dependencies
- ✅ Creates dedicated `brainstormx` user
- ✅ Configures firewall (UFW)

### Application Installation  
- ✅ Downloads BrainStormX from GitHub
- ✅ Sets up Python virtual environment
- ✅ Installs all Python dependencies
- ✅ Configures environment variables

### AI/Speech Features
- ✅ Installs Piper TTS engine and models
- ✅ Downloads Vosk speech recognition model
- ✅ Configures audio processing libraries

### Web Server Setup
- ✅ Configures Gunicorn WSGI server
- ✅ Sets up Nginx reverse proxy
- ✅ Creates self-signed SSL certificate
- ✅ Enables HTTPS with security headers

### Production Services
- ✅ Creates systemd service for auto-start
- ✅ Configures log rotation
- ✅ Sets up automated backups
- ✅ Enables crash recovery

## 🌐 Access Your Application

After deployment completes:

### Web Access
- **HTTPS**: `https://ec2-13-222-58-210.compute-1.amazonaws.com`
- **HTTP**: `http://ec2-13-222-58-210.compute-1.amazonaws.comm` (redirects to HTTPS)

### SSL Certificate Note
The script creates a self-signed SSL certificate. Browsers will show a security warning:
1. Click "Advanced" or "More Information"
2. Click "Proceed to site (unsafe)" or similar
3. Site will load normally with full functionality

## ⚙️ Post-Deployment Configuration

### 1. Configure AWS Credentials (Required for AI features)
```bash
sudo nano /home/brainstormx/brainstorm_x/.env
```

Add your AWS credentials:
```env
AWS_ACCESS_KEY_ID=your_access_key_here
AWS_SECRET_ACCESS_KEY=your_secret_key_here
```

### 2. Configure Email Settings (Required for user registration)
```env
MAIL_SERVER=your.smtp.server.com
MAIL_PORT=587
MAIL_USERNAME=your-email@domain.com
MAIL_PASSWORD=your-email-password
MAIL_DEFAULT_SENDER=your-email@domain.com
MAIL_SUPPRESS_SEND=False
```

### 3. Restart Services After Configuration Changes
```bash
sudo systemctl restart brainstormx nginx
```

## 🔧 Management Commands

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

### File Locations
- **Application**: `/home/brainstormx/brainstorm_x/`
- **Config**: `/home/brainstormx/brainstorm_x/.env`
- **Logs**: `/home/brainstormx/brainstorm_x/instance/logs/`
- **Backups**: `/home/brainstormx/backups/`

## 🔒 Security Features

### Automatic Security Setup
- ✅ UFW firewall configured
- ✅ SSL/HTTPS enabled with security headers
- ✅ Nginx security configurations
- ✅ User privilege separation
- ✅ File permission hardening

### Security Recommendations
1. **Use IAM Roles**: Instead of access keys, configure EC2 IAM role for AWS services
2. **Domain SSL**: Replace self-signed cert with real SSL for production
3. **Regular Updates**: Keep system packages updated
4. **Strong Passwords**: Change default passwords
5. **VPC Security**: Use private subnets for production

## 🔄 Backup & Recovery

### Automatic Backups
- Daily database backups at 2 AM
- Automatic cleanup (keeps 7 days)
- Backs up uploads and user data

### Manual Backup
```bash
# Run backup manually
sudo -u brainstormx /home/brainstormx/backup.sh

# View backups
ls -la /home/brainstormx/backups/
```

### Recovery
```bash
# Restore database
sudo -u brainstormx cp /home/brainstormx/backups/database_YYYYMMDD_HHMMSS.sqlite /home/brainstormx/brainstorm_x/instance/app_database.sqlite

# Restart application
sudo systemctl restart brainstormx
```

## 🐛 Troubleshooting

### Common Issues

#### Services Won't Start
```bash
# Check logs
sudo journalctl -u brainstormx --no-pager -n 50

# Test application directly
sudo -u brainstormx bash -c "cd /home/brainstormx/brainstorm_x && source venv/bin/activate && python run.py"
```

#### Permission Errors (403 Forbidden)
```bash
# Fix static file permissions
sudo chmod 755 /home/brainstormx
sudo chmod 755 /home/brainstormx/brainstorm_x
sudo chmod -R 755 /home/brainstormx/brainstorm_x/app/static
```

#### SSL Issues
```bash
# Test certificate
curl -k -I https://ec2-13-222-58-210.compute-1.amazonaws.com

# Recreate certificate if needed
sudo openssl req -x509 -nodes -days 365 -newkey rsa:2048 \
  -keyout /etc/ssl/private/brainstormx-selfsigned.key \
  -out /etc/ssl/certs/brainstormx-selfsigned.crt \
  -subj "/C=US/ST=State/L=City/O=BrainStormX/OU=IT/CN=your-ec2-dns.amazonaws.com"
```

#### Audio/TTS Issues
```bash
# Test Piper TTS
sudo -u brainstormx bash -c "cd /home/brainstormx/brainstorm_x && source venv/bin/activate && ./venv/bin/piper --version"

# Test Vosk model
sudo -u brainstormx ls -la /home/brainstormx/brainstorm_x/stt_models/vosk-model-en-us-0.22-lgraph/
```

## 📊 Monitoring

### Health Checks
```bash
# Application health
curl -I http://127.0.0.1:5001

# SSL health  
curl -k -I https://ec2-13-222-58-210.compute-1.amazonaws.com

# Service status
sudo systemctl is-active brainstormx nginx
```

### Resource Monitoring
```bash
# CPU and memory usage
htop

# Disk space
df -h

# Network connections
netstat -tulpn | grep :5001
```

## 🆙 Updates

### Update Application Code
```bash
# Pull latest code
sudo -u brainstormx bash -c "cd /home/brainstormx/brainstorm_x && git pull origin staging"

# Restart services
sudo systemctl restart brainstormx
```

### Update System Packages
```bash
sudo apt update && sudo apt upgrade -y
sudo systemctl restart brainstormx nginx
```

## 📞 Support

- **Email**: patrick@broadcomms.net
- **GitHub**: https://github.com/broadcomms/BrainStormX
- **Documentation**: See deployment report at `/home/brainstormx/deployment_report.txt`

## 🎯 Production Checklist

Before going live:

- [ ] Configure real domain name
- [ ] Install proper SSL certificate (Let's Encrypt)
- [ ] Set up AWS IAM roles
- [ ] Configure email settings
- [ ] Test all application features
- [ ] Set up monitoring/alerting
- [ ] Create backup strategy
- [ ] Security audit and hardening
- [ ] Performance testing
- [ ] Disaster recovery plan

---

**Deployment Time**: ~10-15 minutes  
**Supported OS**: Ubuntu 24.04 LTS, Ubuntu 22.04 LTS, Ubuntu 20.04 LTS  
**Minimum Instance**: t3.small (2 vCPU, 2GB RAM, 32GB disk)  
**Recommended**: t3.medium+ for production workloads