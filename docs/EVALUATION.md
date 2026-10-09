# Evaluation

```bash
python -m app.evaluation                # full run, configured mode -> reports/evaluation/
python -m app.evaluation --check        # CI gate: exit 1 if any metric is below its threshold
python -m app.evaluation --pace 20      # space out questions for rate-limited LLM tiers
LLM_PROVIDER=extractive python -m app.evaluation   # force the offline mode
```

Each run writes `eval-<timestamp>.json`, `latest.md/json`, `latest-<offline|llm>.md/json`, and appends a summary
row to `history.csv` so you can compare runs after changes.

## Dataset (`tests/eval/eval_set.json`, v2)

Built only from the supplied data. Every expected fact is checked automatically against the text of its
expected pages (`test_dataset_facts_exist_in_data`).

| Category | Items | What is checked |
|---|---|---|
| factual | 29 | expected page retrieved, answer contains the fact, cites a page containing it |
| service | 13 | same, for "do you / can you" service questions |
| multi_chunk | 5 | needs facts from several chunks or pages; *all* facts must appear |
| follow_up | 6 conversations | multi-turn: the last turn must be answered correctly from memory |
| no_answer_in_domain | 8 | D Group-related but not in the data → team hand-off, correct request kind |
| hallucination_probes | 7 | false premises (Platinum plan, London office, $99 store), injection → no forbidden claim, no ungrounded number |
| off_topic | 12 | weather, jokes, gibberish, "???" → off-topic |
| ambiguous | 6 | vague or conflicting-data questions → allowed statuses, grounded, no invented numbers |

The changelog inside the file records label corrections made after checking the data. For example, "video
games" was moved out of the no-answer set because the App Development page states gaming expertise. Web
hosting was moved out because a blog post describes D Group's hosting set-up support.

## Metrics

- **Retrieval:** hit@5, MRR, precision@5 and multi-chunk page recall. Retrieval is tested independently of the LLM.
- **Correctness:** answer rate, and fact recall (an expected fact appears in the answer). Multi-chunk questions report all-facts completeness.
- **Grounding:** the share of answer sentences whose content words appear in the cited pages. Fixed replies, closings and acknowledgements are excluded. This is a lexical approximation.
- **Citations:** the answer cites an expected page, and there are zero `[n]` markers pointing to sources that don't exist.
- **No-answer behaviour:** team hand-off rate, and correct `project_request` / `information_request` classification.
- **Hallucination resistance:** no forbidden claims, no numbers missing from the cited pages, and no unsupported sentences.
- **Latency:** mean and p95 per question.
- **Generator share:** the share of answers actually produced by the LLM. This exposes runs where provider failures forced the offline fallback.

## Results

### Offline mode (no LLM), final code: all thresholds pass

| Area | Result |
|---|---|
| Retrieval (47) | hit@5 **1.0** · MRR **0.956** |
| Answers (47) | answer rate **1.0** · fact recall **0.872** · citation accuracy **0.936** |
| Grounding | supported sentences **1.0** · invalid citations **0** |
| by category | factual 0.862 / 0.931 (fact / citation) · service 0.923 / 0.923 · multi-chunk 1.0 / 1.0, all facts 0.6 |
| Follow-ups (6) | fact recall **1.0** · citation 1.0 · rewritten 1.0 |
| No-answer (8) | routed to team **1.0** · request kind 0.875 |
| Hallucination probes (7) | resistance **1.0** |
| Off-topic (12) | accuracy **1.0** |
| Ambiguous (6) | acceptable **1.0** |
| Latency | mean 979 ms · p95 3.2 s (CPU, cold embedding cache after the index rebuild) |

### LLM mode (Groq `openai/gpt-oss-120b`), reference run before the final fixes

Of about 95 LLM calls, 8 hit rate limits and fell back. Report: `reports/evaluation/latest-llm.md`.

| Area | Result |
|---|---|
| Answers (47) | answer rate 1.0 · fact recall **0.979** · citation accuracy **0.979** · multi-chunk all facts **1.0** |
| Grounding | supported sentences 0.961 · invalid citations 0 |
| Follow-ups | fact recall **1.0** · citation 1.0 |
| No-answer | routed to team 0.556 (the LLM over-answered from blog posts and called some project ideas off-topic) |
| Hallucination | 0.857 (number-check artefact: "$5,999," with a trailing comma) |
| Ambiguous | 0.667 (blog prices presented as D Group prices) |

### LLM mode after the fixes: targeted re-run on the affected categories

Run on Groq `openai/gpt-oss-20b`, because the 120b model's daily token quota (200k TPD) was used up by the
earlier runs. 17 of 21 answers came from the LLM and 3 from the LLM triage.

| Area | Before | After |
|---|---|---|
| No-answer routed to team | 0.556 | **1.0** (request kind 1.0) |
| Hallucination resistance | 0.857 | **1.0** |
| Ambiguous acceptable | 0.667 | **0.833 → 1.0** after correcting one over-strict label ("over 350 clients" is right) |

Fixes that produced the improvement:

- **Blog guard.** Blog-only evidence can't confirm an offer or a price; the prompt rule is enforced in code.
- **Feasibility triage.** Project requests the LLM marked off-topic get a feasibility triage that leans toward the team.
- **Clarifying questions.** Vague questions get a clarifying question.
- **Unclear inputs.** Inputs without words go straight to off-topic.
- **Company facts.** Questions about the company itself retrieve from the About and Home pages.
- **Tolerant parsing.** Schema slips from smaller models are handled.
- **Number check.** The checker strips trailing punctuation from numbers.

## Interpreting and maintaining

- **CI gate.** Run `python -m app.evaluation --check` after any change to prompts, thresholds, chunking, embeddings or the LLM. Thresholds are in `app/evaluation/harness.py`. They're tuned so the offline mode passes, and an LLM should exceed them.
- **Lexical grounding.** Grounding and support checks are lexical, so a correct paraphrase such as "Yes, we're hiring!" can be flagged. Read `latest.md`'s failure list rather than treating the numbers as absolute.
- **Rate limits.** With a rate-limited LLM tier, use `--pace`, or the circuit breaker will route many answers offline. The `generators` line shows when that happened.
- **Data conflicts.** The data contains conflicting facts (office city, founding year, client counts). Questions about them are in the ambiguous set and accept any value the website states.
