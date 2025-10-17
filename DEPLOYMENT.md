# BrainStormX Deployment Script

## Quick Start

Deploy your latest changes to the live site with a single command:

```bash
./deploy_live_site.sh
```

## Features

### 🚀 **Complete Automation**
- Automatically pulls latest changes from GitHub
- Restarts BrainStormX and Nginx services
- Verifies deployment success
- Provides detailed status and error reporting

### 🛡️ **Safety Checks**
- Verifies SSH connectivity before deployment
- Checks for uncommitted local changes
- Confirms services are running properly
- Tests live site response after deployment

### 📊 **Smart Deployment**
- Only deploys when changes are detected
- Shows exactly what changed between deployments
- Provides memory usage and service health metrics
- Generates comprehensive deployment reports

## Usage Options

### Basic Deployment
```bash
./deploy_live_site.sh
```
Normal deployment - pulls changes, restarts services, verifies success.

### Check What Would Be Updated
```bash
./deploy_live_site.sh --check-only
```
Shows what changes would be deployed without actually deploying.

### Force Service Restart
```bash
./deploy_live_site.sh --force-restart
```
Restarts services even if no code changes are detected.

### Get Help
```bash
./deploy_live_site.sh --help
```
Shows detailed usage information and examples.

## What The Script Does

1. **Prerequisites Check**
   - ✅ Verifies SSH key exists and has correct permissions
   - ✅ Tests SSH connectivity to live server
   - ✅ Confirms you're in the BrainStormX git repository

2. **Local Status Check**
   - 📋 Shows any uncommitted changes
   - 📤 Pushes latest changes to GitHub if needed
   - 🔍 Displays current commit information

3. **Remote Status Check**
   - 🔍 Compares local and remote commit versions
   - 📊 Shows current service status
   - ⚡ Determines if deployment is needed

4. **Deployment Process**
   - 📥 Pulls latest changes on live server
   - 🔄 Restarts BrainStormX application service
   - 🌐 Reloads Nginx configuration
   - ⏱️ Waits for services to stabilize

5. **Verification & Reporting**
   - 🌐 Tests live site HTTP response
   - ✅ Verifies deployed commit matches local
   - 📈 Checks service health and memory usage
   - 📋 Generates deployment success report

## Configuration

The script automatically uses these settings:

- **Live Server**: `brainstormx.ca`
- **SSH Key**: `instance/brainstorm_x_vm_sshkey.pem`
- **App Directory**: `/home/brainstormx/BrainStormX`
- **Service Name**: `brainstormx`

## Example Output

```
==============================================================================
              BrainStormX Live Site Deployment Script v1.0.0
==============================================================================

==============================================================================
CHECKING PREREQUISITES
==============================================================================

✓ SSH key found: instance/brainstorm_x_vm_sshkey.pem
✓ SSH connection to live server successful
✓ Git repository detected
✓ On main branch

==============================================================================
CHECKING LOCAL REPOSITORY STATUS
==============================================================================

✓ Working directory is clean
ℹ Current commit: a1b2c3d - Update AWS Bedrock service names in marketing content
✓ Repository is up to date with origin/main

==============================================================================
CHECKING REMOTE SERVER STATUS
==============================================================================

ℹ Live server commit: x7y8z9a
ℹ Local commit:       a1b2c3d
⚠ Live server needs to be updated
✓ BrainStormX service is running
✓ Nginx service is running

==============================================================================
DEPLOYING TO LIVE SERVER
==============================================================================

➤ Pulling latest changes from GitHub...
✓ Successfully pulled latest changes
ℹ Changes deployed:
 14 files changed, 3059 insertions(+), 1739 deletions(-)

➤ Restarting BrainStormX application...
➤ Waiting for service to start...
✓ BrainStormX service restarted successfully
➤ Reloading Nginx configuration...
✓ Nginx configuration reloaded

==============================================================================
VERIFYING DEPLOYMENT
==============================================================================

➤ Testing live site response...
✓ Live site is responding correctly (HTTP 200)
➤ Verifying deployed version...
✓ Deployment verified - commit a1b2c3d is live
➤ Checking service health...
ℹ Service memory usage: 692MB

==============================================================================
DEPLOYMENT COMPLETED SUCCESSFULLY
==============================================================================

🚀 BrainStormX deployment successful!

Deployment Details:
  📅 Time: Thu Oct 17 2025 14:30:45
  🔗 Commit: a1b2c3d
  📝 Message: Update AWS Bedrock service names in marketing content

Access Points:
  🌐 Live Site: https://brainstormx.ca
  🔒 Admin: https://brainstormx.ca/admin

Services Status:
  ✅ BrainStormX Application: Running
  ✅ Nginx Reverse Proxy: Running
  ✅ SSL Certificate: Active

💡 Next Steps:
  • Test the live site functionality
  • Check application logs: sudo journalctl -u brainstormx -f
  • Monitor performance and errors

For support: patrick@broadcomms.net
Deployment completed at: Thu Oct 17 2025 14:30:45
```

## Troubleshooting

### Common Issues

**SSH Key Permissions Error**
```bash
chmod 400 instance/brainstorm_x_vm_sshkey.pem
```

**Can't Connect to Server**
- Check your internet connection
- Verify the server is running
- Ensure SSH key is correct

**Service Won't Start**
- Check logs: `ssh -i instance/brainstorm_x_vm_sshkey.pem ubuntu@brainstormx.ca "sudo journalctl -u brainstormx -n 20"`
- Verify configuration files
- Check for port conflicts

**Site Returns 502/503 Error**
- Wait a few seconds for service to fully start
- Check Nginx configuration
- Verify application is listening on correct port

### Log Files

Deployment logs are saved to `/tmp/brainstormx_deploy.log` for debugging.

## Security Notes

- SSH key is never transmitted over the network
- All connections use encrypted SSH tunnels
- Script checks key permissions automatically
- No passwords or sensitive data in logs

## Support

For deployment issues or questions:
- **Email**: patrick@broadcomms.net
- **Repository**: [BrainStormX on GitHub](https://github.com/broadcomms/BrainStormX)

---

*Last Updated: October 17, 2025*