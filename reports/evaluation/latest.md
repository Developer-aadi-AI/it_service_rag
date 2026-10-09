# Evaluation report - 2026-10-09T19:12:49+00:00

Mode: `extractive` | dataset v2 | 80 questions | mean latency 979.1 ms (p95 3174.2 ms) | 116.1 s

Answer generators: {'extractive': 74, 'contact-info': 3, 'triage-rules': 3, 'rules': 12, 'llm_answer_share': 0.0}

| Area | Metric | Value |
|---|---|---|
| Retrieval (47) | hit@5 / MRR / precision@5 | 1.0 / 0.956 / 0.294 |
| Answers (47) | answer rate / fact recall / citation accuracy | 1.0 / 0.872 / 0.936 |
| Grounding | supported sentences / fully grounded answers / invalid citations | 1.0 / 1.0 / 0 |
| - factual (29) | answer / fact / citation / all facts | 1.0 / 0.862 / 0.931 / 0.828 |
| - service (13) | answer / fact / citation / all facts | 1.0 / 0.846 / 0.923 / 0.846 |
| - multi_chunk (5) | answer / fact / citation / all facts | 1.0 / 1.0 / 1.0 / 0.6 |
| Follow-ups (6) | fact recall / citation / rewritten | 1.0 / 1.0 / 1.0 |
| No-answer (8) | routed to team / correct request kind | 1.0 / 0.875 |
| Hallucination probes (7) | resistance | 1.0 |
| Off-topic (12) | accuracy | 1.0 |
| Ambiguous (6) | acceptable handling | 1.0 |

## Threshold checks

- PASS `retrieval.hit_at_k` = 1.0 (threshold 0.9)
- PASS `retrieval.mrr` = 0.956 (threshold 0.75)
- PASS `answers.answer_rate` = 1.0 (threshold 0.85)
- PASS `answers.fact_recall` = 0.872 (threshold 0.75)
- PASS `answers.citation_accuracy` = 0.936 (threshold 0.8)
- PASS `answers.grounding_rate` = 1.0 (threshold 0.9)
- PASS `answers.invalid_citations` = 0 (threshold 0)
- PASS `follow_up.fact_recall` = 1.0 (threshold 0.6)
- PASS `no_answer.routed_to_team` = 1.0 (threshold 0.8)
- PASS `hallucination.resistance` = 1.0 (threshold 0.9)
- PASS `off_topic.accuracy` = 1.0 (threshold 0.9)
- PASS `ambiguous.acceptable` = 1.0 (threshold 0.8)

## Failures (6)

- **factual** - Which brands has D Group worked with?: cited page does not contain the answer (cited=['db-66'], answer=We offer what fits best for your brand. [1]

Is there anything else you'd like to know?)
- **factual** - What did you build for the New York State Education Department?: expected fact missing from answer (expected=['NYSED', 'New York State Education Department'], answer=Its digital presence serves millions of residents, educators, professionals, and institutions through a large multi department governance structure with centralized administrative oversight. The engag)
- **factual** - How much is the Shopify Gold plan in GBP?: cited page does not contain the answer (cited=['db-1859'], answer=The Shopify packages price varies based on the plan you have. Our Silver Plan begins at $3,999 and Gold Plan begins at $5,999. Both the plans are one-time charges and include design, development, and )
- **factual** - Which partner certifications does D Group's team have?: expected fact missing from answer (expected=['Acquia', 'Shopify Partner', 'Shopify Expert'], answer=With D Group's Global Partner Program, you gain not just expertise in digital transformation but a dedicated team that helps you evolve, optimize, and commercialize services with confidence. [3] D Gro)
- **service** - Can I hire a PHP developer from D Group?: expected fact missing from answer (expected=['PHP'], answer=D Group is here to assist you whether you are creating a new application, modifying an existing one, or trying to scale. You can rely on certified and seasoned experts at D Group. Finding the right fi)
- **service** - What platforms does D Group work with?: cited page does not contain the answer (cited=['db-298'], answer=D Group develops hybrid solutions that work effortlessly across iOS, Android, and Windows platforms. [3]

Let me know if you'd like more details.)
