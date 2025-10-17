# BrainStormX Deployment Scripts

This directory contains automated deployment and management scripts for BrainStormX.

## 🚀 Automated EC2 Deployment

### [`ec2_auto_deploy.sh`](./ec2_auto_deploy.sh)
**Complete automated deployment script for AWS EC2 Ubuntu instances**

- **Purpose**: Fully automates BrainStormX deployment from scratch
- **Duration**: 10-15 minutes
- **Requirements**: Fresh Ubuntu 24.04 LTS EC2 instance with internet access
- **What it does**: 
  - Installs all system dependencies
  - Sets up Python environment and application
  - Configures Nginx, SSL, and systemd services
  - Installs Piper TTS and Vosk STT
  - Sets up firewall, backups, and monitoring

**Usage:**
```bash
# On your EC2 instance
wget https://raw.githubusercontent.com/broadcomms/BrainStormX/scripts/ec2_auto_deploy.sh
chmod +x ec2_auto_deploy.sh
sudo ./ec2_auto_deploy.sh
```

### [`validate_deployment.sh`](./validate_deployment.sh)
**Post-deployment validation and health check script**

- **Purpose**: Validates that deployment completed successfully
- **Duration**: 1-2 minutes
- **Requirements**: Run after `ec2_auto_deploy.sh` completes
- **What it checks**:
  - Service status (systemd, nginx)
  - Network connectivity (HTTP/HTTPS)
  - File permissions and SSL certificates
  - Application features (TTS, STT, Python environment)
  - Security configuration and backup system

**Usage:**
```bash
# After deployment completes
sudo ./validate_deployment.sh
```

## 📚 Documentation

### [`EC2_DEPLOYMENT_GUIDE.md`](./EC2_DEPLOYMENT_GUIDE.md)
**Complete user guide for EC2 deployment**

Comprehensive documentation covering:
- Quick start instructions
- Post-deployment configuration
- Management commands
- Troubleshooting guide
- Security recommendations
- Backup and recovery procedures

## 🛠️ Script Features

### Automated Deployment (`ec2_auto_deploy.sh`)
- ✅ **Zero-config deployment** - Just run the script
- ✅ **Production-ready** - SSL, firewall, systemd services
- ✅ **Error handling** - Stops on errors with detailed logging
- ✅ **Progress tracking** - Color-coded output with status updates
- ✅ **Auto-detection** - Detects EC2 environment and configures accordingly
- ✅ **Comprehensive logging** - Full deployment log for troubleshooting
- ✅ **Backup setup** - Automated daily backups with retention
- ✅ **Security hardening** - UFW firewall, SSL, security headers

### Validation Script (`validate_deployment.sh`)
- ✅ **Health checks** - Services, network, SSL, permissions
- ✅ **Feature testing** - AI components, TTS, STT functionality  
- ✅ **Security audit** - Firewall, headers, certificate validation
- ✅ **Comprehensive report** - Detailed validation summary

## 🔧 System Requirements

### Minimum Requirements
- **OS**: Ubuntu 24.04 LTS (recommended), 22.04 LTS, or 20.04 LTS
- **Instance**: AWS EC2 t3.small or larger
- **Memory**: 2GB RAM minimum
- **Storage**: 32GB disk space
- **Network**: Internet access for package downloads

### Security Groups
Ensure your EC2 security group allows:
- Port 22 (SSH) - Your IP only
- Port 80 (HTTP) - 0.0.0.0/0
- Port 443 (HTTPS) - 0.0.0.0/0

## 📝 Usage Examples

### Quick Deployment
```bash
# 1. Launch Ubuntu 24.04 LTS EC2 instance
# 2. SSH to instance
ssh -i "your-key.pem" ubuntu@your-ec2-dns.amazonaws.com

# 3. Run deployment script
wget https://raw.githubusercontent.com/broadcomms/BrainStormX/scripts/ec2_auto_deploy.sh
chmod +x ec2_auto_deploy.sh
sudo ./ec2_auto_deploy.sh

# 4. Validate deployment
sudo ./validate_deployment.sh

# 5. Access your app
# https://your-ec2-dns.amazonaws.com
```

### Post-Deployment Configuration
```bash
# Configure AWS credentials
sudo nano /home/brainstormx/brainstorm_x/.env

# Restart services after config changes
sudo systemctl restart brainstormx nginx

# Check service status
sudo systemctl status brainstormx nginx
```

## 🔍 Troubleshooting

### Common Issues
1. **Script fails**: Check `/tmp/brainstormx_deploy.log` for details
2. **Services won't start**: Check `sudo journalctl -u brainstormx`
3. **SSL warnings**: Normal for self-signed certificates
4. **Permission errors**: Run validation script to check permissions
5. **Network issues**: Verify security group settings

### Debug Commands
```bash
# Check deployment log
tail -f /tmp/brainstormx_deploy.log

# Test application directly
sudo -u brainstormx bash -c "cd /home/brainstormx/brainstorm_x && source venv/bin/activate && python run.py"

# Check Nginx configuration
sudo nginx -t

# View service logs
sudo journalctl -u brainstormx -f
sudo journalctl -u nginx -f
```

## 📞 Support

- **Email**: patrick@broadcomms.net
- **GitHub Issues**: https://github.com/broadcomms/brainstorm_x/issues
- **Documentation**: Check deployment report at `/home/brainstormx/deployment_report.txt`

## 🔄 Updates

### Update Scripts
```bash
# Download latest scripts
wget -O ec2_auto_deploy.sh https://raw.githubusercontent.com/broadcomms/BrainStormX/scripts/ec2_auto_deploy.sh
wget -O validate_deployment.sh https://raw.githubusercontent.com/broadcomms/BrainStormX/scripts/validate_deployment.sh
chmod +x *.sh
```

### Update Application
```bash
# Pull latest application code
sudo -u brainstormx bash -c "cd /home/brainstormx/brainstorm_x && git pull origin staging"
sudo systemctl restart brainstormx
```

---

**Last Updated**: October 16, 2025  
**Script Version**: 1.0.0  
**Compatibility**: Ubuntu 24.04 LTS, 22.04 LTS, 20.04 LTS  
**Tested On**: AWS EC2 t3.small, t3.medium, t3.large