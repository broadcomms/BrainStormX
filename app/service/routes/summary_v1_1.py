# app/service/routes/summary.py
import json
from datetime import datetime
from flask import current_app

from app.extensions import db
from app.models import Workshop, BrainstormTask, BrainstormIdea, IdeaCluster, IdeaVote, ChatMessage
from app.config import Config, TASK_SEQUENCE
from app.utils.json_utils import extract_json_block
from app.utils.data_aggregation import get_pre_workshop_context_json
from app.utils.llm_bedrock import get_chat_llm
from pydantic import SecretStr
from sqlalchemy import func # <--- Import func
from langchain_core.prompts import PromptTemplate
from sqlalchemy.orm import joinedload


def generate_summary_text(workshop_id: int, phase_context: str):
    """Generates workshop summary text using LLM."""
    current_app.logger.debug(f"[Summary] Generating text for workshop {workshop_id}")

    # --- Aggregate More Data for Summary ---
    # Start with pre-workshop data
    summary_context = get_pre_workshop_context_json(workshop_id)
    if not summary_context:
        return "Could not generate summary: Workshop data unavailable.", 500

    # Add Ideas, Clusters (with votes), and Chat Messages
    ideas = BrainstormIdea.query.filter(BrainstormIdea.task.has(workshop_id=workshop_id)).all()
    # Query clusters and their vote counts using func.count and group_by
    clusters_with_counts = db.session.query(
            IdeaCluster, func.count(IdeaVote.id).label('vote_count')
        ).outerjoin(IdeaVote, IdeaCluster.id == IdeaVote.cluster_id) \
         .filter(IdeaCluster.task.has(workshop_id=workshop_id)) \
         .group_by(IdeaCluster.id) \
         .all()
    chat_messages = ChatMessage.query.filter_by(workshop_id=workshop_id).order_by(ChatMessage.timestamp).all()

    summary_context += "\n\n**Workshop Activity:**\n"
    if ideas:
        summary_context += f"*   **Ideas Generated ({len(ideas)}):**\n" + "\n".join([f"    - {idea.content[:80]}..." for idea in ideas[:10]]) + ("\n    - ..." if len(ideas) > 10 else "") + "\n"
    if clusters_with_counts:
        summary_context += f"*   **Clusters Discussed ({len(clusters_with_counts)}):**\n" + "\n".join([f"    - {cluster.name} (Votes: {count})" for cluster, count in clusters_with_counts]) + "\n" # Use the count from the query
    
    
    if chat_messages:
         summary_context += f"*   **Chat Snippets ({len(chat_messages)}):**\n" + "\n".join([f"    - {msg.username}: {msg.message[:60]}..." for msg in chat_messages[-5:]]) + "\n" # Last 5 messages
    # --------------------------------------

    prompt_template = """
You are the workshop facilitator, responsible for summarizing the entire session.

Workshop Context and Activity:
{summary_context}

Current Action Plan Context (Final Phase):
{phase_context}

Instructions:
1. Review all the provided context, including initial objectives, generated ideas, cluster votes, and chat snippets.
2. Synthesize the key outcomes, decisions, and any potential action items identified during the workshop.
3. Format the summary as a concise Markdown report suitable for sharing. Include sections like "Key Outcomes", "Decisions Made", "Next Steps/Action Items".
4. Generate a JSON object containing the final task details and the summary report.

Produce output as a *single* valid JSON object with these keys:
- title: "Workshop Summary"
- task_type: "summary"
- task_description: "Here is a summary of the workshop brainstorming session."
- instructions: "Thank you for your participation! The workshop is now complete."
- task_duration: The time allocated for the task in seconds (e.g., 300 for 5 minutes). Choose a reasonable value based on context.
- summary_report: A string containing the Markdown summary report.

- narration: A single paragraph the facilitator would speak aloud that:
    1) frames the purpose of the wrap-up,
    2) highlights outcomes and decisions,
    3) points participants to the action items,
    4) notes any follow-ups and the handoff.
  Use plain, natural English. No lists.
- tts_script: A single paragraph (90–180 words) that reads smoothly for text-to-speech and covers:
    session arc → highlights → decisions → next steps/owners (or TBD) → thank-you and handoff/time cue.
  Keep it natural and human. Avoid lists and quotation-marking full sentences.
- tts_read_time_seconds: Integer estimate of read time for tts_script (e.g., 45–90).

Style & Constraints:
- Tone: Warm, professional, appreciative; first-person facilitator voice.
- Grounding: Use only what’s in the provided context; mark missing details as "TBD".
- Markdown: Allowed only in summary_report; all other fields are plain text.
- No bullet point, or numbered lists. Use the spoken words to convery numbers. for 1 say one for 2 say two, not "1-2" or "1–2". DO not say dash or hyphen.
- Strict JSON: No trailing commas, no extra keys, no markdown outside summary_report, no code fences.


Respond with *only* the valid JSON object, nothing else.
"""

    bedrock_llm = get_chat_llm(
        model_kwargs={
            "temperature": 0.6,
            "max_tokens": 1200,
        }
    )

    prompt = PromptTemplate.from_template(prompt_template)
    chain = prompt | bedrock_llm

    try:
        raw_output = chain.invoke({"summary_context": summary_context, "phase_context": phase_context})
        current_app.logger.debug(f"[Summary] Raw LLM output for {workshop_id}: {raw_output}")
        return raw_output, 200
    except Exception as e:
        current_app.logger.error(f"[Summary] LLM error for workshop {workshop_id}: {e}", exc_info=True)
        return f"Error generating workshop summary: {e}", 500


def get_summary_payload(workshop_id: int, phase_context: str):
    """Generates text, creates DB record, returns payload."""
    raw_text, code = generate_summary_text(workshop_id, phase_context)
    if code != 200:
        return raw_text, code

    # Normalize AIMessage/dict to string
    def _to_text(raw):
        try:
            if raw is None:
                return ""
            if isinstance(raw, str):
                return raw
            if hasattr(raw, "content"):
                return raw.content
            if isinstance(raw, dict):
                if "content" in raw:
                    return raw.get("content")
                return json.dumps(raw)
            return str(raw)
        except Exception:
            return str(raw)

    raw_text_str = _to_text(raw_text) or ""
    json_block = extract_json_block(raw_text_str)
    if not json_block: return "Could not extract valid JSON for summary task.", 500
    try:
        payload = json.loads(json_block)
        if not all(k in payload for k in ["title", "task_description", "instructions", "task_duration", "summary_report"]):
            raise ValueError("Missing keys.")
        payload["task_type"] = "summary"
        task = BrainstormTask()
        task.workshop_id = workshop_id
        task.task_type = payload["task_type"]
        task.title = payload["title"]
        task.description = payload.get("task_description")
        payload_str = json.dumps(payload)
        task.prompt = payload_str
        task.payload_json = payload_str
        task.duration = int(payload.get("task_duration", 120))
        task.status = "pending"
        db.session.add(task)
        db.session.flush()
        payload['task_id'] = task.id
        current_app.logger.info(f"[Summary] Created task {task.id} for workshop {workshop_id}")
        # Note: Workshop status is set to 'completed' in the stop_workshop route usually.
        return payload
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        current_app.logger.error(f"[Summary] Payload error {workshop_id}: {e}\nJSON: {json_block}", exc_info=True)
        db.session.rollback()
        return f"Invalid summary task format: {e}", 500
    except Exception as e:
        current_app.logger.error(f"[Summary] DB error {workshop_id}: {e}", exc_info=True)
        db.session.rollback()
        return "Server error creating summary task.", 500