# app/service/routes/tip.py
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
from app.utils.json_utils import extract_json_block
import markdown # If you plan to return HTML directly later

# #-----------------------------------------------------------
# # 2.d Generate tips for participants

def generate_tip_text(workshop_id):
    """Generates only the tip text using the LLM."""
    pre_workshop_data = get_pre_workshop_context_json(workshop_id)
    if not pre_workshop_data:
        return "No pre‑workshop data found."
    
    tip_prompt_template = """
        You are an AI assistant providing helpful advice to the workshop participants.
        Based *only* on the workshop context provided below, generate ONE concise and actionable tip to help participants prepare for the workshop.
        The tip should be directly related to the workshop's objective or agenda.

        Workshop Context:
        {pre_workshop_data}

        Instructions:
        - Generate ONE tip.
        - Keep it short and brief.
        - Ensure it relates to the workshop context (based on the Title and Objective).
        
        Format:
        Output MUST be valid JSON with the key:
        - tip: The workshop tip.

        Response:
        """
    bedrock_llm = get_chat_llm(
        model_kwargs={
            "temperature": 0.9,
            "max_tokens": 200,
        }
    )
    tip_prompt = PromptTemplate.from_template(tip_prompt_template)
    chain = tip_prompt | bedrock_llm
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

    print(f"[Tip Service] Workshop raw LLM tip output: {workshop_id}: {text}")

    # Extract JSON from fenced code or fallback to first object
    json_blob = extract_json_block(text)
    if not json_blob:
        m2 = re.search(r"(\{[\s\S]*?\})", text, re.DOTALL)
        json_blob = m2.group(1) if m2 else ""
    if json_blob:
        try:
            parsed = json.loads(json_blob)
            return (parsed.get("tip", "") or "").strip()
        except json.JSONDecodeError:
            pass
    # Non-JSON fallback
    m_kv = re.search(r"\"tip\"\s*:\s*\"([\s\S]*?)\"", text, re.DOTALL)
    if m_kv:
        return m_kv.group(1).strip()
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\n?|```$", "", cleaned).strip()
    return cleaned

@agent_bp.route("/generate_tips/<int:workshop_id>", methods=["POST"])
@login_required
def generate_tips(workshop_id):
    tip = generate_tip_text(workshop_id)
    return jsonify({"tip": tip}), 200