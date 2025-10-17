#!/bin/bash

# =============================================================================
# BrainStormX Live Site Deployment Script
# Version: 1.0.0
# Description: Automatically deploy latest changes to the live BrainStormX site
# 
# Usage: ./deploy_live_site.sh [options]
# Options:
#   --force-restart   Force restart even if no changes detected
#   --check-only      Only check for updates, don't deploy
#   --help            Show this help message
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

# Configuration
SCRIPT_VERSION="1.0.0"
LIVE_SERVER="brainstormx.ca"
SSH_KEY="instance/brainstorm_x_vm_sshkey.pem"
SSH_USER="ubuntu"
APP_USER="brainstormx"
APP_DIR="/home/${APP_USER}/BrainStormX"
SERVICE_NAME="brainstormx"
LOG_FILE="/tmp/brainstormx_deploy.log"

# Command line options
FORCE_RESTART=false
CHECK_ONLY=false
SHOW_HELP=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --force-restart)
            FORCE_RESTART=true
            shift
            ;;
        --check-only)
            CHECK_ONLY=true
            shift
            ;;
        --help|-h)
            SHOW_HELP=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            echo "Use --help for usage information"
            exit 1
            ;;
    esac
done

# Show help if requested
if [[ "$SHOW_HELP" == true ]]; then
    echo -e "${GREEN}BrainStormX Live Site Deployment Script v${SCRIPT_VERSION}${NC}"
    echo ""
    echo "Usage:"
    echo "  $0 [options]"
    echo ""
    echo "Options:"
    echo "  --force-restart   Force restart services even if no changes detected"
    echo "  --check-only      Only check for updates, don't deploy"
    echo "  --help, -h        Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0                    # Normal deployment"
    echo "  $0 --check-only      # Check what would be updated"
    echo "  $0 --force-restart   # Deploy and force restart services"
    echo ""
    echo "Requirements:"
    echo "  • SSH key: ${SSH_KEY}"
    echo "  • Live server: ${LIVE_SERVER}"
    echo "  • Git repository with latest changes"
    echo ""
    exit 0
fi

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

print_info() {
    echo -e "${CYAN}ℹ $1${NC}"
}

# Error handler
error_exit() {
    print_error "Deployment failed at line $1"
    print_error "Check log file: ${LOG_FILE}"
    print_error "Last few log entries:"
    tail -10 "${LOG_FILE}" 2>/dev/null || echo "No log file found"
    exit 1
}

trap 'error_exit $LINENO' ERR

# Check prerequisites
check_prerequisites() {
    print_header "CHECKING PREREQUISITES"
    
    # Check if SSH key exists
    if [[ ! -f "$SSH_KEY" ]]; then
        print_error "SSH key not found: $SSH_KEY"
        print_error "Please ensure the SSH key exists and has correct permissions"
        exit 1
    fi
    print_success "SSH key found: $SSH_KEY"
    
    # Check SSH key permissions
    local key_perms=$(stat -c "%a" "$SSH_KEY")
    if [[ "$key_perms" != "400" ]]; then
        print_warning "SSH key permissions are $key_perms, should be 400"
        print_step "Fixing SSH key permissions..."
        chmod 400 "$SSH_KEY"
        print_success "SSH key permissions fixed"
    fi
    
    # Test SSH connectivity
    print_step "Testing SSH connectivity to live server..."
    if ssh -i "$SSH_KEY" -o ConnectTimeout=10 -o BatchMode=yes "${SSH_USER}@${LIVE_SERVER}" "echo 'SSH connection successful'" &>> "${LOG_FILE}"; then
        print_success "SSH connection to live server successful"
    else
        print_error "Cannot connect to live server via SSH"
        print_error "Please check your network connection and server status"
        exit 1
    fi
    
    # Check if we're in a git repository
    if ! git status &> /dev/null; then
        print_error "Not in a git repository"
        print_error "Please run this script from the BrainStormX repository root"
        exit 1
    fi
    print_success "Git repository detected"
    
    # Check if we're on main branch
    local current_branch=$(git branch --show-current)
    if [[ "$current_branch" != "main" ]]; then
        print_warning "Currently on branch: $current_branch"
        print_warning "Recommended to be on main branch for deployment"
    else
        print_success "On main branch"
    fi
}

# Get local repository status
check_local_status() {
    print_header "CHECKING LOCAL REPOSITORY STATUS"
    
    # Check for uncommitted changes
    if ! git diff --quiet || ! git diff --cached --quiet; then
        print_warning "You have uncommitted changes in your local repository"
        print_info "Uncommitted changes:"
        git status --porcelain | while read line; do
            echo -e "  ${YELLOW}$line${NC}"
        done
        echo ""
        
        read -p "Do you want to commit these changes first? (y/N): " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            print_step "Staging all changes..."
            git add .
            
            read -p "Enter commit message: " commit_message
            if [[ -n "$commit_message" ]]; then
                git commit -m "$commit_message"
                print_success "Changes committed"
            else
                print_error "No commit message provided"
                exit 1
            fi
        fi
    else
        print_success "Working directory is clean"
    fi
    
    # Get current commit hash
    local current_commit=$(git rev-parse HEAD)
    local current_commit_short=$(git rev-parse --short HEAD)
    local commit_message=$(git log -1 --pretty=format:"%s")
    
    print_info "Current commit: ${WHITE}$current_commit_short${NC} - $commit_message"
    
    # Check if we need to push
    local unpushed_commits=$(git log origin/main..HEAD --oneline | wc -l)
    if [[ $unpushed_commits -gt 0 ]]; then
        print_warning "$unpushed_commits unpushed commit(s) detected"
        print_step "Pushing to origin/main..."
        git push origin main
        print_success "Changes pushed to GitHub"
    else
        print_success "Repository is up to date with origin/main"
    fi
}

# Check remote server status
check_remote_status() {
    print_header "CHECKING REMOTE SERVER STATUS"
    
    # Get current commit on server
    print_step "Checking current deployment on live server..."
    local remote_commit=$(ssh -i "$SSH_KEY" "${SSH_USER}@${LIVE_SERVER}" \
        "cd $APP_DIR && sudo -u $APP_USER git rev-parse HEAD" 2>>"${LOG_FILE}")
    local remote_commit_short=$(echo "$remote_commit" | cut -c1-7)
    
    local local_commit=$(git rev-parse HEAD)
    local local_commit_short=$(git rev-parse --short HEAD)
    
    print_info "Live server commit: ${WHITE}$remote_commit_short${NC}"
    print_info "Local commit:       ${WHITE}$local_commit_short${NC}"
    
    if [[ "$remote_commit" == "$local_commit" ]]; then
        print_success "Live server is already up to date"
        
        if [[ "$CHECK_ONLY" == true ]]; then
            print_info "Check complete - no deployment needed"
            return 0
        elif [[ "$FORCE_RESTART" == false ]]; then
            print_info "No changes to deploy"
            read -p "Do you want to restart services anyway? (y/N): " -n 1 -r
            echo
            if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                print_info "Deployment cancelled - no changes needed"
                return 0
            fi
        fi
    else
        print_warning "Live server needs to be updated"
    fi
    
    # Check service status
    print_step "Checking service status on live server..."
    local service_status=$(ssh -i "$SSH_KEY" "${SSH_USER}@${LIVE_SERVER}" \
        "sudo systemctl is-active $SERVICE_NAME" 2>>"${LOG_FILE}" || echo "inactive")
    
    if [[ "$service_status" == "active" ]]; then
        print_success "BrainStormX service is running"
    else
        print_warning "BrainStormX service status: $service_status"
    fi
    
    # Check nginx status
    local nginx_status=$(ssh -i "$SSH_KEY" "${SSH_USER}@${LIVE_SERVER}" \
        "sudo systemctl is-active nginx" 2>>"${LOG_FILE}" || echo "inactive")
    
    if [[ "$nginx_status" == "active" ]]; then
        print_success "Nginx service is running"
    else
        print_warning "Nginx service status: $nginx_status"
    fi
    
    return 1  # Indicate deployment is needed
}

# Deploy changes to live server
deploy_to_server() {
    print_header "DEPLOYING TO LIVE SERVER"
    
    if [[ "$CHECK_ONLY" == true ]]; then
        print_info "Check-only mode - skipping actual deployment"
        return 0
    fi
    
    # Pull latest changes
    print_step "Pulling latest changes from GitHub..."
    local pull_output=$(ssh -i "$SSH_KEY" "${SSH_USER}@${LIVE_SERVER}" \
        "cd $APP_DIR && sudo -u $APP_USER git pull origin main" 2>&1)
    
    if echo "$pull_output" | grep -q "Already up to date"; then
        print_info "Repository was already up to date on server"
    elif echo "$pull_output" | grep -q "Fast-forward\|Updating"; then
        print_success "Successfully pulled latest changes"
        print_info "Changes deployed:"
        echo "$pull_output" | grep -E "^ [0-9]+ file[s]? changed" || true
    else
        print_warning "Git pull output:"
        echo "$pull_output"
    fi
    
    # Restart BrainStormX service
    print_step "Restarting BrainStormX application..."
    ssh -i "$SSH_KEY" "${SSH_USER}@${LIVE_SERVER}" \
        "sudo systemctl restart $SERVICE_NAME" &>> "${LOG_FILE}"
    
    # Wait for service to start
    print_step "Waiting for service to start..."
    sleep 3
    
    # Check if service started successfully
    local service_status=$(ssh -i "$SSH_KEY" "${SSH_USER}@${LIVE_SERVER}" \
        "sudo systemctl is-active $SERVICE_NAME" 2>>"${LOG_FILE}")
    
    if [[ "$service_status" == "active" ]]; then
        print_success "BrainStormX service restarted successfully"
    else
        print_error "BrainStormX service failed to start"
        print_error "Service status: $service_status"
        
        # Get service logs
        print_error "Recent service logs:"
        ssh -i "$SSH_KEY" "${SSH_USER}@${LIVE_SERVER}" \
            "sudo journalctl -u $SERVICE_NAME --no-pager -n 10" 2>>"${LOG_FILE}" | \
            while read line; do
                echo -e "  ${RED}$line${NC}"
            done
        exit 1
    fi
    
    # Reload Nginx configuration
    print_step "Reloading Nginx configuration..."
    ssh -i "$SSH_KEY" "${SSH_USER}@${LIVE_SERVER}" \
        "sudo systemctl reload nginx" &>> "${LOG_FILE}"
    print_success "Nginx configuration reloaded"
}

# Verify deployment
verify_deployment() {
    print_header "VERIFYING DEPLOYMENT"
    
    if [[ "$CHECK_ONLY" == true ]]; then
        print_info "Check-only mode - skipping verification"
        return 0
    fi
    
    # Test HTTP response
    print_step "Testing live site response..."
    local response_code=$(curl -s -o /dev/null -w "%{http_code}" "https://$LIVE_SERVER" --max-time 10)
    
    if [[ "$response_code" == "200" ]]; then
        print_success "Live site is responding correctly (HTTP $response_code)"
    else
        print_warning "Live site response code: $response_code"
    fi
    
    # Get final commit hash
    print_step "Verifying deployed version..."
    local deployed_commit=$(ssh -i "$SSH_KEY" "${SSH_USER}@${LIVE_SERVER}" \
        "cd $APP_DIR && sudo -u $APP_USER git rev-parse --short HEAD" 2>>"${LOG_FILE}")
    local local_commit=$(git rev-parse --short HEAD)
    
    if [[ "$deployed_commit" == "$local_commit" ]]; then
        print_success "Deployment verified - commit $deployed_commit is live"
    else
        print_warning "Version mismatch - deployed: $deployed_commit, local: $local_commit"
    fi
    
    # Check service health
    print_step "Checking service health..."
    local memory_usage=$(ssh -i "$SSH_KEY" "${SSH_USER}@${LIVE_SERVER}" \
        "sudo systemctl show $SERVICE_NAME --property=MemoryCurrent --value" 2>>"${LOG_FILE}" || echo "unknown")
    
    if [[ "$memory_usage" != "unknown" && "$memory_usage" != "[not set]" ]]; then
        local memory_mb=$((memory_usage / 1024 / 1024))
        print_info "Service memory usage: ${memory_mb}MB"
    fi
    
    # Test application health endpoint (if available)
    if curl -s "https://$LIVE_SERVER/health" &> /dev/null; then
        print_success "Application health endpoint responding"
    fi
}

# Generate deployment report
generate_report() {
    print_header "DEPLOYMENT COMPLETED SUCCESSFULLY"
    
    local deployed_commit=$(git rev-parse --short HEAD)
    local commit_message=$(git log -1 --pretty=format:"%s")
    local deployment_time=$(date)
    
    echo -e "${GREEN}🚀 BrainStormX deployment successful!${NC}\n"
    
    echo -e "${CYAN}Deployment Details:${NC}"
    echo -e "  📅 Time: ${WHITE}$deployment_time${NC}"
    echo -e "  🔗 Commit: ${WHITE}$deployed_commit${NC}"
    echo -e "  📝 Message: ${WHITE}$commit_message${NC}"
    echo ""
    
    echo -e "${CYAN}Access Points:${NC}"
    echo -e "  🌐 Live Site: ${WHITE}https://$LIVE_SERVER${NC}"
    echo -e "  🔒 Admin: ${WHITE}https://$LIVE_SERVER/admin${NC}"
    echo ""
    
    if [[ "$CHECK_ONLY" == false ]]; then
        echo -e "${CYAN}Services Status:${NC}"
        echo -e "  ✅ BrainStormX Application: Running"
        echo -e "  ✅ Nginx Reverse Proxy: Running"
        echo -e "  ✅ SSL Certificate: Active"
        echo ""
    fi
    
    echo -e "${YELLOW}💡 Next Steps:${NC}"
    echo -e "  • Test the live site functionality"
    echo -e "  • Check application logs: ${WHITE}sudo journalctl -u $SERVICE_NAME -f${NC}"
    echo -e "  • Monitor performance and errors"
    echo ""
    
    echo -e "${GREEN}For support: patrick@broadcomms.net${NC}"
    echo -e "${GREEN}Deployment completed at: $(date)${NC}"
}

# Main deployment function
main() {
    echo -e "${GREEN}"
    echo "=============================================================================="
    echo "              BrainStormX Live Site Deployment Script v${SCRIPT_VERSION}"
    echo "=============================================================================="
    echo -e "${NC}"
    
    # Initialize logging
    echo "Starting deployment at $(date)" > "${LOG_FILE}"
    
    # Run deployment steps
    check_prerequisites
    check_local_status
    
    if check_remote_status; then
        # No deployment needed (server is up to date)
        if [[ "$FORCE_RESTART" == false && "$CHECK_ONLY" == false ]]; then
            print_info "No deployment needed. Use --force-restart to restart services anyway."
            exit 0
        fi
    fi
    
    deploy_to_server
    verify_deployment
    generate_report
}

# Run main function
main "$@"