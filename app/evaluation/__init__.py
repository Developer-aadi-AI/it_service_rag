"""RAG evaluation: dataset, legacy metrics and the repeatable harness.

    python -m app.evaluation                 # full harness, writes reports/evaluation/
    python -m app.evaluation --check         # non-zero exit if any metric is below its threshold
"""
from app.evaluation.legacy import EVAL_PATH, evaluate_pipeline, evaluate_retrieval, load_eval_set  # noqa: F401
