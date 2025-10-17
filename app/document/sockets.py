"""Socket handlers for document processing channels."""

from __future__ import annotations

from flask import current_app, request
from flask_login import current_user
from flask_socketio import join_room

from app.extensions import db, socketio
from app.models import Document, WorkspaceMember


def _user_can_access(document: Document) -> bool:
	if not current_user.is_authenticated:
		return False
	if document.uploaded_by_id == current_user.user_id:
		return True
	membership = current_user.workspace_memberships.filter_by(
		workspace_id=document.workspace_id,
		status='active',
	).first()
	return membership is not None


@socketio.on('document_subscribe')
def handle_document_subscribe(payload):  # type: ignore[override]
	"""Allow clients to subscribe to workspace-level document processing events."""
	try:
		document_id = int(payload.get('documentId'))  # type: ignore[arg-type]
	except Exception:
		current_app.logger.warning('document_subscribe missing/invalid documentId: %s', payload)
		return

	document = db.session.get(Document, document_id)
	if not document:
		current_app.logger.warning('document_subscribe for missing document %s', document_id)
		return

	if not _user_can_access(document):
		current_app.logger.warning('document_subscribe denied for user %s on doc %s', getattr(current_user, 'user_id', None), document_id)
		return

	room = f"workspace_{document.workspace_id}" if document.workspace_id else f"document_{document_id}"
	join_room(room)
	sid = getattr(request, 'sid', None)
	current_app.logger.debug('SID %s joined %s for document %s', sid, room, document_id)
	if sid:
		socketio.emit('doc_subscription_ack', {'documentId': document_id, 'room': room}, to=sid)


@socketio.on('document_unsubscribe')
def handle_document_unsubscribe(payload):  # type: ignore[override]
	"""Placeholder handler for completeness; currently no-op."""
	# Flask-SocketIO automatically removes room membership on disconnect. We keep the handler
	# for potential future use where clients explicitly leave document rooms.
	current_app.logger.debug('document_unsubscribe payload=%s (no action)', payload)
