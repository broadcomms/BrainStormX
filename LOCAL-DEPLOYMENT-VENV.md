# BrainStormX Local Linux Deployment Guide (Python venv)

## Overview

This guide provides step-by-step instructions to deploy and run BrainStormX on a Linux/Unix server using Python virtual environments. It covers environment setup, dependencies, configuration, running the app, and troubleshooting.

---

## Prerequisites

- Ubuntu 20.04+ (or any modern Linux/macOS)
- Python 3.10+
- Git
- Required system packages (see below)

---

## 1. Install System Dependencies (Omit for macOS)

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-pip python3-venv git sqlite3 tesseract-ocr libtesseract-dev ffmpeg build-essential libssl-dev libffi-dev python3-dev 
sudo apt install libespeak-ng1
```

---

## 2. Clone the Repository

```bash
git clone -b production https://github.com/broadcomms/brainstorm_x.git
cd brainstorm_x
```

---

## 3. Create and Activate Python Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
```

---

## 4. Install Python Dependencies

```bash
pip install --upgrade pip
pip install -r requirements.txt
```

---

## 4a. Install Piper TTS Engine

Piper is an open-source text-to-speech engine for the AI voice features.
If you plan to use AI Voice, follow these steps to install Piper TTS engine locally:

```bash
# For Linux:
# Download latest Piper release (see https://github.com/rhasspy/piper/releases) 
# Using wget:
wget https://github.com/rhasspy/piper/releases/latest/download/piper_linux_x86_64.tar.gz
# Or using curl:
# curl -L -o piper_linux_x86_64.tar.gz https://github.com/rhasspy/piper/releases/latest/download/piper_linux_x86_64.tar.gz

# Extract and install the binary
tar -xzf piper_linux_x86_64.tar.gz

# Copy binary and libraries 
cp piper/piper venv/bin
chmod +x venv/bin/piper

# Copy all required libraries
cp piper/lib*.so* venv/lib/
sudo cp piper/lib*.so* /usr/local/lib/

# Copy espeak-ng data and libraries
sudo mkdir -p /usr/share/espeak-ng-data
sudo cp -r piper/espeak-ng-data/* /usr/share/espeak-ng-data/
sudo cp piper/libespeak-ng.so* /usr/local/lib/

# Update library cache
sudo ldconfig

# Verify piper installation
./venv/bin/piper --version

# (Optional) Download voice models as needed
# See https://github.com/rhasspy/piper#voice-models for available voices
# Default voice model is already configured in application

# Clean up the compressed binary
rm piper_linux_x86_64.tar.gz
```


```bash
# For macOS:
# Install espeak-ng via Homebrew (required dependency)
brew install espeak-ng

# Install Piper TTS via Python package (recommended for macOS)
pip install piper-tts

# Verify installation
python -c "import piper; print('Piper TTS installed successfully')"

# Test Piper with existing voice model
echo "Hello, this is a test." | piper -m ./tts_models/en_US-hfc_male-medium.onnx -f /tmp/test.wav

# Check if piper binary exists in venv/bin
which piper

# Test piper command
piper --version

# Test with Existing Models
echo "Hello, this is a test." | piper -m ./tts_models/en_US-hfc_male-medium.onnx -f /tmp/test.wav

```

- Ensure `venv/bin/piper` exists and is executable (`chmod +x venv/bin/piper`).
- Update your configuration if you use a different location for the Piper binary.


---

## 4b. Install Vosk Speech Recognition (STT) Model

Vosk is used for offline speech-to-text transcription. To use transcription features, download a Vosk model:

```bash
# Create models directory if not already
mkdir -p stt_models

# Download Vosk English model for lighter deployment (lightweight version ~50MB)
# cd stt_models
# For Linux: wget https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
# For macOS: curl -L -o vosk-model-small-en-us-0.15.zip https://alphacephei.com/vosk/models/vosk-model-small-en-us-0.15.zip
# unzip vosk-model-small-en-us-0.15.zip
# rm vosk-model-small-en-us-0.15.zip

# For better accuracy, download the larger model (~1.8GB)
cd stt_models

# For Linux:
# wget https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip

# For macOS (use curl instead of wget):
curl -L -o vosk-model-en-us-0.22-lgraph.zip https://alphacephei.com/vosk/models/vosk-model-en-us-0.22-lgraph.zip

unzip vosk-model-en-us-0.22-lgraph.zip
rm vosk-model-en-us-0.22-lgraph.zip

cd ..

# Verify model directory
ls -la stt_models/

# Verify installation by trying to import vosk
python -c "import vosk; print('Vosk installed successfully')"


```

Update your `.env` file to point to the right model, for light container smaller model is recommended:

```env
TRANSCRIPTION_PROVIDER=vosk
VOSK_MODEL_PATH=/Users/patricken/brainstorm_x_dev/stt_models/vosk-model-en-us-0.22-lgraph
```

---

## 5. Configure Environment Variables

- Copy `.env.developer` to `.env` and edit as needed:

```bash
cp .env.developer .env
nano .env
```

- Set `SECRET_KEY`, database path, mail settings, AWS credentials, etc.
- **For macOS**: Update the following paths to match your local environment:
  - `PIPER_BIN=/Users/yourusername/brainstorm_x/venv/bin/piper`
  - `PIPER_MODEL=/Users/yourusername/brainstorm_x/tts_models/en_US-hfc_male-medium.onnx`
  - `VOSK_MODEL_PATH=/Users/yourusername/brainstorm_x/stt_models/vosk-model-en-us-0.22-lgraph`

---

## 6. Running the Application

### Development Mode

```bash
python run.py
```

- Access at: http://localhost:5001

### Production Mode (Gunicorn)

```bash
gunicorn -c gunicorn.conf.py run:app
```

- For Socket.IO, ensure `worker_class = 'eventlet'` in `gunicorn.conf.py`.

---

## 9. Troubleshooting

### Common Issues

**Database Lock Errors:**
If you see `(sqlite3.OperationalError) database is locked`, try:

```bash
# Stop the application
pkill -f "python run.py"

# Check for hanging processes
ps aux | grep python

# Restart the application
python run.py
```

**Transcription Errors:**

- Verify Vosk model is downloaded and path is correct in `.env`
- Check microphone permissions in browser
- Ensure proper sample rate handling (fixed in latest version)

**TTS (Piper) Errors:**

- Verify all Piper libraries are installed: `./venv/bin/piper --version`
- Check `libespeak-ng` is installed: `dpkg -l | grep libespeak-ng`
- Ensure proper library paths: `sudo ldconfig`

### Log Locations

- Check logs in `instance/logs/`.
- Ensure `.env` is present and correct.
- For AI features, verify AWS credentials and Bedrock access.
- For file uploads, ensure `instance` directory is writable.

---

## 10. Backup & Restore

- Backup `instance/app_database.sqlite` and `instance/uploads/` regularly.
- Restore by copying files into the `instance` directory.

---

## 11. Security

- Keep `.env` and credentials secure.
- Regularly update system and Python packages.

---

## 12. Verification

- Access the app at `http://localhost:5001`.

---

**Publisher:** BroadComms (https://www.broadcomms.net)
**Deployment Support Contact:** patrick@broadcomms.net
**Last Updated:** October 12, 2025
