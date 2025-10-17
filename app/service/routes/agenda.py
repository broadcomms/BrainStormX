# app/service/routes/agenda.py
import re  # Add this import for regex
import json
from flask import jsonify
from flask_login import login_required
from app.utils.llm_bedrock import get_chat_llm
from app.utils.json_utils import extract_json_block
from langchain_core.prompts import PromptTemplate
from app.config import Config

# Attempt to import agent blueprint; fall back to a dummy blueprint when disabled in tests
try:  # pragma: no cover - import side effect only
    from .agent import agent_bp  # type: ignore
except Exception:  # Provides a no-op substitute when langgraph / agent stack unavailable
    from flask import Blueprint
    agent_bp = Blueprint("agent_bp", __name__)

from app.utils.data_aggregation import get_pre_workshop_context_json
import markdown # If you plan to return HTML directly later

# -----------------------------------------------------------
# 1.b Generate workshop agenda (New Function)
def generate_agenda_text(workshop_id):
    """Generates a suggested workshop agenda using the LLM."""
    pre_workshop_data = get_pre_workshop_context_json(workshop_id)
    if not pre_workshop_data:
        return "Could not generate agenda: Workshop data unavailable."

    agenda_prompt_template = """
                            You are an AI assistant facilitating a brainstorming workshop.
                            Based *only* on the workshop context provided below, generate a structured timed agenda for the workshop.
                            The agenda should logically flow towards achieving the workshop's objective within the workshop duration.

                            Workshop Context:
                            {pre_workshop_data}

                            Instructions:
                            - Generate 4-5 bullet points to list the agenda items.
                            - Include estimated time to complete each item.
                            - Ensure it is related to workshop context (based on the Title and Objective)
                            
                            Format:
                            Output MUST be valid JSON with the key "agenda":, with an array of objects each containing the following keys:
                            - "time_slot":
                            - "activity":
                            - "description":
                            - "estimated_duration":

                            Response:
                            """

    bedrock_llm_agenda = get_chat_llm(
        model_kwargs={
            "temperature": 0.5,
            "max_tokens": 800,
            "top_k": 45,
            "top_p": 0.9,
        }
    )

    agenda_prompt = PromptTemplate.from_template(agenda_prompt_template)
    chain = agenda_prompt | bedrock_llm_agenda

    try:
        raw = chain.invoke({"pre_workshop_data": pre_workshop_data})
        print(f"[Agenda Service] Workshop raw agenda _ID:{workshop_id}: {raw}")  # Debugging

        # Normalize response to text
        text = raw
        try:
            # AIMessage or similar object with .content
            if hasattr(raw, "content"):
                text = raw.content
            # LangChain may return dict-like with 'content'
            elif isinstance(raw, dict) and "content" in raw:
                text = raw["content"]
        except Exception:
            pass

        # Ensure we are working with a string
        if not isinstance(text, str):
            text = str(text)

        # Use our robust JSON extractor (handles ```json fences)
        json_block = extract_json_block(text)
        if json_block:
            return json_block.strip()

        # Fallback: simple object regex
        match = re.search(r"(\{[\s\S]*?\})", text, re.DOTALL)
        if match:
            return match.group(1).strip()

        raise ValueError("No valid JSON block found in the response.")

    except Exception as e:
        print(f"[Agenda Service] Error generating agenda _ID:{workshop_id}: {e}")
        return "Could not generate agenda due to an internal error."


# API endpoint if you want a direct route to generate *only* the agenda for frontend processing.
# Note: Your current setup calls /workshop/.../regenerate/agenda which then calls generate_agenda_text
@agent_bp.route("/generate_agenda/<int:workshop_id>", methods=["POST"])
@login_required
def generate_agenda(workshop_id):
    """API endpoint to generate and return an agenda (optional direct route)."""
    agenda_text = generate_agenda_text(workshop_id)
    if "Could not generate agenda" in agenda_text:
        return jsonify({"error": agenda_text}), 500 # Use 500 for server-side generation issues
    # Return raw text or rendered HTML
    # agenda_html = markdown.markdown(agenda_text)
    return jsonify({"agenda": agenda_text}), 200 # Returning raw text for now
