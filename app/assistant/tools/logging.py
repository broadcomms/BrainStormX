# app/assistant/tools/logging.py
import logging
import json
from pythonjsonlogger import jsonlogger

def setup_tool_logging(app):
    """Configure structured logging for tools"""
    
    # JSON formatter
    formatter = jsonlogger.JsonFormatter(
        fmt='%(timestamp)s %(level)s %(name)s %(message)s',
        rename_fields={'timestamp': '@timestamp'}
    )
    
    # Tool-specific logger
    tool_logger = logging.getLogger('tools')
    tool_logger.setLevel(logging.INFO)
    
    # Console handler with JSON
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    tool_logger.addHandler(console_handler)
    
    # File handler for tool audit
    file_handler = logging.FileHandler('logs/tools.jsonl')
    file_handler.setFormatter(formatter)
    tool_logger.addHandler(file_handler)
    
    return tool_logger