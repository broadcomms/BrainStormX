# BrainStormX Local Linux Deployment Guide (Python venv)

## Overview

This guide provides step-by-step instructions to deploy and run BrainStormX on a Linux/Unix server using Python virtual environments. It covers environment setup, dependencies, configuration, running the app, and troubleshooting.

---

## Prerequisites

- **Ubuntu 20.04+** (or any modern Linux distribution)
- **macOS 10.15+** (for macOS users)
- **Python 3.10+** (verify with `python3 --version`)
- **Git** (verify with `git --version`)
- **At least 4GB RAM** and **20GB free disk space**
- **Internet connection** for downloading models and dependencies

---

## 1. Install System Dependencies

### For Ubuntu/Debian Linux:
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git sqlite3 \
    tesseract-ocr libtesseract-dev ffmpeg build-essential \
    libssl-dev libffi-dev python3-dev curl wget unzip \
    libespeak-ng1 libespeak-ng-dev
```

### For macOS:
```bash
# Install Homebrew if not already installed
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"

# Install dependencies
brew install python3 git sqlite tesseract ffmpeg espeak-ng
```

---

## 2. Clone the Repository

```bash
# Clone the main branch (recommended for stable deployment)
git clone https://github.com/broadcomms/BrainStormX.git
cd BrainStormX

# Verify you're in the correct directory
pwd
ls -la
```

## 3. Create and Activate Python Virtual Environment

```bash
# Clone the main branch (recommended for stable deployment)
git clone https://github.com/broadcomms/BrainStormX.git
cd BrainStormX

# Verify you're in the correct directory
pwd
ls -la
```

**Important:** Always ensure the virtual environment is activated before running any pip commands or the application.

---

## 4. Install Python Dependencies

```bash
# Upgrade pip to latest version
pip install --upgrade pip

# Install all required dependencies
pip install -r requirements.txt

# Verify critical packages are installed
python -c "import flask, socketio, boto3; print('Core packages installed successfully')"
```

---
## 4a. Install Piper TTS Engine

Piper is an open-source text-to-speech engine required for AI voice features.

### For Linux (Ubuntu/Debian):

```bash
# Ensure you're in the BrainStormX directory and virtual environment is active
cd BrainStormX
source venv/bin/activate

# Download latest Piper release
PIPER_VERSION="2023.11.14-2"  # Update this to latest version if needed
wget "https://github.com/rhasspy/piper/releases/download/${PIPER_VERSION}/piper_linux_x86_64.tar.gz"

# Verify download
ls -la piper_linux_x86_64.tar.gz

# Extract Piper
tar -xzf piper_linux_x86_64.tar.gz

# Install Piper binary to virtual environment
cp piper/piper venv/bin/
chmod +x venv/bin/piper

# Install required libraries
sudo cp piper/lib*.so* /usr/local/lib/
sudo cp piper/libespeak-ng.so* /usr/local/lib/

# Install espeak-ng data
sudo mkdir -p /usr/share/espeak-ng-data
sudo cp -r piper/espeak-ng-data/* /usr/share/espeak-ng-data/

# Update library cache
sudo ldconfig

# Verify Piper installation
./venv/bin/piper --version

# Expected output: something like "1.2.0"
# If you see version info, Piper is correctly installed

# Clean up installation files
rm -rf piper piper_linux_x86_64.tar.gz
```

### For macOS:

```bash
# Ensure virtual environment is active
source venv/bin/activate

# Install Piper via pip (recommended for macOS)
pip install piper-tts

# Verify installation
python -c "import piper; print('Piper TTS installed successfully')"

# Check if piper command is available
which piper || echo "Piper binary not in PATH - will use Python module"
```

### Troubleshooting Piper Installation:

**Common Issues:**

1. **"piper: command not found"**
   ```bash
   # Check if piper exists in venv
   ls -la venv/bin/piper
   
   # If missing, re-run installation steps
   # Make sure virtual environment is activated
   ```

2. **"error while loading shared libraries"**
   ```bash
   # Update library cache
   sudo ldconfig
   
   # Check library paths
   ldd venv/bin/piper
   ```

3. **Permission errors**
   ```bash
   # Ensure binary is executable
   chmod +x venv/bin/piper
   ```

---

## 4b. Install Vosk Speech Recognition (STT) Model

Vosk provides offline speech-to-text functionality. Choose model size based on your needs:

### Model Options:
- **Small model** (~50MB): Faster, less accurate, good for testing
- **Large model** (~1.8GB): Slower, more accurate, recommended for production

### Download Small Model (Recommended for Testing):

```bash
# Ensure you're in BrainStormX directory
cd BrainStormX

# Create models directory
mkdir -p stt_models

# Download small English model
cd stt_models

# For Linux:
wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip

# For macOS (use curl):
curl -L -o vosk-model-small-en-us-0.15.zip https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip

# Extract model
unzip vosk-model-small-en-us-0.15.zip
rm vosk-model-small-en-us-0.15.zip

# Verify extraction
ls -la vosk-model-small-en-us-0.15/
# Should see files like: am/, conf/, graph/, ivector/

cd ..  # Return to BrainStormX directory
```

### Download Large Model (Recommended for Production):

```bash
# Only do this if you want better accuracy and have sufficient disk space
cd stt_models

# For Linux:
wget https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip

# For macOS:
curl -L -o vosk-model-en-us-0.22-lgraph.zip https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip

# Extract model
unzip vosk-model-en-us-0.22-lgraph.zip
rm vosk-model-en-us-0.22-lgraph.zip

# Verify extraction
ls -la vosk-model-en-us-0.22-lgraph/

cd ..  # Return to BrainStormX directory
```

### Verify Vosk Installation:

```bash
# Test Vosk module
python -c "import vosk; print('Vosk installed successfully')"

# Check model directory structure
ls -la stt_models/
```

### Troubleshooting Vosk:

**Common Issues:**

1. **Download failures**
   ```bash
   # Check internet connection
   ping alphacephei.com
   
   # Try alternative download with verbose output
   wget -v https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
   ```

2. **Extraction errors**
   ```bash
   # Check if unzip is installed
   which unzip || sudo apt install unzip  # Linux
   which unzip || brew install unzip      # macOS
   
   # Verify zip file integrity
   unzip -t vosk-model-small-en-us-0.15.zip
   ```

3. **Model not found errors**
   ```bash
   # Verify model directory exists and has correct structure
   find stt_models/ -name "*.conf" -type f
   ```

---

## 5. Configure Environment Variables

### Create Environment File:

```bash
# Copy the developer template
cp .env.developer .env

# Edit the configuration file
nano .env  # or use your preferred editor
```

### Critical Configuration Updates:

**For Linux Users:**
```bash
# Update these paths in your .env file:
PIPER_BIN=/full/path/to/BrainStormX/venv/bin/piper
PIPER_MODEL=/full/path/to/BrainStormX/tts_models/en_US-hfc_male-medium.onnx
VOSK_MODEL_PATH=/full/path/to/BrainStormX/stt_models/vosk-model-en-us-0.22-lgraph

# Replace /full/path/to with your actual path. Find it with:
pwd
```

**For macOS Users:**
```bash
# Update these paths in your .env file:
PIPER_BIN=/Users/yourusername/BrainStormX/venv/bin/piper
PIPER_MODEL=/Users/yourusername/BrainStormX/tts_models/en_US-hfc_male-medium.onnx
VOSK_MODEL_PATH=/Users/yourusername/BrainStormX/stt_models/vosk-model-en-us-0.22-lgraph

# Or if using small model:
VOSK_MODEL_PATH=/Users/yourusername/BrainStormX/stt_models/vosk-model-small-en-us-0.15
```

### Get Absolute Paths:
```bash
# Get current directory (should be BrainStormX)
echo "Current directory: $(pwd)"

# Get absolute path for VOSK_MODEL_PATH
echo "VOSK_MODEL_PATH=$(pwd)/stt_models/vosk-model-en-us-0.22-lgraph"

# Get absolute path for PIPER_BIN
echo "PIPER_BIN=$(pwd)/venv/bin/piper"
```

### Essential Configuration Items:

1. **Database Settings** (keep default for local development):
   ```
   DATABASE_URL=sqlite:///instance/app_database.sqlite
   ```

2. **AWS Credentials** (required for AI features):
   ```
   AWS_ACCESS_KEY_ID=your_access_key_here
   AWS_SECRET_ACCESS_KEY=your_secret_key_here
   AWS_REGION=us-east-1
   ```

3. **Mail Settings** (optional for development):
   ```
   MAIL_SUPPRESS_SEND=True  # Set to True for development
   ```

---

## 6. Download Required TTS Models

```bash
# Create TTS models directory
mkdir -p tts_models
cd tts_models

# Download default voice model
wget https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/hfc_male/medium/en_US-hfc_male-medium.onnx
wget https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/hfc_male/medium/en_US-hfc_male-medium.onnx.json

# For macOS, use curl:
# curl -L -o en_US-hfc_male-medium.onnx https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/hfc_male/medium/en_US-hfc_male-medium.onnx
# curl -L -o en_US-hfc_male-medium.onnx.json https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/hfc_male/medium/en_US-hfc_male-medium.onnx.json

# Verify downloads
ls -la *.onnx*

cd ..  # Return to BrainStormX directory
```

---

## 7. Initialize Application Database

```bash
# Ensure virtual environment is active
source venv/bin/activate

# Create instance directory
mkdir -p instance

# Initialize the database
python -c "
from app import create_app
app = create_app()
with app.app_context():
    from app.extensions import db
    db.create_all()
    print('Database initialized successfully')
"
```

---

## 8. Running the Application

### Development Mode (Recommended for Testing):

```bash
# Ensure virtual environment is active
source venv/bin/activate

# Run the application
python run.py

# Expected output:
# * Running on http://127.0.0.1:5001
# * Debug mode: on/off
```

**Access the application:**
- Open your web browser
- Navigate to: `http://localhost:5001`

### Production Mode (Using Gunicorn):

```bash
# Install gunicorn if not already installed
pip install gunicorn eventlet

# Run with gunicorn
gunicorn -c gunicorn.conf.py run:app

# Or with custom settings:
gunicorn --bind 127.0.0.1:5001 --worker-class eventlet --workers 1 run:app
```

---

## 9. Verification and Testing

### Test Core Application:
```bash
# Test application startup
curl http://localhost:5001

# Should return HTML content or redirect
```

### Test TTS (Piper):
```bash
# Test Piper directly
echo "Hello, this is a test." | ./venv/bin/piper -m ./tts_models/en_US-hfc_male-medium.onnx -f /tmp/test.wav

# Check if audio file was created
ls -la /tmp/test.wav

# Play audio (if available)
# aplay /tmp/test.wav  # Linux
# afplay /tmp/test.wav  # macOS
```

### Test STT (Vosk):
```bash
# Test Vosk model loading
python -c "
import vosk
import os
model_path = './stt_models/vosk-model-en-us-0.22-lgraph'  # or your model path
if os.path.exists(model_path):
    model = vosk.Model(model_path)
    print('Vosk model loaded successfully')
else:
    print(f'Model not found at: {model_path}')
"
```

### Test AI Features (Optional):
```bash
# Test AWS Bedrock connection (requires valid credentials)
python -c "
import boto3
try:
    client = boto3.client('bedrock-runtime', region_name='us-east-1')
    print('AWS Bedrock client created successfully')
except Exception as e:
    print(f'AWS connection error: {e}')
"
```

---

## 10. Troubleshooting

### Common Issues and Solutions:

#### Database Lock Errors:
```bash
# Stop the application
pkill -f "python run.py"

# Check for hanging processes
ps aux | grep python

# If database is locked:
rm instance/app_database.sqlite.lock  # if exists
sqlite3 instance/app_database.sqlite "PRAGMA journal_mode=WAL;"

# Restart the application
python run.py
```

#### Piper TTS Errors:

1. **Binary not found:**
   ```bash
   # Check if piper binary exists
   ls -la venv/bin/piper
   
   # If missing, reinstall:
   # [Follow Piper installation steps again]
   ```

2. **Library loading errors:**
   ```bash
   # Check required libraries
   ldd venv/bin/piper
   
   # Update library cache
   sudo ldconfig
   
   # Install missing libraries
   sudo apt install libespeak-ng1  # Linux
   brew install espeak-ng          # macOS
   ```

3. **Model not found:**
   ```bash
   # Verify model file exists
   ls -la tts_models/en_US-hfc_male-medium.onnx*
   
   # Check .env configuration
   grep PIPER_MODEL .env
   ```

#### Vosk STT Errors:

1. **Model loading failures:**
   ```bash
   # Check model directory structure
   find stt_models/ -type f -name "*.conf"
   
   # Verify model path in .env
   grep VOSK_MODEL_PATH .env
   
   # Test model manually
   python -c "import vosk; print(vosk.Model('./stt_models/vosk-model-small-en-us-0.15'))"
   ```

2. **Memory errors with large models:**
   ```bash
   # Use smaller model instead
   # Download vosk-model-small-en-us-0.15 instead of large model
   # Update .env to point to small model
   ```

3. **Audio input issues:**
   ```bash
   # Check microphone permissions (browser)
   # Ensure HTTPS is used for microphone access in production
   ```

#### Permission Errors:
```bash
# Fix instance directory permissions
chmod -R 755 instance/
chmod -R 644 instance/*.sqlite

# Fix virtual environment permissions
chmod +x venv/bin/*
```

#### Import Errors:
```bash
# Verify virtual environment is active
which python
# Should show path to venv/bin/python

# Reinstall requirements if needed
pip install -r requirements.txt --force-reinstall
```

### Log Locations and Debugging:

```bash
# Application logs
tail -f instance/logs/app.log

# Flask development server output
# (shown in terminal when running python run.py)

# Check Python path issues
python -c "import sys; print('\n'.join(sys.path))"

# Check installed packages
pip list | grep -E "(flask|socketio|vosk|boto3)"
```

---

## 11. Security and Best Practices

### Development Security:
- Keep `.env` file secure and never commit to version control
- Use environment-specific configuration files
- Regularly update dependencies: `pip install -r requirements.txt --upgrade`

### Production Considerations:
- Use proper database (PostgreSQL/MySQL instead of SQLite)
- Configure reverse proxy (Nginx)
- Set up SSL certificates
- Use environment variables for sensitive data
- Configure proper logging and monitoring

---

## 12. Backup and Maintenance

### Backup Important Data:
```bash
# Backup database
cp instance/app_database.sqlite backups/database_$(date +%Y%m%d).sqlite

# Backup uploads
tar -czf backups/uploads_$(date +%Y%m%d).tar.gz instance/uploads/

# Backup configuration
cp .env backups/env_$(date +%Y%m%d)
```

### Regular Maintenance:
```bash
# Update system packages (monthly)
sudo apt update && sudo apt upgrade  # Linux
brew update && brew upgrade          # macOS

# Update Python packages (monthly)
pip install -r requirements.txt --upgrade

# Clean up old log files (weekly)
find instance/logs/ -name "*.log" -mtime +30 -delete
```

---

## 13. Performance Optimization

### For Better Performance:
1. **Use SSD storage** for better I/O performance
2. **Allocate sufficient RAM** (minimum 4GB, recommended 8GB+)
3. **Use production WSGI server** (Gunicorn) instead of development server
4. **Configure caching** for static assets
5. **Use smaller Vosk model** if transcription accuracy requirements are flexible

### Resource Usage Guidelines:
- **Small Vosk model**: ~500MB RAM
- **Large Vosk model**: ~2GB RAM  
- **Piper TTS**: ~100MB RAM
- **Base application**: ~200MB RAM
- **Total recommended**: 4GB+ RAM for comfortable operation

---

**Publisher:** BroadComms (https://www.broadcomms.net)  
**Support Contact:** patrick@broadcomms.net  
**Last Updated:** October 17, 2025