# app/services/routes/agent.py
# ----------------------------------------------------------

# -----------------------------------------------------------
#                   VIRTUAL AGENT MODULE
# -----------------------------------------------------------
#
# # This file contains the routes for the ai agent module.
# # Using AWS Bedrock (Nova) Models
# 
#
# #-----------------------------------------------------------
# # Imports and blueprint setup
# #-----------------------------------------------------------
import os, json, re
from typing import Any
from flask import Blueprint, current_app, request, jsonify
from datetime import datetime, timedelta
from flask_login import login_required
from flask_socketio import emit
from app.extensions import db
from app.models import Workshop, WorkshopParticipant, WorkshopDocument, ChatMessage
from app.config import Config
from pydantic import SecretStr
import importlib

# Import Bedrock LLM helper and prompt template
from app.utils.llm_bedrock import get_chat_llm, get_chat_llm_pro, get_text_embeddings
from langchain_core.prompts import PromptTemplate
from concurrent.futures import ThreadPoolExecutor # TODO: ... overload if required later.

# Serialized pre-workshop context helper
from app.utils.data_aggregation import get_pre_workshop_context_json
from app.utils.json_utils import extract_json_block

agent_bp = Blueprint(   "agent_bp", 
                        __name__, 
                        template_folder="templates", 
                        static_folder="static",
                        url_prefix="/agent"
                    )


##############################################################

# #-----------------------------------------------------------

### VIRTUAL ASSISTANT # #-----------------------------------------
# # LLM-powered architecture with chat interface for the agent.
### #---------------------------------------------------------

# #-----------------------------------------------------------
# # Import libraries
from langgraph.prebuilt import create_react_agent
from langchain.agents import Tool, initialize_agent
from langchain.tools import tool
from langchain_core.prompts import PromptTemplate


from app.extensions import db, socketio
from flask_login import current_user
from datetime import datetime
from app.config import Config
from langchain.schema import HumanMessage, SystemMessage
from langgraph.graph import START, MessagesState, StateGraph
#-----------------------------------------------------------
# # Model Setup
llm = get_chat_llm_pro(
    model_kwargs={
        "temperature": 0.6,
        "max_tokens": 500,
        "top_k": 40,
        "top_p": 0.8,
    }
)
#-----------------------------------------------------------
# # System message template
system_message = """
                You are the virtual workshop assistant.
                """
#-----------------------------------------------------------
# # Conversational workflow
workflow = StateGraph(state_schema=MessagesState)
def call_model(state: MessagesState):
    """
    This function is responsible for sending the current conversation messages
    to the model and returning the model's response. It takes the current state,
    extracts the messages, invokes the model, and then returns the new messages.
    """
    system_msg = SystemMessage(content=system_message)
    # Ensure the system message is the first message in the conversation
    messages = [system_msg] + state["messages"]
    
    response = llm.invoke(messages)
    # Ensure the response is in dictionary format
    if not isinstance(response, dict):
        response = {"message": response}
    
    return {"messages": response}

workflow.add_edge(START, "model")
workflow.add_node("model", call_model)
#-----------------------------------------------------------
# # State Management
# The legacy LangGraph SqliteSaver memory has been retired in favor of
# AgentCore-managed memories. We keep the workflow scaffolding here for
# future agent expansions, but no on-disk checkpointing is initialized.


def mark_action_item_tool_func(workshop_id: int, description: str) -> str:
    """Fallback action item hook for the legacy agent workflow.

    The modern assistant persists action items through the AgentCore stack.
    This stub keeps the legacy tool wiring intact while signalling that the
    feature is no longer active in this route.
    """

    current_app.logger.info(
        "mark_action_item_tool invoked for workshop %s but legacy agent tooling is disabled.",
        workshop_id,
    )
    return "Action item capture is handled by the assistant memory service."


#-----------------------------------------------------------

# # PDF Retrieval & Q&A Tool
def pdf_qa(input_str: str) -> str:
    """
    Expects a JSON or simple string input with two keys: 
      - "pdf_path": path to the PDF
      - "question": question to ask
    Example input:
        {
            "pdf_path": "path/to/file.pdf",
            "question": "What does the document say about XYZ?"
        }
    """
    try:
        data = json.loads(input_str)
        pdf_path = data.get("pdf_path", "").strip()
        question = data.get("question", "").strip()
        if not pdf_path or not question:
            return "Error: Please provide both 'pdf_path' and 'question'."
    except:
        return ("Error: Input must be valid JSON with 'pdf_path' and 'question' keys. "
                "Example: {\"pdf_path\": \"sample.pdf\", \"question\": \"...\"}")

    # Load and process PDF document
    try:
        doc_loader_mod = importlib.import_module("langchain_community.document_loaders")
        PyPDFLoader = getattr(doc_loader_mod, "PyPDFLoader")
    except Exception:
        return "PDF QA unavailable: PyPDFLoader not installed."
    loader = PyPDFLoader(pdf_path)
    docs = loader.load()

    # Split documents into manageable chunks.
    try:
        try:
            splitters_mod = importlib.import_module("langchain_text_splitters")
            RecursiveCharacterTextSplitter = getattr(splitters_mod, "RecursiveCharacterTextSplitter")
        except Exception:
            splitters_mod = importlib.import_module("langchain.text_splitter")
            RecursiveCharacterTextSplitter = getattr(splitters_mod, "RecursiveCharacterTextSplitter")
    except Exception:
        return "PDF QA unavailable: RecursiveCharacterTextSplitter not installed."
    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=100)
    splitted_docs = text_splitter.split_documents(docs)

    # Create embeddings using Bedrock embeddings (Titan v2 by default)
    try:
        embeddings = get_text_embeddings()
    except Exception:
        return "PDF QA unavailable: BedrockEmbeddings not installed or misconfigured."

    # Build in-memory FAISS vector store for retrieval.
    try:
        vs_mod = importlib.import_module("langchain_community.vectorstores")
        FAISS = getattr(vs_mod, "FAISS")
    except Exception:
        return "PDF QA unavailable: FAISS vector store not installed."
    vector_store = FAISS.from_documents(splitted_docs, embeddings)

    # Setup a retrieval-based Q&A chain with a nested LLM.
    qa_llm = get_chat_llm(model_kwargs={"temperature": 0, "max_tokens": 1500})
    try:
        chains_mod = importlib.import_module("langchain.chains")
        RetrievalQA = getattr(chains_mod, "RetrievalQA")
    except Exception:
        return "PDF QA unavailable: RetrievalQA chain not installed."
    qa_chain = RetrievalQA.from_chain_type(
        llm=qa_llm,
        chain_type="stuff",
        retriever=vector_store.as_retriever(),
    )

    # Ask the question with retrieved PDF context
    answer = qa_chain.run(question)
    return answer

# Wrap the PDF Q&A function as a Tool object
pdf_qa_tool = Tool(
    name="PdfQA",
    func=pdf_qa,
    description=(
        "Use this tool to answer questions about the contents of a PDF. "
        "Input must be JSON with 'pdf_path' and 'question' keys."
    ),
)
#-----------------------------------------------------------

# Note: action_item_tool is created within create_agent_executor to capture workshop_id
#-----------------------------------------------------------
# # Agent Initialization
def create_agent_executor(workshop_id):
    # Combine All Tools
    action_item_tool = Tool(
        name="mark_action_item",
        func=lambda description: mark_action_item_tool_func(workshop_id, description),
        description=("Use this tool to add action items."),
    )
    tools = [pdf_qa_tool, action_item_tool]
    # Create the agent executor using the ReACT agent with Model and Tools.
    agent_executor = create_react_agent(llm, tools, checkpointer=None)
    return agent_executor


#-----------------------------------------------------------
# # Process User Query
def process_user_query(workshop_id, user_query):
    print("PROCESSING USER QUERY") # DEBUG CODE
    pre_workshop_data = get_pre_workshop_context_json(workshop_id)
    print("AGGREGATE DATA ACQUIRED")
    agent_executor = create_agent_executor(workshop_id)
    
    prompt = f"""
    You are a helpful virtual assistant for a workshop.

    workshop Context:
    {pre_workshop_data}

    User Query:
    {user_query}

    Please respond accurately or execute any required actions.
    """
    # Create a state dictionary with a "messages" key
    state = {"messages": [HumanMessage(content=prompt)]}
    # Some versions of langgraph expect RunnableConfig; avoid passing plain dict to keep it compatible
    try:
        result = agent_executor.invoke(state)
        
        # If the result contains messages, extract the last message only
        # Best-effort extraction of text content from result
        if isinstance(result, dict):
            messages = result.get("messages")
            if isinstance(messages, list) and messages:
                last_message = messages[-1]
                if isinstance(last_message, dict) and "content" in last_message:
                    return str(last_message.get("content", ""))
                if hasattr(last_message, "content"):
                    return str(getattr(last_message, "content"))
                return str(last_message)
            return str(messages) if messages is not None else json.dumps(result)
        return str(result)
    except Exception as e:
        return f"Agent execution error: {str(e)}"
#-----------------------------------------------------------
# # Agent chat message
@agent_bp.route("/chat", methods=["POST"])
# @login_required
def chat():
    data = request.get_json() or {}
    print("AGENT Chat Workshop ID ", data.get("workshop_id", 0))
    print("AGENT Chat User ID ", data.get("user_id", 0))
    user_message = data.get("message", "").strip()
    workshop_id = data.get("workshop_id", 0)
    
    
    if not user_message:
        return jsonify({"error": "Message required."}), 400
    
    
    # Check if current_user is authenticated and has the necessary attributes.
    if hasattr(current_user, "is_authenticated") and current_user.is_authenticated:
        uid = getattr(current_user, "user_id", 0)
        uname = getattr(current_user, "username", "anonymous")
    else:
        uid = 0
        uname = "anonymous"
    
    # Save the user's message in the DB
    chat_msg = ChatMessage()
    chat_msg.workshop_id = workshop_id
    chat_msg.user_id = uid
    chat_msg.username = uname
    chat_msg.message = user_message
    chat_msg.timestamp = datetime.utcnow()
    try:
        chat_msg.message_type = 'user'
    except Exception:
        pass
    # Scope: allow client to hint, default to workshop_chat
    try:
        scope = (data.get('scope') or 'workshop_chat').strip()
        if scope not in ('workshop_chat', 'discussion_chat'):
            scope = 'workshop_chat'
        setattr(chat_msg, 'chat_scope', scope)
    except Exception:
        pass
    db.session.add(chat_msg)
    db.session.commit()
    
    # Use the unified LLM chain to process the query
    agent_response = process_user_query(workshop_id, user_message)
    
    socketio.emit("agent_response", {
        "message": agent_response,
        "type": "unified",
        "username": "Agent",
        "message_type": "facilitator",
        "chat_scope": getattr(chat_msg, 'chat_scope', 'workshop_chat')
    }, to=f"workshop_room_{workshop_id}", namespace="/agent")
    current_app.logger.info(f"Agent response emitted to workshop_{workshop_id}")

    return jsonify({"ok": True}), 200



