# BrainStormX Deployment Scripts Summary

## 📁 Available Scripts

| Script | Purpose | Duration | Prerequisites |
|--------|---------|----------|---------------|
| [`ec2_auto_deploy.sh`](./ec2_auto_deploy.sh) | Complete automated deployment | 10-15 min | Fresh Ubuntu 24.04 EC2 |
| [`domain_ssl_setup.sh`](./domain_ssl_setup.sh) | Custom domain & SSL setup | 5-10 min | Basic deployment + DNS configured |
| [`deployment_workflow_example.sh`](./deployment_workflow_example.sh) | Reference workflow guide | N/A | Documentation only |
| [`validate_deployment.sh`](./validate_deployment.sh) | Post-deployment validation | 1-2 min | After deployment |

## 🚀 Quick Start Commands

### Basic Deployment (EC2 DNS with Self-Signed SSL)
```bash
# Download and run main deployment
wget "https://raw.githubusercontent.com/broadcomms/BrainStormX/main/scripts/ec2_auto_deploy.sh?$(date +%s)" -O ec2_auto_deploy.sh
chmod +x ec2_auto_deploy.sh
sudo ./ec2_auto_deploy.sh

# Result: https://ec2-xx-xx-xx-xx.compute-1.amazonaws.com
```

### Custom Domain Deployment (Valid SSL Certificate)
```bash
# 1. Complete basic deployment first (above)

# 2. Configure DNS A record: brainstormx.ca → your_ec2_ip

# 3. Download and run domain setup
wget "https://raw.githubusercontent.com/broadcomms/BrainStormX/main/scripts/domain_ssl_setup.sh?$(date +%s)" -O domain_ssl_setup.sh
chmod +x domain_ssl_setup.sh
sudo ./domain_ssl_setup.sh

# Result: https://brainstormx.ca (with valid SSL)
```

## 📋 Deployment Workflow

### Standard Workflow
1. **Launch EC2 Instance** (Ubuntu 24.04 LTS, t3.small+)
2. **Configure Security Groups** (ports 22, 80, 443)
3. **Run Basic Deployment** (`ec2_auto_deploy.sh`)
4. **Configure Application** (AWS credentials, email settings)
5. **Access via EC2 DNS** (with self-signed SSL)

### Custom Domain Workflow
1. **Complete Standard Workflow** (steps 1-5 above)
2. **Configure DNS Records** (A record pointing to EC2 IP)
3. **Wait for DNS Propagation** (5-30 minutes typically)
4. **Run Domain Setup** (`domain_ssl_setup.sh`)
5. **Access via Custom Domain** (with valid Let's Encrypt SSL)

## 🎯 What Each Script Provides

### `ec2_auto_deploy.sh` - Core Deployment
- ✅ Complete BrainStormX installation
- ✅ System dependencies (Python, Nginx, etc.)
- ✅ AI/Speech components (Piper TTS, Vosk STT)
- ✅ Self-signed SSL certificate
- ✅ Systemd services and auto-start
- ✅ Firewall configuration (UFW)
- ✅ Automated backups and log rotation
- ✅ Interactive AWS/email configuration
- ✅ EC2 instance reset capability

**Provides**: Working application at `https://ec2-dns.amazonaws.com`

### `domain_ssl_setup.sh` - Domain Enhancement
- ✅ Custom domain configuration
- ✅ Let's Encrypt SSL certificates (valid, trusted)
- ✅ Automatic certificate renewal
- ✅ HTTP to HTTPS redirects
- ✅ DNS validation and verification
- ✅ Nginx reconfiguration for domain
- ✅ Complete setup verification

**Provides**: Production domain at `https://yourdomain.ca`

## 🔧 Configuration Requirements

### EC2 Instance Requirements
- **OS**: Ubuntu 24.04 LTS (recommended), 22.04 LTS, or 20.04 LTS  
- **Instance Type**: t3.small minimum (t3.medium+ recommended for production)
- **Storage**: 32GB minimum (64GB recommended)
- **Memory**: 2GB minimum (4GB+ recommended)

### Security Group Configuration
```
Port 22 (SSH)   - Your IP only
Port 80 (HTTP)  - 0.0.0.0/0  
Port 443 (HTTPS) - 0.0.0.0/0
```

### DNS Configuration (for Custom Domain)
```
Type: A
Name: yourdomain.ca
Value: your.ec2.public.ip
TTL: 300

# Optional WWW subdomain
Type: A  
Name: www.yourdomain.ca
Value: your.ec2.public.ip
TTL: 300
```

## 📊 Feature Comparison

| Feature | Basic Deployment | + Custom Domain |
|---------|------------------|-----------------|
| **SSL Certificate** | Self-signed (browser warnings) | Let's Encrypt (trusted) |
| **Domain** | EC2 DNS only | Custom domain |
| **Professional Appearance** | ❌ | ✅ |
| **Production Ready** | ⚠️ (functional) | ✅ (fully ready) |
| **Setup Complexity** | Simple | Requires DNS setup |
| **Maintenance** | Basic | Auto SSL renewal |

## 🛠️ Management Commands

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

### SSL Management (Custom Domain Only)
```bash
# Check SSL certificates
sudo certbot certificates

# Test renewal
sudo certbot renew --dry-run

# Manual renewal
sudo certbot renew
```

### Application Configuration
```bash
# Edit environment settings
sudo nano /home/brainstormx/BrainStormX/.env

# Restart after changes
sudo systemctl restart brainstormx
```

## 🐛 Common Issues & Solutions

### Basic Deployment Issues
- **Services won't start**: Check logs with `sudo journalctl -u brainstormx`
- **Permission errors**: Verify `/home/brainstormx` permissions
- **Python errors**: Check virtual environment and dependencies

### Domain Setup Issues
- **DNS not resolving**: Wait for propagation (5-30 min typically)
- **SSL certificate failed**: Verify DNS points to correct IP
- **Domain not accessible**: Check firewall and service status

### Quick Fixes
```bash
# Fix common permission issues
sudo chmod 755 /home/brainstormx
sudo chmod 755 /home/brainstormx/BrainStormX
sudo chmod -R 755 /home/brainstormx/BrainStormX/app/static

# Restart everything
sudo systemctl restart brainstormx nginx

# Check what's running
sudo netstat -tulpn | grep :80
sudo netstat -tulpn | grep :443
```

## 📞 Support

- **Email**: patrick@broadcomms.net
- **GitHub**: https://github.com/broadcomms/BrainStormX
- **Generated Reports**: 
  - `/tmp/brainstormx_deploy.log` (basic deployment)
  - `/home/brainstormx/deployment_report.txt` (basic deployment report)
  - `/home/brainstormx/domain_setup_report.txt` (domain setup report)

## 🎯 Success Checklist

### Basic Deployment Success
- [ ] ✅ Services running: `sudo systemctl status brainstormx nginx`
- [ ] ✅ HTTPS access: `https://ec2-xx-xx-xx-xx.compute-1.amazonaws.com`
- [ ] ✅ Application loads without errors
- [ ] ✅ Can create user accounts (with email configured)
- [ ] ✅ AI features work (with AWS credentials configured)

### Custom Domain Success  
- [ ] ✅ DNS resolves: `dig yourdomain.ca`
- [ ] ✅ HTTPS works: `https://yourdomain.ca` (no browser warnings)
- [ ] ✅ HTTP redirects: `http://yourdomain.ca` → `https://yourdomain.ca`
- [ ] ✅ SSL auto-renewal: `sudo systemctl status certbot.timer`
- [ ] ✅ Both domains work (if WWW configured)

---

**Total Deployment Time**: 15-25 minutes (including domain setup)  
**Maintenance**: Minimal (auto-backups, auto-SSL renewal, auto-restart)  
**Support**: Full documentation and troubleshooting guides included