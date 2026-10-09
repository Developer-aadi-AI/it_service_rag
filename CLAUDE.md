# CLAUDE.md

## Project: D Group AI Customer Support & Sales Assistant

### Goal
Build an AI-powered customer support and sales chatbot for D Group using the provided D Group website/WordPress data as the knowledge source. The system should provide grounded RAG answers and support multi-turn conversations, service recommendations, lead capture, contact/support requests, sentiment-aware responses, session management, transcripts, email follow-ups, feedback, logging, and analytics.

### Source of Truth
The user will provide the project data to the agents.
- Inspect the supplied data before implementing retrieval or answer logic.
- Do not invent D Group services, policies, pricing, FAQs, or business facts.
- Preserve useful source metadata.
- If the supplied data does not contain an answer, handle it as a no-answer case rather than hallucinating.
- Use the supplied data as the source of truth for business knowledge.

### Expected Knowledge-Base Fields
The project plan identifies:
- service
- category
- description
- keywords
- CTA
- source_type
- platform

Additional fields may exist; do not discard useful fields without a reason.

### Target Architecture
User → Chat Interface → FastAPI → Session/Conversation Management → Query Processing → Retriever/Vector DB → Relevant Context → LLM → Grounded Response + Sources → Lead/Contact/Logging/Analytics/Email workflows.

Keep components modular so the LLM, embedding model, vector database, email provider, and UI can be changed independently.

### RAG
Implement:
- data loading/validation
- chunking
- metadata tagging
- embeddings
- vector storage
- similarity search
- configurable top-k
- configurable similarity threshold
- context construction
- grounded prompting
- source/citation output
- no-answer handling
- off-topic handling

The retrieval layer must be independently testable.

### Conversation
Support:
- multi-turn conversations
- session memory
- follow-up questions
- context-aware answers
- primary service recommendations
- secondary/related service recommendations
- graceful closing

Keep session state controlled and configurable.

### Sentiment
Adapt responses to user sentiment. For frustrated/negative users, acknowledge the frustration naturally, reassure where appropriate, provide useful help, and ask clarifying questions when needed. Avoid robotic or excessive empathy.

### Irrelevant Inputs
Detect repeated irrelevant/unclear inputs. Inform the user that the question is outside supported D Group information, track attempts, show remaining attempts, and end the session after 3 irrelevant user inputs. Make the limit configurable.

### Idle Sessions
Implement an inactivity flow:
1. Send an "Are you still there?" / connection check.
2. Wait for the configured timeout.
3. If there is still no response, end the session.
4. Explain that the session ended because the user was unavailable.
5. Send/follow up by email where configured.

Do not scatter hard-coded timeout values throughout the code.

### Lead / Contact Capture
Provide a chat contact form for users who want to connect with marketing/support. After submission, confirm it, say the relevant team will connect shortly, and ask whether the user needs anything else. If no, close politely; if yes, continue.

Validate contact data before storage.

### Transcript / Email
Support:
- conversation transcripts
- UTC timestamps
- date-friendly transcript header
- `.txt` transcript output
- transcript email
- conversation summary email
- follow-up/feedback email
- simple feedback such as Great / OK / Poor

Do not expose internal implementation details in customer-facing emails.

### Chat Notifications
Support notification sounds for sent/received messages and a sound enable/disable control. This should not affect backend behavior.

### FastAPI Backend
Use FastAPI. Keep these concerns separated:
- API routes
- RAG/retrieval
- LLM
- embeddings
- vector store
- session management
- recommendations
- lead/contact handling
- sentiment
- email
- transcript
- logging
- analytics
- configuration

The `/ask` endpoint should wrap the core RAG/chat pipeline. Use environment variables for secrets; never hard-code keys.

### Logging / Analytics
Track useful product and debugging metrics such as:
- top questions
- no-answer rate
- service recommendations
- session statistics
- lead/contact submissions
- feedback results
- errors/failures

Avoid unnecessary sensitive-data logging.

### Testing
Test:
- retrieval quality
- RAG grounding
- citation correctness
- no-answer cases
- off-topic cases
- multi-turn conversations
- recommendations
- sentiment behavior
- irrelevant-input limits
- idle sessions
- contact submission
- transcript generation
- email workflow
- API edge cases

Build an evaluation set from the supplied D Group data. Do not measure quality only by HTTP 200 responses.

### Code Quality
Prioritize correctness, grounded answers, maintainability, clear architecture, testability, security, and performance. Inspect the repository before changing it. Prefer simple modular solutions. Use type hints where useful, centralized configuration, and explicit error handling.

### Security
Never commit API keys, passwords, email credentials, database credentials, or private tokens. Use `.env` and provide `.env.example`. Validate inputs and never expose secrets, stack traces, internal prompts, or internal metadata to users.

### Agent Workflow
1. Inspect the existing repository.
2. Inspect all supplied project data relevant to the task.
3. Understand the current architecture.
4. Make a short implementation plan.
5. Implement incrementally.
6. Run tests after meaningful changes.
7. Fix regressions before moving on.
8. Update documentation.
9. Do not fabricate missing requirements.
10. If requirements conflict with the project documentation, flag the conflict instead of silently choosing.

### Definition of Done
The complete system should support supplied-data ingestion, relevant RAG retrieval, grounded answers with sources, multi-turn memory, recommendations, lead/contact capture, sentiment-aware responses, no-answer/off-topic handling, idle-session handling, transcripts, email follow-up, feedback, logging, analytics, tested/documented FastAPI endpoints, and a working end-to-end chat → API → RAG → answer → citation → lead/contact flow.

### Build in Three Phases
Phase 1: Data → RAG → FastAPI → grounded chatbot foundation.
Phase 2: Conversation UX → memory → recommendations → sentiment → lead/contact → session handling → email/transcript.
Phase 3: Testing → evaluation → analytics → hardening → documentation → deployment/demo readiness.
