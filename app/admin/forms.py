# app/admin/forms.py
from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import StringField, SelectField, BooleanField, TextAreaField, PasswordField
from wtforms.validators import DataRequired, Email, Length, Optional

class UserManagementForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    first_name = StringField('First Name', validators=[Optional(), Length(max=100)])
    last_name = StringField('Last Name', validators=[Optional(), Length(max=100)])
    username = StringField('Username', validators=[Optional(), Length(max=100)])
    role = SelectField('Role', 
                      choices=[('user', 'User'), ('manager', 'Manager'), ('admin', 'Administrator')],
                      validators=[DataRequired()])
    email_verified = BooleanField('Email Verified')
    is_public_profile = BooleanField('Public Profile')
    job_title = StringField('Job Title', validators=[Optional(), Length(max=150)])
    organization = StringField('Organization', validators=[Optional(), Length(max=150)])

class UserCreationForm(UserManagementForm):
    password = PasswordField('Password', validators=[DataRequired(), Length(min=8)])
    send_welcome_email = BooleanField('Send Welcome Email', default=True)

class SystemConfigForm(FlaskForm):
    app_name = StringField('Application Name', validators=[DataRequired()])
    default_timezone = StringField('Default Timezone', validators=[DataRequired()])
    workshop_default_duration = StringField('Default Workshop Duration (minutes)')
    max_workshop_participants = StringField('Max Workshop Participants')
    enable_ai_features = BooleanField('Enable AI Features')
    bedrock_model_id = StringField('Primary Bedrock Model ID')
    smtp_server = StringField('SMTP Server')
    smtp_port = StringField('SMTP Port')
    mail_username = StringField('Mail Username')


class DocumentUploadForm(FlaskForm):
    workspace_id = SelectField('Workspace', coerce=int, validators=[DataRequired(message="Select a workspace")])
    title = StringField('Title', validators=[Optional(), Length(max=255)])
    description = TextAreaField('Description', validators=[Optional(), Length(max=2000)])
    file = FileField(
        'Document file',
        validators=[
            FileRequired(message="Select a document to upload"),
            FileAllowed(
                [
                    'pdf', 'doc', 'docx', 'ppt', 'pptx', 'txt', 'md', 'csv', 'xlsx',
                    'xls', 'rtf', 'html', 'json', 'wav', 'mp3', 'mp4'
                ],
                message="Unsupported file type",
            ),
        ],
    )