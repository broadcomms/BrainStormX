# app/service/routes/task.py
import json
import re
from typing import Any

from flask import current_app

from app.service.routes.agent import generate_next_task_text
from app.utils.json_utils import extract_json_block
from app.types.action_plan import ActionPlanItem


def get_next_task_payload(workshop_id: int, action_plan_item: ActionPlanItem | None = None):
    """
    1) Aggregate pre-workshop data.
    2) Call the LLM to generate the next task based on context/action plan.
    3) Extract and parse the JSON.
    Returns:
      - a dict payload on success
      - (error_message, status_code) tuple on failure
    """
    current_app.logger.debug(f"[Task Service] Generating next task for workshop {workshop_id}")





    # Call the agent function to get the raw LLM output
    raw_task_data = generate_next_task_text(workshop_id, action_plan_item=action_plan_item)
    raw_task_text = raw_task_data if isinstance(raw_task_data, str) else str(raw_task_data)






    # Check if the generator returned an error string directly
    if raw_task_text.startswith('{"error":'):
        try:
            error_payload = json.loads(raw_task_text)
            return error_payload.get("error", "Failed to generate task: Unknown error."), 500
        except json.JSONDecodeError:
            return f"Failed to generate task: {raw_task_text}", 500

    # --- USE THE NEW UTILITY FUNCTION ---
    json_block = extract_json_block(raw_task_text)
    # -----------------------------------
    current_app.logger.debug(f"[Task Service] Raw LLM task: {raw_task_data}")
    current_app.logger.debug(f"[Task Service] Extracted JSON block: {json_block}")

    # --- ADD CHECK FOR EMPTY BLOCK ---
    if not json_block:
        current_app.logger.error(
            f"[Task Service] Could not extract valid JSON block for workshop {workshop_id}. Raw: {raw_task_text[:200]}"
        )
        return "Failed to extract valid task JSON from AI response.", 500
    # --------------------------------

    try:
        # Parse the extracted JSON block
        payload = json.loads(json_block)
        if not isinstance(payload, dict):
            raise ValueError("LLM did not return a valid JSON object.")

        # Basic validation of required fields (optional but recommended)
        required_keys = ["title", "task_type", "task_description", "instructions", "task_duration"]
        if not all(key in payload for key in required_keys):
            missing = [key for key in required_keys if key not in payload]
            current_app.logger.warning(f"[Task Service] Task payload missing keys: {missing}")
            # Consider returning an error or using defaults more strictly here if needed

        # Ensure duration is an integer (strict mode respects AI_STRICT_PHASES config)
        strict_mode = current_app.config.get('AI_STRICT_PHASES', False)
        
        if 'task_duration' not in payload:
            error_msg = "[Task Service] LLM failed to provide task_duration"
            if strict_mode:
                current_app.logger.error(f"{error_msg} - refusing heuristic fallback (strict mode)")
                return "LLM failed to generate task_duration - refusing heuristic fallback", 500
            else:
                current_app.logger.warning(f"{error_msg}, defaulting to 60 seconds")
                payload['task_duration'] = 60
        else:
            try:
                payload['task_duration'] = int(payload['task_duration'])
            except (ValueError, TypeError) as e:
                error_msg = f"[Task Service] Invalid task_duration '{payload.get('task_duration')}'"
                if strict_mode:
                    current_app.logger.error(f"{error_msg} - refusing heuristic fallback (strict mode)")
                    return f"LLM provided invalid task_duration: {e}", 500
                else:
                    current_app.logger.warning(f"{error_msg}, defaulting to 60 seconds")
                    payload['task_duration'] = 60

        current_app.logger.info(f"[Task Service] Successfully parsed task payload for workshop {workshop_id}")
        return payload

    except json.JSONDecodeError as e:
        current_app.logger.error(f"[Task Service] JSON parse error for workshop {workshop_id}: {e}. Block: {json_block}")
        # Make error message slightly more informative
        return f"Invalid task JSON received from AI (parse error): {e}", 500
    except ValueError as e:
        current_app.logger.error(
            f"[Task Service] Invalid task structure for workshop {workshop_id}: {e}. Block: {json_block}"
        )
        return f"Invalid task structure received from AI: {e}", 500
    except Exception as e:
        current_app.logger.error(f"[Task Service] Unexpected error parsing task for workshop {workshop_id}: {e}", exc_info=True)
        return "Unexpected error processing task.", 500

