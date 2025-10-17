# app/config.py
import os
from dotenv import load_dotenv



# --- ADD TASK SEQUENCE HERE ---
TASK_SEQUENCE = [
    "framing",
    "warm-up",
    "brainstorming",
    "clustering_voting",
    "results_feasibility",
    "results_prioritization",
    "discussion",
    "results_action_plan",
    "summary",
]

load_dotenv()  # Load environment variables from .env file
# -----------------------------


class Config:
    # General environmental details
    APP_NAME = os.environ.get("APP_NAME", "BrainStormX")
    SECRET_KEY = os.environ.get("SECRET_KEY", "change_me_in_env")
    # Database
    # Prefer env var, else default to absolute path under ./instance/app_database.sqlite
    _BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    _DEFAULT_SQLITE_PATH = os.path.join(_BASE_DIR, "instance", "app_database.sqlite")
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URI",
        f"sqlite:///{_DEFAULT_SQLITE_PATH}?timeout=20&check_same_thread=False"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # AWS Bedrock (Nova) Configuration
    AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
    AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY")
    AWS_SESSION_TOKEN = os.environ.get("AWS_SESSION_TOKEN")  # optional
    # Bedrock model ID for text/chat generation (Default: Nova Lite)
    BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "amazon.nova-lite-v1:0")
    # Bedrock model ID for pro-level text/chat generation (Nova Pro)
    BEDROCK_NOVA_PRO = os.environ.get("BEDROCK_NOVA_PRO", "amazon.nova-pro-v1:0")
    # Image generation/editing model (Bedrock) for headshot enhancement
    BEDROCK_IMAGE_MODEL_ID = os.environ.get("BEDROCK_IMAGE_MODEL_ID", "amazon.titan-image-generator-v1")
    # You can override with any Bedrock model ID via env, e.g. "amazon.nova-pro-v1" or
    # Anthropic/Meta/Mistral models available in your account.
    BEDROCK_CLAUDE_SONNET = os.environ.get("BEDROCK_CLAUDE_SONNET", "us.anthropic.claude-3-7-sonnet-20250219-v1:0")
    BEDROCK_CLAUDE_OPUS = os.environ.get("BEDROCK_CLAUDE_OPUS", "us.anthropic.claude-4-1-opus-20240917-v1:0")
    # Bedrock retry controls (throttling resilience)
    try:
        BEDROCK_RETRY_MAX_ATTEMPTS = max(1, int(os.environ.get("BEDROCK_RETRY_MAX_ATTEMPTS", "6")))
    except ValueError:
        BEDROCK_RETRY_MAX_ATTEMPTS = 6
    try:
        BEDROCK_RETRY_BASE_DELAY_SECONDS = max(0.05, float(os.environ.get("BEDROCK_RETRY_BASE_DELAY_SECONDS", "0.5")))
    except ValueError:
        BEDROCK_RETRY_BASE_DELAY_SECONDS = 0.5
    try:
        BEDROCK_RETRY_MAX_DELAY_SECONDS = max(
            BEDROCK_RETRY_BASE_DELAY_SECONDS,
            float(os.environ.get("BEDROCK_RETRY_MAX_DELAY_SECONDS", "12.0")),
        )
    except ValueError:
        BEDROCK_RETRY_MAX_DELAY_SECONDS = max(12.0, BEDROCK_RETRY_BASE_DELAY_SECONDS)
    try:
        BEDROCK_RETRY_JITTER_FACTOR = max(
            0.0,
            min(1.0, float(os.environ.get("BEDROCK_RETRY_JITTER_FACTOR", "0.5"))),
        )
    except ValueError:
        BEDROCK_RETRY_JITTER_FACTOR = 0.5
    try:
        BEDROCK_BOTO_MAX_ATTEMPTS = max(
            2,
            int(
                os.environ.get(
                    "BEDROCK_BOTO_MAX_ATTEMPTS",
                    str(max(2, BEDROCK_RETRY_MAX_ATTEMPTS)),
                )
            ),
        )
    except ValueError:
        BEDROCK_BOTO_MAX_ATTEMPTS = max(2, BEDROCK_RETRY_MAX_ATTEMPTS)

    # Time awareness defaults
    DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "America/Toronto")
    try:
        WORKSHOP_DEFAULT_DURATION_MINUTES = max(1, int(os.environ.get("WORKSHOP_DEFAULT_DURATION_MINUTES", "90")))
    except ValueError:
        WORKSHOP_DEFAULT_DURATION_MINUTES = 90
    try:
        PHASE_DEFAULT_DURATION_MINUTES = max(1, int(os.environ.get("PHASE_DEFAULT_DURATION_MINUTES", "15")))
    except ValueError:
        PHASE_DEFAULT_DURATION_MINUTES = 15
    try:
        HEARTBEAT_INTERVAL_SECONDS = max(10, int(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "60")))
    except ValueError:
        HEARTBEAT_INTERVAL_SECONDS = 60
    try:
        IDLE_THRESHOLD_MINUTES = max(1, int(os.environ.get("IDLE_THRESHOLD_MINUTES", "10")))
    except ValueError:
        IDLE_THRESHOLD_MINUTES = 10

    # AgentCore Memory configuration
    AGENTCORE_MEMORY_ENABLED = os.environ.get("AGENTCORE_MEMORY_ENABLED", "false").lower() == "true"
    AGENTCORE_MEMORY_ID = os.environ.get("AGENTCORE_MEMORY_ID")
    AGENTCORE_MEMORY_ARN = os.environ.get("AGENTCORE_MEMORY_ARN")
    AGENTCORE_MEMORY_REGION = os.environ.get("AGENTCORE_MEMORY_REGION") or AWS_REGION
    try:
        AGENTCORE_MEMORY_TOP_K = max(1, int(os.environ.get("AGENTCORE_MEMORY_TOP_K", "3")))
    except ValueError:
        AGENTCORE_MEMORY_TOP_K = 3
    try:
        AGENTCORE_MEMORY_TIMEOUT_SECONDS = max(0.2, float(os.environ.get("AGENTCORE_MEMORY_TIMEOUT_SECONDS", "4.0")))
    except ValueError:
        AGENTCORE_MEMORY_TIMEOUT_SECONDS = 4.0
    AGENTCORE_MEMORY_NAMESPACE_TEMPLATES = os.environ.get("AGENTCORE_MEMORY_NAMESPACE_TEMPLATES", "")
    AGENTCORE_MEMORY_STORE_BACKGROUND = os.environ.get("AGENTCORE_MEMORY_STORE_BACKGROUND", "true").lower() == "true"
    AGENTCORE_MEMORY_DEBUG_LOG = os.environ.get("AGENTCORE_MEMORY_DEBUG_LOG", "false").lower() == "true"

    # Brainstorming AI seed idea controls
    BRAINSTORMING_AI_IDEAS_ENABLED = os.environ.get("BRAINSTORMING_AI_IDEAS_ENABLED", "true").lower() == "true"
    try:
        BRAINSTORMING_AI_IDEAS_MAX_DEFAULT = max(0, int(os.environ.get("BRAINSTORMING_AI_IDEAS_MAX_DEFAULT", "3")))
    except ValueError:
        BRAINSTORMING_AI_IDEAS_MAX_DEFAULT = 3
    try:
        cap_raw = os.environ.get("BRAINSTORMING_AI_IDEAS_MAX_ABSOLUTE", "6")
        BRAINSTORMING_AI_IDEAS_MAX_ABSOLUTE = max(BRAINSTORMING_AI_IDEAS_MAX_DEFAULT, int(cap_raw))
    except ValueError:
        BRAINSTORMING_AI_IDEAS_MAX_ABSOLUTE = max(6, BRAINSTORMING_AI_IDEAS_MAX_DEFAULT)
    try:
        BRAINSTORMING_PREWORK_CHAR_LIMIT = max(1200, int(os.environ.get("BRAINSTORMING_PREWORK_CHAR_LIMIT", "4800")))
    except ValueError:
        BRAINSTORMING_PREWORK_CHAR_LIMIT = 4800

    # Flask-Mail config
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "server108.web-hosting.com")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", "587"))  # 465=SSL, 587=TLS typically
    MAIL_USE_TLS = os.environ.get('MAIL_USE_TLS', 'True').lower() == 'true'
    MAIL_USE_SSL = os.environ.get('MAIL_USE_SSL', 'False').lower() == 'true'
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "no-reply@broadcomms.net")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD")  # must be provided in .env
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "no-reply@broadcomms.net")
    MAIL_SUPPRESS_SEND = os.environ.get('MAIL_SUPPRESS_SEND', 'False').lower() == 'true'  # for local dev
    
    # Flask-Mail SSL configuration for certificate issues
    # Use relaxed SSL context to work around shared hosting certificate problems
    MAIL_USE_RELAXED_SSL = os.environ.get('MAIL_USE_RELAXED_SSL', 'True').lower() == 'true'
    
    # Set to a number (string) of seconds to override task duration globally, or None/empty to disable.
    # NOTE: Keep disabled to honor DB/registry durations end-to-end.
    DEBUG_OVERRIDE_TASK_DURATION = None

    # In development/testing, require LLM for AI-driven phases and do not fallback silently.
    # Can be overridden via env: AI_STRICT_PHASES=true|false
    AI_STRICT_PHASES = (
        os.environ.get('AI_STRICT_PHASES')
        or (
            'true' if (
                os.environ.get('FLASK_ENV') in ('development', 'dev')
                or os.environ.get('DEBUG') in ('1', 'true', 'True')
                or os.environ.get('PYTEST_CURRENT_TEST')
            ) else 'false'
        )
    ).lower() == 'true'

    # Tool Gateway resiliency controls
    try:
        TOOL_GATEWAY_TIMEOUT_SECONDS = max(0.1, float(os.environ.get("TOOL_TIMEOUT_SECONDS", "12")))
    except ValueError:
        TOOL_GATEWAY_TIMEOUT_SECONDS = 20.0
    try:
        TOOL_GATEWAY_MAX_WORKERS = max(1, int(os.environ.get("TOOL_MAX_WORKERS", "4")))
    except ValueError:
        TOOL_GATEWAY_MAX_WORKERS = 4
    try:
        TOOL_GATEWAY_FAILURE_THRESHOLD = max(1, int(os.environ.get("CIRCUIT_BREAKER_THRESHOLD", "3")))
    except ValueError:
        TOOL_GATEWAY_FAILURE_THRESHOLD = 3
    try:
        TOOL_GATEWAY_CIRCUIT_RESET_SECONDS = max(5.0, float(os.environ.get("CIRCUIT_BREAKER_RESET_SECONDS", "60")))
    except ValueError:
        TOOL_GATEWAY_CIRCUIT_RESET_SECONDS = 60.0

    # Optional: allow longer timeouts for control-plane tools (begin/next/end)
    try:
        TOOL_GATEWAY_CONTROL_TIMEOUT_SECONDS = max(0.1, float(os.environ.get("TOOL_CONTROL_TIMEOUT_SECONDS", str(TOOL_GATEWAY_TIMEOUT_SECONDS))))
    except ValueError:
        TOOL_GATEWAY_CONTROL_TIMEOUT_SECONDS = TOOL_GATEWAY_TIMEOUT_SECONDS

    # Media and uploads
    # All profile photos must be stored under instance/uploads/photos
    INSTANCE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "instance"))
    MEDIA_BASE_DIR = os.path.join(INSTANCE_DIR, "uploads")
    MEDIA_PHOTOS_DIR = os.path.join(MEDIA_BASE_DIR, "photos")
    # URL prefix used to serve instance-hosted photos via Flask route
    MEDIA_PHOTOS_URL_PREFIX = "/media/photos"
    # Reports (generated PDFs) live under instance/uploads/reports and are served via Flask route
    MEDIA_REPORTS_DIR = os.path.join(MEDIA_BASE_DIR, "reports")
    MEDIA_REPORTS_URL_PREFIX = "/media/reports"
    # Upload constraints (soft)
    MAX_UPLOAD_BYTES = int(os.environ.get("MAX_UPLOAD_BYTES", str(5 * 1024 * 1024)))  # 5 MB default
    
    # Assistant Threads feature flag (Phase 1 server-side)
    ASSISTANT_THREADS_ENABLED: bool = os.environ.get("ASSISTANT_THREADS_ENABLED", "true").lower() not in {"0", "false"}

    # Strict JSON-only outputs from Assistant (no heuristic fallbacks)
    ASSISTANT_STRICT_JSON: bool = os.environ.get("ASSISTANT_STRICT_JSON", "true").lower() not in {"0", "false"}

    # When enabled, the backend will not inject any proactive UI hints (e.g., "Open Feasibility Report" button).
    # All UI hints must come from the LLM response itself.
    ASSISTANT_UI_STRICT_LLM_ONLY: bool = os.environ.get("ASSISTANT_UI_STRICT_LLM_ONLY", "false").lower() in {"1", "true", "yes"}

