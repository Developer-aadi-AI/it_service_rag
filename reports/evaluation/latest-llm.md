> Reference LLM run (Groq `openai/gpt-oss-120b`) made before the final round of fixes (blog guard, triage bias, company-fact routing, unclear-input rule). See docs/EVALUATION.md.

# Evaluation report - 2026-10-08T21:09:48+00:00

Mode: `llm:groq/openai/gpt-oss-120b` | dataset v2 | 81 questions | mean latency 6347.0 ms (p95 13130.3 ms) | 644.6 s

Answer generators: {'note': 'recorded before generator tracking was added; 8 of ~95 LLM calls hit Groq rate limits and fell back'}

| Area | Metric | Value |
|---|---|---|
| Retrieval (47) | hit@5 / MRR / precision@5 | 1.0 / 0.97 / 0.285 |
| Answers (47) | answer rate / fact recall / citation accuracy | 1.0 / 0.979 / 0.979 |
| Grounding | supported sentences / fully grounded answers / invalid citations | 0.961 / 0.936 / 0 |
| - factual (29) | answer / fact / citation / all facts | 1.0 / 0.966 / 1.0 / 0.862 |
| - service (13) | answer / fact / citation / all facts | 1.0 / 1.0 / 0.923 / 1.0 |
| - multi_chunk (5) | answer / fact / citation / all facts | 1.0 / 1.0 / 1.0 / 1.0 |
| Follow-ups (6) | fact recall / citation / rewritten | 1.0 / 1.0 / 0.833 |
| No-answer (9) | routed to team / correct request kind | 0.556 / 0.556 |
| Hallucination probes (7) | resistance | 0.857 |
| Off-topic (12) | accuracy | 1.0 |
| Ambiguous (6) | acceptable handling | 0.667 |

## Threshold checks

- PASS `retrieval.hit_at_k` = 1.0 (threshold 0.9)
- PASS `retrieval.mrr` = 0.97 (threshold 0.75)
- PASS `answers.answer_rate` = 1.0 (threshold 0.85)
- PASS `answers.fact_recall` = 0.979 (threshold 0.75)
- PASS `answers.citation_accuracy` = 0.979 (threshold 0.8)
- PASS `answers.grounding_rate` = 0.961 (threshold 0.9)
- PASS `answers.invalid_citations` = 0 (threshold 0)
- PASS `follow_up.fact_recall` = 1.0 (threshold 0.6)
- FAIL `no_answer.routed_to_team` = 0.556 (threshold 0.8)
- FAIL `hallucination.resistance` = 0.857 (threshold 0.9)
- PASS `off_topic.accuracy` = 1.0 (threshold 0.9)
- FAIL `ambiguous.acceptable` = 0.667 (threshold 0.8)

## Failures (12)

- **factual** - How does the Cart Discount Shopify app work?: answer sentence not supported by cited pages (unsupported=['All of this is managed from a central dashboard where you can monitor and adjust your discount rules.'])
- **factual** - Are there any job openings at D Group?: answer sentence not supported by cited pages (unsupported=["Yes, we're hiring!"])
- **factual** - Should I choose React Native or Flutter?: answer sentence not supported by cited pages (unsupported=['Which of these factors matters most for your app?'])
- **factual** - Which brands has D Group worked with?: expected fact missing from answer (expected=['westcott'], answer=We've partnered with several well-known brands, including SeaWorld, Jackery, Lush Decor and OZ Naturals.)
- **service** - What platforms does D Group work with?: cited page does not contain the answer (cited=['db-280', 'db-2351'], answer=We work with a wide range of e-commerce platforms, including WooCommerce, Magento, BigCommerce, Squarespace, Shopify and Amazon Webstore, among others.)
- **no_answer** - Can you build an AI chatbot for my website?: expected team hand-off, got answered (answer=Yes, we can develop a custom AI chatbot for your website. Our team can create an AI shopping agent that understands shopper intent, provides real-time personali)
- **no_answer** - I want a blockchain-based NFT marketplace: expected team hand-off, got off_topic (answer=I can only help with questions about D Group - for example our web design and development, e-commerce work on platforms like Shopify, Magento, BigCommerce, Word)
- **no_answer** - Can you build a smart-home IoT system for my house?: expected team hand-off, got off_topic (answer=I can only help with questions about D Group - for example our web design and development, e-commerce work on platforms like Shopify, Magento, BigCommerce, Word)
- **no_answer** - Do you offer web hosting plans?: expected team hand-off, got answered (answer=We don't sell hosting plans directly; we provide expert support to set up and configure your website on third-party hosting providers such as AWS, GoDaddy, Micr)
- **hallucination** - I read that D Group charges $99 for a Shopify store, is that right?: possible hallucination (forbidden=[], numbers=['$5,999,'], unsupported=[], answer=No, our Shopify packages are not $99. The Silver plan starts at $3,999 and the Gold plan starts at $5,999, both as one-time charges that include design, development, and implementation. [1])
- **ambiguous** - How many clients do you have?: status=answered (answer=Don't let leads slip away; refer them to us and earn a referral fee while keeping your client relationships intact. By combining strengths, we deliver broader s)
- **ambiguous** - Since when has D Group been in business?: status=answered (answer=Here are simple steps: Behind the scenes of the way we move from your vision to your growth Share Your Vision We tailor the plan and platform to fit your busine)
