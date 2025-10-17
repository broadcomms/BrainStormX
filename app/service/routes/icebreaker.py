# app/service/routes/icebreaker.py
import re
import json
from flask import jsonify
from flask_login import login_required
from app.utils.llm_bedrock import get_chat_llm
from langchain_core.prompts import PromptTemplate
from app.config import Config
# Import the blueprint and the helper function from agent.py
from .agent import agent_bp
from app.utils.data_aggregation import get_pre_workshop_context_json
import markdown # If you plan to return HTML directly later
from app.utils.json_utils import extract_json_block

# #-----------------------------------------------------------
# # 2.c Generate icebreaker activities

def generate_icebreaker_text(workshop_id):
    """Generates only the icebreaker text using the LLM."""
    pre_workshop_data = get_pre_workshop_context_json(workshop_id)
    if not pre_workshop_data:
        return "Could not generate icebreaker: Workshop data unavailable."
    icebreaker_prompt_template = """
    You are a workshop assistant. Your task is to create a fun and engaging icebreaker question for the workshop.
    Based on the workshop context provided below, generate a fun, engaging, and very short icebreaker question (under 25 words).
    The icebreaker should be relevant to the workshop's title or objective.

    Workshop Context:
    {pre_workshop_data}

    Instructions:
    - Generate ONE icebreaker question.
    - Keep it short and brief under 25 words.
    - Ensure it relates to the workshop context (based on the Title and Objective).
    
    Format:
    Output MUST be valid JSON with the keys:
    - icebreaker: The icebreaker question.

    Response:
    """
    bedrock_llm = get_chat_llm(
        model_kwargs={
            "temperature": 0.9,
            "max_tokens": 200,
        }
    )
    icebreaker_prompt = PromptTemplate.from_template(icebreaker_prompt_template)
    chain = icebreaker_prompt | bedrock_llm
    raw = chain.invoke({"pre_workshop_data": pre_workshop_data})

    # Normalize response to plain text
    text = raw
    try:
        if hasattr(raw, "content"):
            text = raw.content
        elif isinstance(raw, dict) and "content" in raw:
            text = raw["content"]
    except Exception:
        pass
    if not isinstance(text, str):
        text = str(text)

    print(f"[DEBUG] Workshop raw LLM icebreaker output: {workshop_id}: {text}")  # DEBUG

    # Prefer robust fenced JSON extraction
    json_blob = extract_json_block(text)
    if not json_blob:
        # Fallback: try to find first JSON object in free text
        m2 = re.search(r"(\{[\s\S]*?\})", text, re.DOTALL)
        json_blob = m2.group(1) if m2 else ""

    if json_blob:
        try:
            parsed = json.loads(json_blob)
            return (parsed.get("icebreaker", "") or "").strip()
        except json.JSONDecodeError:
            # Continue to non-JSON fallback below
            pass

    # Non-JSON fallback: try to pull value from a key-like pattern
    m_kv = re.search(r"\"icebreaker\"\s*:\s*\"([\s\S]*?)\"", text, re.DOTALL)
    if m_kv:
        return m_kv.group(1).strip()

    # Strip markdown code fences if present and return the remaining line
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?|```$", "", cleaned).strip()
    return cleaned

@agent_bp.route("/generate_icebreaker/<int:workshop_id>", methods=["POST"])
@login_required
def generate_icebreaker(workshop_id):
    """API endpoint to generate and return an icebreaker."""
    icebreaker_text = generate_icebreaker_text(workshop_id)
    if "Could not generate icebreaker" in icebreaker_text:
        return jsonify({"error": icebreaker_text}), 404
    return jsonify({"icebreaker": icebreaker_text}), 200
