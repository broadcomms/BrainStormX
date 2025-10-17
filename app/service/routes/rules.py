# app/service/routes/rules.py

from flask import jsonify
from flask_login import login_required
from app.utils.llm_bedrock import get_chat_llm
from langchain_core.prompts import PromptTemplate
from app.config import Config
# Import the blueprint and the helper function from agent.py
from .agent import agent_bp
import markdown # If you plan to return HTML directly later
from app.utils.data_aggregation import get_pre_workshop_context_json

# #-----------------------------------------------------------
# # 2.b Generate rules and guidelines
@agent_bp.route("/generate_rules_text/<int:workshop_id>", methods=["POST"])
@login_required
def generate_rules_text(workshop_id):
    """ Service Generates suggested workshop rules using the LLM."""
    pre_workshop_data = get_pre_workshop_context_json(workshop_id)
    if not pre_workshop_data:
        # Return a meaningful message or error response
        return jsonify({"error": f"Could not generate rules: Workshop data unavailable."}), 404

    # Define the prompt template for generating rules
    rules_prompt_template =   """
                                You are a facilitator for a brainstorming workshop.
                                Based *only* on the detailed context provided below, create 3-5 clear, concise, and actionable rules or guidelines for the participants.
                                Focus on fostering collaboration, active participation, and respect, tailored to the workshop's specific objective and agenda.

                                Workshop Context:
                                {pre_workshop_data}

                                Instructions:
                                - Generate a numbered list of 3 to 5 rules in less than 80 words.
                                - Ensure rules are directly relevant to the workshop's title and objective.
                                - Output *only* the numbered list of rules, with no introductory sentence, explanation, or any other text before or after the list.

                                Generate the rules now:
                                """
    
    # initialize Bedrock llm
    bedrock_llm_rules = get_chat_llm(
        model_kwargs={
            "temperature": 0.5,
            "max_tokens": 150,
        }
    )
    # Define llm prompt
    rules_prompt = PromptTemplate.from_template(rules_prompt_template)
    
    # Invoke llm chain
    chain = rules_prompt | bedrock_llm_rules
    try:
        raw_rules = chain.invoke({"pre_workshop_data": pre_workshop_data})
        # Normalize to plain string
        text = raw_rules
        try:
            if hasattr(raw_rules, "content"):
                text = raw_rules.content
            elif isinstance(raw_rules, dict) and "content" in raw_rules:
                text = raw_rules["content"]
        except Exception:
            pass
        if not isinstance(text, str):
            text = str(text)

        print(f"[Agent] Workshop raw rules for {workshop_id}: {text}")
        return text.strip()
    except Exception as e:
        # current_app.logger.error(f"LLM invocation failed for rules generation (workshop {workshop_id}): {e}")
        print(f"[Agent] Error generating rules for {workshop_id}: {e}")
        return "Could not generate rules due to an internal error."
    

@agent_bp.route("/generate_rules/<int:workshop_id>", methods=["POST"])
@login_required
def generate_rules(workshop_id):
    """API endpoint to generate and return rules."""
    rules_text = generate_rules_text(workshop_id)
    # Check if the helper function returned an error message
    if "Could not generate rules" in rules_text:
         # You might want a different HTTP status code depending on the error
        return jsonify({"error": rules_text}), 404
    return jsonify({"rules": rules_text}), 200


