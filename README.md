# BrainStormX 1.0

BrainStormX is an AI-embedded collaborative brainstorming platform built with Flask (Python) and JavaScript, leveraging AWS Bedrock (Nova) foundation models. The system enables workshop organizers to create, manage, and execute structured brainstorming sessions with real-time collaboration, AI assistance, and comprehensive reporting.

---

## 🎯 Key Features

1. **Participant & Document Management** – Invite collaborators, assign roles, and centralize workshop artifacts.
2. **Digital Sticky Notes** – Capture brainstorming inputs as color-coded virtual notes that remain linked to session context.
3. **Problem/Solution Enhancement** – Refine raw ideas into crisp problem statements and solution summaries with Nova models.
4. **Automated Workshop Planning** – Generate phase-aware agendas, task sequences, rules, icebreakers, tips, and nudges on demand.
5. **Engagement Monitoring** – Detect inactivity and proactively nudge quiet participants back into the conversation.
6. **Clustering & Voting Workflows** – Group ideas, launch timed dot-voting rounds, and prioritize outcomes interactively.
7. **Feasibility Analysis** – Evaluate proposals against feasibility heuristics using Bedrock AgentCore Memory context.
8. **Market Trend Forecasting** – Surface trend outlooks and external signals to inform decision making.
9. **Session Intelligence** – Produce workshop minutes, transcripts, highlights, and action-item trackers automatically.

---

## 🏗️ Architecture Overview

BrainStormX blends real-time collaboration, AI orchestration, and telemetry-driven insights.

- **Web Application Layer (Flask + Socket.IO)**
  - Flask serves REST APIs, admin views, and Jinja templates.
  - Socket.IO powers real-time collaboration, video conference signaling, transcription updates, and live voting events.
- **AI & Automation Layer (AWS Bedrock + LangChain/LangGraph)**
  - Nova models (Lite/Pro) craft agendas, summaries, and engagement nudges.
  - Bedrock AgentCore Memory persists long-lived context for participants and workshops.
  - LangChain/LangGraph orchestrates multi-tool prompts, JSON-validated agent flows, and fallbacks.
- **Data Layer**
  - SQLAlchemy manages persistence. SQLite is bundled for lightweight deployments; PostgreSQL is supported for scale.
  - Document and media assets are stored under `instance/` directories with secure serve endpoints.
- **Background Services**
  - Transcription providers (Vosk local / AWS Transcribe) and Piper TTS deliver speech experiences.
  - Prometheus-ready metrics surface tool execution timing, engagement signals, and system health.
- **Deployment Footprint**
  - Ships as a single-container image for Docker/Fargate and as a traditional Flask stack for EC2 or on-prem installs.

A high-level module map lives in the root `README.md` under **Architecture → Structure** for deeper reference.

---

## 📦 Release Contents

| Path                                    | Description                                                                         |
| --------------------------------------- | ----------------------------------------------------------------------------------- |
| `release/README.md`                   | This release guide.                                                                 |
| `requirements.txt`                    | Python dependency lock for virtualenv installs.                                     |
| `Dockerfile` & `docker-compose.yml` | Container build assets and local monitoring snapshot.                               |
| `LOCAL-DEPLOYMENT-VENV.md`            | Detailed virtualenv setup (Linux/macOS).                                            |
| `DOCKER-DEPLOYMENT.md`                | Extended Docker build and troubleshooting notes.                                    |
| `EC2-DEPLOYMENT-WITH-PUBLIC-DNS.md`   | Production-grade EC2 deployment walkthrough.                                        |
| `ssl/`                                | Sample self-signed certs for HTTPS development.                                     |
| `instance/`                           | SQLite DB seed, uploads/log directories (create writable copies before production). |

> 💡 **Packaging Tip:** Create an archive for distribution with `tar -czvf BrainStormX-<version>.tar.gz release/ Dockerfile docker-compose.yml requirements.txt app/ instance/ssl_run.py`. Include only sanitized `.env` templates (never real secrets).

---

## 🚀 Installation & Deployment

Each section below assumes a clean checkout or release archive and references supporting docs where available.

### 1. Downloading the Release

**Option A – Git Clone (recommended for contributors):**

```bash
# Clone the staging or production branch
git clone -b staging https://github.com/broadcomms/brainstorm_x.git
cd brainstorm_x
```

**Option B – Download Release Archive:**

1. Navigate to the published GitHub Release.
2. Download `BrainStormX-<version>.tar.gz` (or `.zip`).
3. Extract it locally:
   ```bash
   tar -xzvf BrainStormX-<version>.tar.gz
   cd brainstorm_x
   ```

Verify critical files (`requirements.txt`, `Dockerfile`, `release/README.md`) are present before continuing.

### 2. Set Up a Python Virtual Environment (Local/Linux/macOS)

```bash
# Ensure Python 3.10+ is available
python3 --version

# From the project root
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Additional setup for speech features (optional but recommended):

- Install Piper TTS binary and models (`LOCAL-DEPLOYMENT-VENV.md` §4a).
- Download the Vosk STT model (`LOCAL-DEPLOYMENT-VENV.md` §4b) and update `.env` paths.

### 3. Run the Application Locally with Flask

```bash
# Copy environment template and adjust credentials
cp .env.developer .env
nano .env  # set SECRET_KEY, AWS credentials if needed

# Activate the environment and start Flask
source venv/bin/activate
python run.py
```

Access the app at [http://localhost:5001](http://localhost:5001). Use seeded demo accounts or create new ones via the admin interface.

### 4. Build & Run as a Docker Container

```bash
# Build image
docker build -t brainstormx:latest .

# Run container with persistent instance storage
mkdir -p instance/uploads instance/logs

docker run -d --name brainstormx \
  --env-file .env.docker \
  -p 5001:5001 \
  -v $(pwd)/instance:/app/instance \
  brainstormx:latest
```

Optional enhancements:

- Expose TURN/STUN ports for WebRTC (`docker run ... -p 3478:3478/udp -p 5349:5349/tcp`).
- Mount `ssl/` and launch with `python ssl_run.py` for HTTPS testing (see **DOCKER-DEPLOYMENT.md** §8).

### 5. Deploy Container from Docker Registry

```bash
# Tag for registry (Docker Hub example)
docker tag brainstormx:latest broadcomms/brainstorm_x:<version>

# Authenticate and push
docker login
docker push broadcomms/brainstorm_x:<version>
```

Consumers can then pull and start the image:

```bash
docker pull broadcomms/brainstorm_x:<version>
docker run -d --name brainstormx \
  --env-file .env.docker \
  -p 5001:5001 \
  broadcomms/brainstorm_x:<version>
```

### 6. Deploy to **Amazon Elastic Container Service (ECS)**

> This guide targets AWS Fargate for a fully managed, serverless container runtime. Swap to EC2 launch type if you need GPU or custom AMIs.

1. **Package & Push Image to Amazon ECR**

   ```bash
   aws ecr create-repository --repository-name brainstormx --region us-east-1
   aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <account-id>.dkr.ecr.us-east-1.amazonaws.com
   docker tag brainstormx:latest <account-id>.dkr.ecr.us-east-1.amazonaws.com/brainstormx:<version>
   docker push <account-id>.dkr.ecr.us-east-1.amazonaws.com/brainstormx:<version>
   ```
2. **Create ECS Cluster (Fargate)**

   ```bash
   aws ecs create-cluster --cluster-name brainstormx-prod --region us-east-1
   ```
3. **Define Task Execution Role**

   - Attach `AmazonECSTaskExecutionRolePolicy` and permissions for Secrets Manager / CloudWatch Logs.
   - Store secrets (AWS keys, mail creds) in AWS Secrets Manager or SSM Parameter Store.
4. **Register Task Definition**

   - CPU: `1024` (1 vCPU), Memory: `2048` (2 GiB) or higher.
   - Container definition: image URI from ECR, port mapping `5001:5001`, log driver `awslogs`.
   - Mount ephemeral storage or use EFS for persistent `instance/` data if required.
5. **Create an Application Load Balancer (optional but recommended)**

   - Listener on HTTPS (443) forwarding to target group on port 5001.
   - Upload SSL cert via AWS Certificate Manager.
6. **Run ECS Service**

   ```bash
   aws ecs create-service \
     --cluster brainstormx-prod \
     --service-name brainstormx-web \
     --task-definition brainstormx-task:<revision> \
     --desired-count 1 \
     --launch-type FARGATE \
     --network-configuration "awsvpcConfiguration={subnets=[subnet-abc,subnet-def],securityGroups=[sg-123],assignPublicIp=ENABLED}" \
     --load-balancers "targetGroupArn=arn:aws:elasticloadbalancing:...,containerName=brainstormx,containerPort=5001"
   ```
7. **Post-Deployment Checks**

   - Confirm service stability in the ECS console.
   - Tail logs with `aws logs tail /ecs/brainstormx --follow`.
   - Update DNS (Route 53 / Cloudflare) to the ALB endpoint.

For deeper automation, codify the above in AWS Copilot, CDK, or Terraform.

### 7. Deploy to **Amazon EC2 (Public DNS + SSL)**

Follow the hardened playbook in `EC2-DEPLOYMENT-WITH-PUBLIC-DNS.md`. Key highlights:

1. **Provision Infrastructure** – Ubuntu 24.04 LTS on `t3.small`, 32 GiB gp3 disk, security groups for ports 22/80/443.
2. **Bootstrap Server** – SSH in, install system dependencies, create `brainstormx` user, and clone the repo or upload the release archive.
3. **Configure Python Environment** – Create virtualenv, install `requirements.txt`, plus Piper/Vosk models as needed.
4. **Supply Production `.env`** – Copy `.env.server`, rotate secrets, and set mail/AWS credentials. Restrict permissions to `600`.
5. **Set Up Gunicorn + Systemd** – Use the provided `gunicorn.conf.py` and systemd service to keep the app running.
6. **Configure Nginx + SSL** – Terminate HTTPS with Nginx, proxy to Gunicorn on `127.0.0.1:5001`, and apply Certbot (or bundle self-signed certs for EC2 default domains).
7. **Validate** – Ensure `sudo systemctl status brainstormx` is `active`, check logs in `/home/brainstormx/brainstorm_x/instance/logs/`, and test user flows over HTTPS.

---

## 🔐 Configuration Checklist

Before going live, confirm the following:

- Secrets loaded from `.env` or AWS Secrets Manager (no plaintext credentials in source control).
- Database persistence strategy chosen (SQLite for pilot, PostgreSQL for production).
- S3 or equivalent storage configured if long-term media retention is required.
- Monitoring integrated (Prometheus scrape or CloudWatch metrics/alarms).
- Backup schedule for `instance/` directory (or managed storage snapshots).

---

## ✅ Verification Steps

1. Run unit tests locally: `pytest -q` (ensure dependencies like Redis/Message brokers are stubbed or disabled if not needed).
2. Check health endpoint: `curl http://localhost:5001/health` (implement as needed).
3. Validate AI features with sample prompts from `workshop/` workflows.
4. Inspect Socket.IO events in browser dev tools to verify real-time capabilities.

---

## 📄 License & Support

- **License:** See root `LICENSE` (Apache 2.0).
- **Commercial Inquiries & Support:** patrick@broadcomms.net
- **Status Page & Updates:** Follow release notes in `SUBMISSION.md` or GitHub Releases tab.

For feedback or contributions, please open an issue or submit a pull request on GitHub. Happy brainstorming!
