# PROJECT SUBMISSION

```
System:
You are an expert technical writer. 
You will help me write submission of BrainStormX for a hackathon. 
I will provide you with the context of the hackathon and the requirements for the submission. 
You will study the context and requirements carefully.
You will study the any existing document in the code base that is relevant to the submission.
You will study the exiting code base to understand the technical details of the project.
You will understand the project deeply. How it works, what technologies it uses, what problems it solves, how it is architected. 

DELIVERABLES:
You will help me write the submission document in markdown format.
1. You will help me write the project story. output in markdown format to PROJECT_STORY.md.
2. You will help me write the "Build with" section. output in markdown format to BUILD_WITH.md.
```

**CONTEXT**

AWS AI Agent Global Hackathon

Agents of change - building tomorrows AI solution today

We challenge you to build, develop, and deploy a working AI Agent on AWS using cutting-edge tools like Amazon Bedrock, Amazon SageMaker AI, and the Amazon Bedrock AgentCore. It's an exciting opportunity to explore the future of autonomous systems by building agents that use reasoning, connect to external tools and APIs, and execute complex tasks.

Showcase your technical prowess, solve real-world problems, and push the boundaries of what's possible with AI. Built a novel solutions that are creative, well-architected, and have the potential for measurable impact.

Start architecting your solution and show us what you can build.

### Requirements

##### WHAT TO BUILD

Build, develop, and deploy a working AI Agent on AWS. Each working AI agent must meet three conditions below:

1. Large Language Model (LLM) hosted out of AWS Bedrock or Amazon SageMaker AI.
2. Uses one or more of the following AWS services:
   * Amazon Bedrock AgentCore - at least 1 primitive (strongly recommended)
   * Amazon Bedrock/Nova
   * Amazon Q
   * Amazon SageMaker AI
   * Amazon SDKs for Agents/Nova Act SDK (Strands Agents, DIY agents using AWS) infrastructure)
   * AWS Transform
   * Kiro (leveraged for agent building)
3. Meets AWS-defined APPENDIX A [AI agent qualification](https://aws.amazon.com/what-is/ai-agents/)
   * Uses reasoning LLMs (or a similar component) for decision-making
   * Demonstrates autonomous capabilities with or without human inputs for task execution
   * Integrates APIs, databases, external tools (e.g., web search, code execution, etc.) or other agents

*Below are additional helper services that are optional for participants to leverage:*

* *AWS Lambda*
* *Amazon Simple Storage Service (S3)*
* *Amazon API Gateway*

##### WHAT TO SUBMIT

* URL to your **public** code repo - **must contain all necessary source code, assets, and instructions required for the project to be functional**
* **Architecture Diagram - https://aws.amazon.com/what-is/architecture-diagramming/**
* **Project Story**
  * About the project
    * Be sure to write what inspired you, what you learned, how you built your project, and the challenges you faced. Format your story in Mrkdown, with LaTeX support for math.
* **Build with**
  * What languages, frameworks, platforms, cloud services, databases, APIs, or other technologies did you use?


### Judges

![Rohan Karmarkar](https://d112y698adiu2z.cloudfront.net/photos/production/judge_photos/003/716/759/datas/large.png)

**Rohan Karmarkar**
*AWS Director, Partner Solutions Architecture, [LinkedIn](https://www.linkedin.com/in/rohankarmarkar/)*

![Raghvender Arni, ](https://d112y698adiu2z.cloudfront.net/photos/production/judge_photos/003/716/760/datas/large.png)

**Raghvender Arni, "Arni"**
*AWS Director, Cloud & AI Innovation, [LinkedIn](https://www.linkedin.com/in/rarni/)*

![Rory Richardson](https://d112y698adiu2z.cloudfront.net/photos/production/judge_photos/003/716/761/datas/large.png)

**Rory Richardson**
*AWS Director, Next Generation Developer Experience, GenAI GTM, [LinkedIn](https://www.linkedin.com/in/roryr/)*

![Kamini Aisola](https://d112y698adiu2z.cloudfront.net/photos/production/judge_photos/003/716/762/datas/large.png)

**Kamini Aisola**
*AWS Director EMEA Specialists, [LinkedIn](https://www.linkedin.com/in/kaisola/)*



### Judging Criteria

* **Potential Value/Impact 20%**

  • What real-world problem/process is it solving/improving?
  • What (measurable) impact does it have?

* **Creativity 10%**

  • Novelty of problem
  • Novelty of approach

* **Technical Execution 50%**

  Must use the technology/technologies mentioned in the What to Build section
  • Is the solution well-architected?
  • Is it reproducible?

* **Functionality 10%**

  • Is/are the agent(s) working as expected?
  • Is the solution scalable?

* **Demo Presentation 10%**

  • Does it include the end-to-end agentic workflow?
  • How is the demo quality and clarity?

**SUBMISSION**

Please respect our Community Guidelines

[https://info.devpost.com/legal/community-guidelines](https://info.devpost.com/legal/community-guidelines)

Full rules - https://aws-agent-hackathon.devpost.com/rules 

**PROJECT DETAILS**

**Project Name:**

BrainStormX

**Elevator Pitch:**

*Provide a short tagline for the project.*

Intelligent Brainstorming and Collaborative-Innovation Platform

**Thumbnail Logo:**

5 MB

3:2 aspect ration.

**Project Story**

About the project

Be sure to write what inspired you, what you learned, how you built your project, and the challenges you faced. Format your story in Mrkdown, with LaTeX support for math.

**Build with**

What languages, frameworks, platforms, cloud services, databases, APIs, or other technologies did you use?

## APPENDIX A - AWS AI AGENT QUALIFICATION

What Are AI Agents? - https://aws.amazon.com/what-is/ai-agents/

## What are AI Agents?

An artificial intelligence (AI) agent is a software program that can interact with its environment, collect data, and use that data to perform self-directed tasks that meet predetermined goals. Humans set goals, but an AI agent independently chooses the best actions it needs to perform to achieve those goals. For example, consider a contact center AI agent that wants to resolve customer queries. The agent will automatically ask the customer different questions, look up information in internal documents, and respond with a solution. Based on the customer responses, it determines if it can resolve the query itself or pass it on to a human.

Multiple AI agents can collaborate to automate complex workflows and can also be used in [agentic ai systems](https://aws.amazon.com/ai/agentic-ai/). They exchange data with each other, allowing the entire system to work together to achieve common goals. Individual AI agents can be specialized to perform specific subtasks with accuracy. An orchestrator agent coordinates the activities of different specialist agents to complete larger, more complex tasks.

[Learn more about what is artificial intelligence (AI)](https://aws.amazon.com/what-is/artificial-intelligence/?refid=faq_card)

## What are the key principles that define AI agents?

All software autonomously performs various routine tasks as specified by the software developer. So, what makes AI agents special?

### Autonomy

AI agents act autonomously, without constant human intervention. While traditional software follows hard-coded instructions, AI agents identify the next appropriate action based on past data and execute it without continuous human oversight.

For example, a bookkeeping agent automatically flags and requests missing invoice data for purchases.

### Goal-oriented behavior

AI agents are driven by objectives. Their actions aim to maximize success as defined by a utility function or performance metric. Unlike traditional programs that merely complete tasks, intelligent agents pursue goals and evaluate the consequences of their actions in relation to those goals.

For example, an AI logistics system optimizes delivery routes to balance speed, cost, and fuel consumption simultaneously, thereby balancing multiple objectives.

### Perception

AI agents interact with their environment by collecting data through sensors or digital inputs. They can collect data from external systems and tools via APIS. This data allows them to perceive the world around them, recognize changes, and update their internal state accordingly.

For example, cybersecurity agents collect data from third-party databases to remain aware of the latest security incidents.

### Rationality

AI agents are rational entities with reasoning capabilities. They combine data from their environment with domain knowledge and past context to make informed decisions, achieving optimal performance and results.

For example, a robotic agent collects sensor data, and a[ chatbot](https://aws.amazon.com/what-is/chatbot/?refid=faq_card) uses customer queries as input. The AI agent applies the data to make an informed decision. It analyzes the collected data to predict the best outcomes that support predetermined goals. The agent also uses the results to formulate the next action that it should take. For example, self-driving cars navigate around obstacles on the road based on data from multiple sensors.

### Proactivity

AI agents can take initiative based on forecasts and models of future states. Instead of simply reacting to inputs, they anticipate events and prepare accordingly.

For instance, an AI-based customer service agent might reach out to a user whose behavior suggests frustration, offering help before a support ticket is filed. Autonomous warehouse robots may reposition themselves in anticipation of upcoming high-traffic operations.

### Continuous learning

AI agents improve over time by learning from past interactions. They identify patterns, feedback, and outcomes to refine their behavior and decision-making. This differentiates them from static programs that always behave the same way regardless of new inputs.

For instance, predictive maintenance agents learn from past equipment failures to better forecast future issues.

### Adaptability

AI agents adjust their strategies in response to new circumstances. This flexibility allows them to handle uncertainty, novel situations, and incomplete information.

For example, a stock trading bot adapts its strategy during a market crash, while a game-playing agent like AlphaZero discovers new tactics through self-play, even without prior human strategies.

### Collaboration

AI agents can work with other agents or human agents to achieve shared goals. They are capable of communicating, coordinating, and cooperating to perform tasks together. Their collaborative behavior often involves negotiation, sharing information, allocating tasks, and adapting to others' actions.

For example, multi-agent systems in healthcare can have agents specializing in specific tasks like diagnosis, preventive care, medicine scheduling, etc., for holistic patient care automation.

## What are the benefits of using AI agents?

AI agents can improve your business operations and your customers' experiences.

### Improved productivity

Business teams are more productive when they delegate repetitive tasks to AI agents. This way, they can divert their attention to mission-critical or creative activities, adding more value to their organization.

### Reduced costs

Businesses can utilize intelligent agents to minimize unnecessary costs resulting from process inefficiencies, human errors, and manual processes. They can confidently tackle complex tasks because autonomous agents follow a consistent model that adapts to changing environments. Agent technology automating business processes can lead to significant cost savings.

### Informed decision-making

Advanced intelligent agents have predictive capabilities and can collect and process massive amounts of real-time data. This enables business managers to make more informed predictions at speed when strategizing their next move. For example, you can use AI agents to analyze product demands in different market segments when running an ad campaign.

### Improved customer experience

Customers seek engaging and personalized experiences when interacting with businesses. Integrating AI agents allows businesses to personalize product recommendations, provide prompt responses, and innovate to improve customer engagement, conversion, and loyalty. AI agents can provide detailed responses to complex customer questions and resolve challenges more efficiently.

## What are the key components of AI agent architecture?

An AI agent architecture contains the following key components.

### Foundation model

At the core of any AI agent lies a foundation or large language model (LLM) such as GPT or Claude. It enables the agent to interpret natural language inputs, generate human-like responses, and reason over complex instructions. The LLM acts as the agent's reasoning engine, processing prompts and transforming them into actions, decisions, or queries to other components (e.g., memory or tools). It retains some memory across sessions by default and can be coupled with external systems to simulate continuity and context awareness.

### Planning module

The planning module enables the agent to break down goals into smaller, manageable steps and sequence them logically. This module employs symbolic reasoning, decision trees, or algorithmic strategies to determine the most effective approach for achieving a desired outcome. It can be implemented as a prompt-driven task decomposition or more formalized approaches, such as Hierarchical Task Networks (HTNs) or classical planning algorithms. Planning allows the agent to operate over longer time horizons, considering dependencies and contingencies between tasks.

### Memory module

The memory module allows the agent to retain information across interactions, sessions, or tasks. This includes both short-term memory, such as chat history or recent sensor input, and long-term memory, including customer data, prior actions, or accumulated knowledge. Memory enhances the agent’s personalization, coherence, and context-awareness. When building AI agents, developers use vector databases or knowledge graphs to store and retrieve semantically meaningful content.

### Tool integration

AI agents often extend their capabilities by connecting to external software, APIs, or devices. This allows them to act beyond natural language, performing real-world tasks such as retrieving data, sending emails, running code, querying databases, or controlling hardware. The agent identifies when a task requires a tool and then delegates the operation accordingly. Tool use is typically guided by the LLM through planning and parsing modules that format the tool call and interpret its output.

### Learning and reflection

Reflection can occur in multiple forms:

* The agent evaluates the quality of its own output (e.g., did it solve the problem correctly?).
* Human users or automated systems provide corrections.
* The agent selects uncertain or informative examples to improve its learning.

Reinforcement Learning (RL) is a key learning paradigm. The agent interacts with an environment, receives feedback in the form of rewards or penalties, and learns a policy that maps states to actions for maximum cumulative reward. RL is especially useful in environments where explicit training data is sparse, such as robotics, gaming, or financial trading. The agent balances exploration (trying new actions) and exploitation (using known best actions) to improve its strategy over time.

## How does an AI agent work?

AI agents work by simplifying and automating complex tasks. Most autonomous agents follow a specific workflow when performing assigned tasks.

### Determine goals

The AI agent receives a specific instruction or goal from the user. It uses the goal to plan tasks that make the final outcome relevant and useful to the user. Then, the agent breaks down the goal into several smaller, actionable tasks. To achieve the goal, the agent performs those tasks based on specific orders or conditions.

### Acquire information

AI agents require information to execute tasks they have planned successfully. For example, the agent must extract conversation logs to analyze customer sentiments. As such, AI agents might access the internet to search for and retrieve the information they need. In some applications, an intelligent agent can interact with other agents or machine learning models to access or exchange information.

### Implement tasks

With sufficient data, the AI agent methodically implements the task at hand. Once it accomplishes a task, the agent removes it from the list and proceeds to the next one. Between task completions, the agent evaluates whether it has achieved the designated goal by seeking external feedback and inspecting its own logs. During this process, the agent may create and act on additional tasks to achieve the final outcome.

## What are the types of AI agents?

Organizations create and deploy AI agents across a range of types and tasks. We share some examples below.

### Simple reflex agents

A simple reflex agent operates strictly based on predefined rules and its immediate data. It will not respond to situations beyond a given event, condition, and action rule. Hence, these agents are suitable for simple tasks that don’t require extensive training. For example, you can use a simple reflex agent to reset passwords by detecting specific keywords in a user’s conversation.

### Model-based reflex agents

A model-based agent is similar to simple reflex agents, except that it has a more advanced decision-making mechanism. Rather than merely following a specific rule, a model-based agent evaluates probable outcomes and consequences before making a decision. Using supporting data, it builds an internal model of the world it perceives and uses that to support its decisions.

### Goal-based agents

Goal-based agents, also known as rule-based agents, are AI agents that possess more robust reasoning capabilities. Besides evaluating the environment data, the agent compares different approaches to help it achieve the desired outcome. Goal-based agents always choose the most efficient path. They are suitable for performing complex tasks, such as natural language processing (NLP) and robotics applications.

### Utility-based agents

A utility-based agent employs a complex reasoning algorithm to assist users in maximizing the outcome they desire. The agent compares different scenarios and their respective utility values or benefits. Then, it selects one that offers users the most rewards. For example, customers can use a utility-based agent to search for flight tickets with the minimum travel time, regardless of the price.

### Learning agents

A learning agent continually learns from past experiences to enhance its performance. Using sensory input and feedback mechanisms, the agent adapts its learning element over time to meet specific standards. Additionally, it utilizes a problem generator to design new tasks that train itself using collected data and past results.

### Hierarchical agents

Hierarchical agents are an organized group of intelligent agents arranged in tiers. Higher-level agents decompose complex tasks into smaller ones and assign them to lower-level agents. Each agent runs independently and submits a progress report to its supervising agent. The higher-level agent collects the results and coordinates subordinate agents to ensure they collectively achieve goals.

### Multi-agent systems

A multi-agent system (MAS) consists of multiple agents that interact with one another to solve problems or achieve shared objectives. These agents can be homogeneous (similar in design) or heterogeneous (different in structure or function) and may collaborate, coordinate, or even compete depending on the context. MAS are particularly effective in complex, distributed environments where centralized control is impractical.

For example, in autonomous vehicle fleets, each vehicle acts as an independent agent but collaborates with others to avoid traffic congestion and prevent collisions, leading to smoother traffic flow.

## What are the challenges of using AI agents?

AI agents are helpful software technologies that automate business workflows to achieve better outcomes. That being said, organizations should address the following concerns when deploying autonomous AI agents for business use cases.

### Data privacy concerns

Developing and operating advanced AI agents requires acquiring, storing, and moving massive volumes of data. Organizations should be aware of data privacy requirements and employ necessary measures to improve their data security posture.

### Ethical challenges

In certain circumstances, AI models may produce results that are biased or inaccurate. Applying safeguards, such as human reviews, helps to ensure customers receive helpful and fair responses from the agents deployed.

### Technical complexities

Implementing advanced AI agents requires specialized experience and knowledge of machine learning technologies. Developers must be able to integrate machine learning libraries with software applications and train the agent with enterprise-specific data.

### Limited compute resources

Training and deploying deep learning AI agents require substantial computing resources. When organizations implement these agents on-premise, they must invest in and maintain costly infrastructure that is not easily scalable.

## How can AWS help with your AI agent requirements?

[Amazon Bedrock](https://aws.amazon.com/bedrock/) is a fully managed service that provides easy access to industry-leading generative AI models, such as Claude, Llama 2, and Amazon Titan, along with a broad set of capabilities needed to build generative AI applications.

[Amazon Bedrock Agents](https://aws.amazon.com/bedrock/agents/) use the reasoning of FMs, APIs, and data to break down user requests, gather relevant information, and efficiently complete tasks. Building an agent is straightforward and fast, with setup in just a few steps. Amazon Bedrock supports:

* Memory retention for seamless task continuity
* Multi-agent collaboration to build multiple specialized agents under the coordination of a supervisor agent
* [Amazon Bedrock Guardrails](https://aws.amazon.com/bedrock/guardrails/) for built-in security and reliability.

AWS has introduced an [open-source toolkit](https://aws-samples.github.io/amazon-bedrock-agents-healthcare-lifesciences/) with a growing catalog of starter agents purpose-built for healthcare and life sciences use cases.

[AWS Transform](https://aws.amazon.com/transform/) is the first agentic AI service for transforming .NET, mainframe, and VMware workloads. Built on 19 years of migration experience, it deploys specialized AI agents to automate complex tasks like assessments, code analysis, refactoring, decomposition, dependency mapping, validation, and transformation planning. It helps organizations to simultaneously modernize hundreds of applications while maintaining quality and control.

[Amazon Q Business](https://aws.amazon.com/q/business/) is a generative AI-powered assistant designed to help you find information, gain insights, and take action at work. It puts the power of AI agent creation in the hands of every employee. Anyone can use it to create lightweight agentic AI apps that interact with common enterprise software and automate repetitive tasks.

Get started with AI agents on AWS by [creating a free account](https://signin.aws.amazon.com/signin/) today

## APPENDIX B Architecture Diagram

Architecture Diagram - https://aws.amazon.com/what-is/architecture-diagramming/

## What is architecture diagramming?

Architecture diagramming is the process of creating visual representations of software system components. In a software system, the term *architecture *refers to various functions, their implementations, and their interactions with each other.  As software is inherently abstract, architecture diagrams visually illustrate the various data movements within the system. They also highlight how the software interacts with the environment around it.

![](https://d1.awsstatic.com/Figure_1.93fa4ba70e5cdbb38a36e99c003eda006e5f4821.png)

## What are the benefits of architecture diagramming?

Architecture diagrams provide several benefits, such as collaboration, risk reduction, efficiency, and scalability.

### **Collaboration**

Architecture diagrams significantly boost collaboration between developers and designers and create a unified view of system functionality and potential issues. A shared understanding of a system, application, or website has several benefits. It supports communication during the design process, helps teams develop effective system software components, and ensures a project meets its goals.

### **Risk reduction**

Architecture diagrams identify potential system development risks, such as incorrect assumptions, faulty logic, or inadequate testing. By identifying and addressing risks early in the software development lifecycle, development teams can make changes earlier and reduce the risk that significant issues appear later.

[Read about the software development lifecycle »](https://aws.amazon.com/what-is/sdlc/)

### **Efficiency**

Architecture diagrams provide a clear view of system components and structure. So, stakeholders can identify problems accurately and resolve them quickly. Diagrams also make it easier to maintain and scale systems, so ongoing changes are more efficient.

### **Scalability**

Architecture diagrams allow stakeholders to identify efficient ways to scale a system.  For example, a diagram may show if a system's architecture is centralized or distributed. Since distributed components scale more efficiently, monolithic components can be updated or replaced in time. Similarly, graphical representations provide insight into how data is stored and moved. Stakeholders can identify potential bottlenecks and ways to avoid them.

## What software architecture patterns can you represent with architecture diagramming?

Software architecture patterns are design principles and best practices used to develop software systems. They provide a framework to structure the software and address specific challenges in complex software architectures.

Here are some of the most commonly used software architecture patterns.

### **Client-server architecture**

Client-server architecture is a distributed application structure that separates tasks and workloads between servers and clients. Servers provide the resource or service, and clients request it.

The client and server are separate programs that communicate over a network. A web browser and web server are an example of client-server architecture. It’s a commonly used architecture in distributed computing.

[Read about distributed computing »](https://aws.amazon.com/what-is/distributed-computing/)

### **Service-oriented architecture**

Service-oriented architecture allow for interaction between distributed application components through services. Services are abstract, loosely coupled, and language-independent. Applications access them through interfaces. Developers can reuse existing services rather than having to rebuild from scratch. Service-oriented architecture is widely used in distributed systems, as services can be deployed across multiple servers.

[Read about service-oriented architecture »](https://aws.amazon.com/what-is/service-oriented-architecture/)

### **Microservices architecture**

Service-oriented architecture has evolved further, so developers use microservices architecture to build, deploy, and manage individual services. Applications are split into independently deployable services that communicate through APIs.

Smaller, independent services make it simpler for developers to develop, test, and deploy applications and deliver improved fault tolerances and rapid scaling. An example of a microservices architecture is a web application consisting of several independent services, each responsible for specific tasks.

[Read about microservices »](https://aws.amazon.com/microservices/)

[Read about APIs »](https://aws.amazon.com/what-is/api/)

[Read about web applications »](https://aws.amazon.com/what-is/web-application/)

### **Cloud-centered architecture**

Cloud-centered architecture is used to design and build applications for cloud environments. Cloud-centered architecture is built and delivered with cloud-specific technologies such as containers, microservices, DevOps, and serverless computing. It prioritizes automated deployment and management so that applications can be scaled up and down as needed.

[Read about containerization »](https://aws.amazon.com/what-is/containerization/)

[Read about DevOps »](https://aws.amazon.com/devops/what-is-devops/)

[Read about serverless architectures »](https://aws.amazon.com/lambda/serverless-architectures-learn-more/)

### **Event-driven architecture**

Event-driven architecture is software architecture based on the production, detection, and consumption of events. User interactions, background tasks, and other sources trigger events that further trigger other functionality. The event-driven architecture allows applications to be more responsive to changes in a software system and its environment.

### **Layered architecture**

Layered architecture is a software architecture pattern that separates applications into logic-based layers. This type of architecture is designed to simplify complex applications and systems, as you can split tasks between layers.

Layers are organized from top to bottom:

* A presentation layer (for example, a UI) is at the top
* A business layer is in the middle
* A data layer is at the bottom

Layers can also be structured hierarchically, which aids maintenance and scalability.

![](https://d1.awsstatic.com/bdb809-build-a-lake-house-2_1.7233066b71353d85b228083cedfb58192334769e.png)

## What types of information are included in an architecture diagram?

Here are some common types of information found in an architecture diagram:

* Squares and circles represent components such as databases, networks, applications, and services
* Lines and arrows show the connections and interactions between the system's components
* Labels provide additional information about the components and connections

Additionally, the diagram may also use icons or symbols to visually represent the different components. A small legend at the bottom, similar to the legend on a map, explains icon usage. The way in which the components and connections are arranged is called a  *layout* .

[Read about databases »](https://aws.amazon.com/what-is/database/)

[Read about computer networking »](https://aws.amazon.com/what-is/computer-networking/)

## What are the types of architectural diagrams?

Several types of architectural diagrams visually represent various systems and software architectures. Here are some of the most common architecture diagram examples.

### **Software architecture diagram**

Software architecture diagrams visually represent software components, relationships, and system interactions. They document, analyze, and communicate software design and are used to make decisions on implementation. These diagrams range from straightforward, high-level diagrams to detailed depictions of software component interactions.

### **System architecture diagram**

System architecture diagrams provide a visual illustration of a system's various components and show how they communicate and interact with each other. These diagrams document a system's structure and architecture. This allows for a clear understanding of how the system works and how it can be improved.

### **Application architecture diagram**

Application architecture diagrams illustrate application structure. They include components and how they interact with each other as well as the data flow between them. Application architecture diagrams provide a complete view of an application and are used to inform the application's design, implementation, and maintenance.

![](https://d1.awsstatic.com/AWS_L2_DT_EV_Architecture.7035ffd064569e5bce8e05e06b7e10ab363927b3.jpg)

### **Integration architecture diagram**

Integration architecture diagrams visually represent components, data, and technology involved in integration solutions. They show the relationships between different components, systems, and services and are used to help design, develop, and manage complex integration solutions. These diagrams are used to document and explain existing systems as well as plan and develop new integration solutions.

### **Deployment architecture diagram**

Deployment architecture diagrams visually represent relationships between different application components and their deployment environments. Deployment architecture diagrams show the layout of an application and its components—including, for example, servers, storage, and networks. They’re used to plan capacity, scalability, and fault tolerance.

### **DevOps architecture diagram**

DevOps architecture diagrams visualize the components of a DevOps system and how they interact. They commonly include components such as development environments, continuous integration and continuous delivery pipelines, infrastructure as code, and cloud services. The diagrams illustrate the components' interactions and places in the wider DevOps environment.

[Read about infrastructure as a service »](https://aws.amazon.com/what-is/iaas/)

### **Website architecture diagram**

Website architecture diagrams visually represent website structures. The diagrams visually map the relationships and interactions between website components, such as webpages, databases, and content management systems. Web designers with access to website architecture diagrams can identify potential problem areas and develop effective strategies to improve the website's performance.

## How can AWS support your architecture diagramming requirements?

At Amazon Web Services (AWS), we offer [Workload Discovery on AWS](https://aws.amazon.com/solutions/implementations/workload-discovery-on-aws/) as a tool to visualize AWS Cloud workloads. You can use it to build, customize, and share detailed architecture diagrams of your workloads based on live data from AWS. Workload Discovery on AWS removes significant documentation process overheads by providing both the data and the visualization tooling in one place.

Here are ways you can benefit from Workload Discovery on AWS:

* Build, customize, and share detailed architecture diagrams
* Save and export architecture diagrams
* Query AWS cost and usage reports
* Search and locate basic information, such as resource names, tag names, or IP addresses
* Explore account resources and AWS Regions by using the resource directory

Get started with architecture diagramming on AWS by [creating a free AWS account ](https://portal.aws.amazon.com/gp/aws/developer/registration/index.html)today.
