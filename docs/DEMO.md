# Demo guide

Ten scripted conversations that show every capability. All questions use facts that exist in the supplied
D Group data (`Database/final_dataset.json`); expected answers below are quoted from it.

**Before the demo:** `python -m app.demo` runs all ten scenarios against the real system and checks the expected
behaviour (exit code 0 = all pass). A recorded offline run is in [DEMO_RUN.md](DEMO_RUN.md).

**Live:** `uvicorn app.main:app` then open http://127.0.0.1:8000/widget/ (or the Gradio app in the notebook).
For scenario 8 start the server with short idle timings:
`IDLE_CHECK_AFTER_SECONDS=20 IDLE_CLOSE_AFTER_SECONDS=15 uvicorn app.main:app`.

| # | Scenario | Type in the widget | What to point out |
|---|---|---|---|
| 1 | Service question | "Do you build mobile apps for iOS and Android?" | Grounded answer ("custom mobile apps for iOS, Android, and even Windows platforms") with a source link to the App Development page |
| 2 | Follow-up | "How much does the Shopify Gold plan cost?" → "And how long does it take to deliver?" | $5,999; then "it" is resolved from memory → "Silver in 15 business days and Gold in 25 business days" |
| 3 | Recommendation | "I need a mobile app for my business, what do you recommend?" | Primary: Mobile App Development Services; related: Hire Android Developers, Shopify App Development (all real pages) |
| 4 | Frustrated user | "This is so frustrating - how many support hours does Annual Support include?" → "Ugh, this is ridiculous. What does it cost?" | One short acknowledgement, then the answer (120 hours; $3,500 per year); on repeated frustration the support team is offered with the form |
| 5 | No answer | "I want a blockchain-based NFT marketplace" | Never "I don't know": "That sounds like a great idea!" + contact form |
| 6 | Irrelevant | "What is the weather in Paris today?" → "Tell me a joke" → "Who won the 2022 world cup?" | "2 more attempts", "1 more attempt", then the chat closes politely |
| 7 | Lead capture | "How much does the BigCommerce Silver plan cost?" → "I want to talk to your sales team" → fill the form → "No thanks" | $3,999; form; "Thank you, …! … the relevant D Group team will connect with you shortly. Is there anything else…?"; polite close + summary email |
| 8 | Timeout | Ask "What is SMTU?" and wait | "Are you still there?" → chat closes and explains why → follow-up email (see `storage/outbox`) |
| 9 | Transcript & email | "What is included in Annual Support?" → *Transcript* button / *Email transcript* | `.txt` with date header and UTC timestamps; email with the transcript attached |
| 10 | Feedback | "What is ReviewCaddy?" → "bye" → click Great | Feedback buttons; rating stored; visible in `python -m app.analytics` |

After the demo: `python -m app.analytics --days 1` shows sessions, top questions, no-answer rate, recommendations,
leads and feedback from the demo itself.

## Notes for presenters

- With an LLM key configured the wording is generated (still grounded, with `[n]` citations); without one,
  answers quote the website. Both are demo-ready; the checks in `python -m app.demo` pass in offline mode.
- Data facts worth knowing: the website states both "journey started in 1999" (About) and "Since 2007"
  (Web Development), and client counts of 500+, 350+ and 300+ on different pages. The assistant quotes what
  the cited page says.
