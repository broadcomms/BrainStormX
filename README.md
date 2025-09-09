# Brainstorm X

Brainstorm X is an ai-embedded collaborative brainstorming platform designed to streamline workshop planning, executing and follow-up. It enables organizers to create structured workshops, manage participants and documents. It leverages AWS Bedrock foundation models to generate workshop agenda, action plans, session rules, icebreakers, tips and even nudges participants to encourage engagement. The application has the ability to moderate voting and idea prioritization, access the feasibility of proposed idea and forecast the market trends.

## Key features

The solution performs the following operations:

1. Manages workshop participants and documents
2. Captures and organizes participants inputs in to digital colored sticky notes
3. Enhances participants inputs into clear, problem and solution statements
4. Automatically creates workshop agenda, task sequence, session rules, icebreakers, tips
5. Monitors inactivity and nudges silent participants to encourage engagement
6. Clusters and facilitates timed voting pools and idea prioritization
7. Accesses the feasibility of proposed idea
8. Forecasts the market trends
9. Synthesizes workshop minutes, transcripts, summaries and action items

## Architecture

The application is implemented with Python and JavaScript using Flask, LangChain libraries and AWS Bedrock foundation models.

## Structure

```
app/
    __init__.py
    config.py
    models.py
    extensions.py
    sockets.py
    main/
        routes.py
        templates/
            main_template.html
            main_index.html
    auth/
        routes.py
        templates/
            auth_login.html
            auth_password.html
            auth_reset.html
    account/
        routes.py
        templates/
            account_form.html
            account_create.html
            account_edit.html
            account_list.html
            account_details.html
    workspace/
        routes.py
        templates/
            workspace_form.html
            workspace_create.html
            workspace_edit.html
            workspace_list.html
            workspace_details.html
    document/
        routes.py
        templates/
            document_upload.html
            document_list.html
            document_details.html
          
    workshop/
        routes.py
        templates/
            workshop_form.html
            workshop_create.html
            workshop_edit.html
            workshop_list.html
            workshop_details.html
            workshop_lobby.html
            workshop_room.html
            workshop_report.html
    chat/
        routes.py
        templates/
            chat_room.html
    service/
        routes/
            agenda.py
            plan.py
            rules.py
            icebreaker.py
            tip.py
            task.py
            nudge.py
            vote.py
            idea.py
            agent.py
        templates/
            service_agenda.html
            service_plan.html
            service_rules.html
            service_tip.html
            service_icebreaker.html
            service_task.html
            service_nudge.html
            service_vote.html
            service_agent.html
instance
   app_database.sqlite
   agent_memory.sqlite
   documents/
            _sample.png 
            _sample.pdf
            _sample.docx
            _sample.pptx
            _sample.xlsx
   default/
            _profile.png
   uploads/
requirements.txt
run.py
```

# Initialize Virtual environment

```
# ensure you are in the project root
cd brainstormx

# create virtual environment
python3 -m venv venv

# activate virtual environment
source venv/bin/activate

# upgrade package manager
pip install -upgrade pip

# install project requirements
pip install -r requirements.txt

```

# Run the application locally using the flask server

```
# start flask server
python run.py
```

# Build and run the application as a docker container

```
# Build the Docker image
docker build -t brainstormx .

# Run the Docker container
docker run -p 5001:5001 --name brainstormx -d brainstorm_x

# Access the application in browser
http://localhost:5001

# Optionally, if you want to access the application detached mode
docker run -d -p 5001:5001 --name brainstormx -d brainstormx

```

# Deploying application from docker container registry

```
# Tag the image
docker tag brainstormx broadcomms/brainstorm_x:latest

# Login to the Docker registry
docker login

# Push the image docker hub
docker push broadcomms/brainstormx:latest

```
