#!/usr/bin/env python3
"""
Layer 3 — NLP scoring: language, tone/sentiment, hate/toxicity, readability.

Runs on local GPU when available. Writes post_scores to parquet + DuckDB.
Never writes to Postgres.

Domain caveat: war/terror corpus → toxicity models over-fire on reported violence.
Hate figures require Layer 5 calibration before publication.
"""
from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.config import DUCKDB_PATH, REPORTS_DIR
from lib.duck import connect, replace_table_from_df, write_parquet

READ_MIN_WORDS = 25
BATCH = 64

# Models (document in scores)
SENTIMENT_MODEL = "cardiffnlp/twitter-xlm-roberta-base-sentiment"
TOXICITY_MODEL = "unitary/multilingual-toxic-xlm-roberta"
# Fallback toxicity if above fails
TOXICITY_FALLBACK = "martin-ha/toxic-comment-model"


def word_count(text: str) -> int:
    return len(str(text).split())


def detect_lang(text: str) -> str | None:
    if not text or not str(text).strip():
        return None
    try:
        from langdetect import detect, LangDetectException

        return detect(str(text)[:2000])
    except Exception:
        return None


def flesch_kincaid_grade(text: str) -> float | None:
    try:
        import textstat

        return float(textstat.flesch_kincaid_grade(text))
    except Exception:
        return None


def flesch_reading_ease(text: str) -> float | None:
    try:
        import textstat

        return float(textstat.flesch_reading_ease(text))
    except Exception:
        return None


def lix_score(text: str) -> float | None:
    """LIX readability — more language-portable than Flesch."""
    words = re.findall(r"\w+", text, re.UNICODE)
    if len(words) < 10:
        return None
    sentences = max(1, len(re.findall(r"[.!?。！？]+", text)) or 1)
    long_words = sum(1 for w in words if len(w) > 6)
    return float(len(words) / sentences + 100 * long_words / len(words))


def score_readability(text: str, lang: str | None) -> dict:
    wc = word_count(text)
    if wc < READ_MIN_WORDS:
        return {
            "readability_score": None,
            "readability_metric": None,
            "readability_valid": False,
            "readability_reason": "too_short",
        }
    lang = (lang or "").lower()
    if lang in ("zh", "zh-cn", "zh-tw", "ja", "th", "my"):
        return {
            "readability_score": None,
            "readability_metric": None,
            "readability_valid": False,
            "readability_reason": "lang_not_applicable",
        }
    if lang in ("en", "en-us", "en-gb", ""):
        fk = flesch_kincaid_grade(text)
        if fk is not None:
            return {
                "readability_score": fk,
                "readability_metric": "flesch_kincaid_grade",
                "readability_valid": True,
                "readability_reason": None,
            }
    lix = lix_score(text)
    if lix is not None:
        return {
            "readability_score": lix,
            "readability_metric": "lix",
            "readability_valid": True,
            "readability_reason": None,
        }
    return {
        "readability_score": None,
        "readability_metric": None,
        "readability_valid": False,
        "readability_reason": "compute_failed",
    }


def load_sentiment_pipeline(device: int | str):
    from transformers import AutoModelForSequenceClassification, AutoTokenizer, pipeline

    tok = AutoTokenizer.from_pretrained(SENTIMENT_MODEL, use_fast=False)
    model = AutoModelForSequenceClassification.from_pretrained(SENTIMENT_MODEL)
    return pipeline(
        "sentiment-analysis",
        model=model,
        tokenizer=tok,
        device=device,
        truncation=True,
        max_length=256,
        top_k=None,
    )


def load_toxicity_pipeline(device: int | str):
    from transformers import AutoModelForSequenceClassification, AutoTokenizer, pipeline

    for model_id in (TOXICITY_MODEL, TOXICITY_FALLBACK):
        try:
            tok = AutoTokenizer.from_pretrained(model_id, use_fast=False)
            model = AutoModelForSequenceClassification.from_pretrained(model_id)
            pipe = pipeline(
                "text-classification",
                model=model,
                tokenizer=tok,
                device=device,
                truncation=True,
                max_length=256,
                top_k=None,
            )
            return pipe, model_id
        except Exception as e:
            print(f"  toxicity model {model_id} failed: {e}")
    return None, None


def sentiment_to_continuous(results) -> float:
    """Map multi-label sentiment outputs to [-1, 1]."""
    # results: list of {label, score}
    if not results:
        return 0.0
    # normalize labels
    scores = {}
    for r in results:
        lab = str(r["label"]).lower()
        scores[lab] = float(r["score"])
    # xlm-roberta-base-sentiment: positive/neutral/negative
    pos = scores.get("positive", scores.get("label_2", 0.0))
    neg = scores.get("negative", scores.get("label_0", 0.0))
    neu = scores.get("neutral", scores.get("label_1", 0.0))
    # continuous: pos - neg
    if pos or neg:
        return float(pos - neg)
    # single best label fallback
    best = max(results, key=lambda x: x["score"])
    lab = str(best["label"]).lower()
    if "pos" in lab:
        return float(best["score"])
    if "neg" in lab:
        return float(-best["score"])
    return 0.0


def toxicity_prob(results) -> float:
    if not results:
        return 0.0
    for r in results:
        lab = str(r["label"]).lower()
        if "toxic" in lab and "non" not in lab:
            return float(r["score"])
        if lab in ("label_1", "hate", "offensive"):
            return float(r["score"])
    # if binary with LABEL_0 = non-toxic
    for r in results:
        if str(r["label"]).upper() in ("LABEL_1", "TOXIC"):
            return float(r["score"])
    return float(max(results, key=lambda x: x["score"])["score"])


def main() -> int:
    if not DUCKDB_PATH.exists():
        print("ERROR: run 01_extract_base.py first")
        return 1

    import torch

    device = 0 if torch.cuda.is_available() else -1
    print(f"device={'cuda' if device == 0 else 'cpu'}")

    con = connect(read_only=False)
    posts = con.execute(
        "SELECT post_uid, platform, text, lang FROM post_facts"
    ).df()
    print(f"posts: {len(posts):,}")

    posts["text"] = posts["text"].fillna("").astype(str)
    posts["word_count"] = posts["text"].map(word_count)

    # ---- Language detection for missing ----
    print("language detection ...")
    detected = []
    for i, (txt, lang) in enumerate(zip(posts["text"], posts["lang"])):
        if lang and str(lang).strip() and str(lang).lower() not in ("unknown", "(unknown)", "null"):
            detected.append(str(lang).lower()[:8])
        else:
            detected.append(detect_lang(txt) or "unknown")
        if (i + 1) % 5000 == 0:
            print(f"  lang {i+1:,}/{len(posts):,}")
    posts["lang_resolved"] = detected

    # ---- Sentiment + toxicity ----
    print(f"loading sentiment: {SENTIMENT_MODEL}")
    sent_pipe = load_sentiment_pipeline(device)
    print("loading toxicity ...")
    tox_pipe, tox_model = load_toxicity_pipeline(device)
    if tox_model is None:
        print("WARN: no toxicity model available; hate scores will be null")

    texts = posts["text"].tolist()
    # empty texts → skip
    tone_scores = np.zeros(len(texts), dtype=np.float32)
    tone_valid = np.zeros(len(texts), dtype=bool)
    hate_scores = np.full(len(texts), np.nan, dtype=np.float32)
    hate_valid = np.zeros(len(texts), dtype=bool)

    # Length-sorted batching: scores are written by index so order is irrelevant,
    # but similar-length texts per batch cut padding waste (~1.9x on this corpus).
    nonempty_idx = sorted((i for i, t in enumerate(texts) if t and t.strip()),
                          key=lambda i: len(texts[i][:2000]))
    print(f"scoring {len(nonempty_idx):,} non-empty texts ...")

    for start in range(0, len(nonempty_idx), BATCH):
        batch_ix = nonempty_idx[start : start + BATCH]
        batch_txt = [texts[i][:2000] for i in batch_ix]
        try:
            sent_out = sent_pipe(batch_txt)
            for j, out in enumerate(sent_out):
                # pipeline with top_k=None returns list of labels per item
                if isinstance(out, list) and out and isinstance(out[0], dict):
                    tone_scores[batch_ix[j]] = sentiment_to_continuous(out)
                elif isinstance(out, dict):
                    tone_scores[batch_ix[j]] = sentiment_to_continuous([out])
                tone_valid[batch_ix[j]] = True
        except Exception as e:
            print(f"  sentiment batch {start} error: {e}")

        if tox_pipe is not None:
            try:
                tox_out = tox_pipe(batch_txt)
                for j, out in enumerate(tox_out):
                    if isinstance(out, list) and out and isinstance(out[0], dict):
                        hate_scores[batch_ix[j]] = toxicity_prob(out)
                    elif isinstance(out, dict):
                        hate_scores[batch_ix[j]] = toxicity_prob([out])
                    hate_valid[batch_ix[j]] = True
            except Exception as e:
                print(f"  toxicity batch {start} error: {e}")

        if (start // BATCH) % 5 == 0:
            print(
                f"  batch {start // BATCH} ({start + len(batch_ix):,}/{len(nonempty_idx):,})",
                flush=True,
            )

    # ---- Readability ----
    print("readability ...")
    read_rows = [score_readability(t, lang) for t, lang in zip(texts, posts["lang_resolved"])]

    scored_at = datetime.now(timezone.utc)
    rows = []
    for i in range(len(posts)):
        r = read_rows[i]
        rows.append(
            {
                "post_uid": posts.iloc[i]["post_uid"],
                "platform": posts.iloc[i]["platform"],
                "lang_declared": posts.iloc[i]["lang"],
                "lang_resolved": posts.iloc[i]["lang_resolved"],
                "word_count": int(posts.iloc[i]["word_count"]),
                "tone_score": float(tone_scores[i]) if tone_valid[i] else None,
                "tone_valid": bool(tone_valid[i]),
                "tone_model": SENTIMENT_MODEL if tone_valid[i] else None,
                "hate_score": float(hate_scores[i]) if hate_valid[i] and np.isfinite(hate_scores[i]) else None,
                "hate_valid": bool(hate_valid[i]),
                "hate_model": tox_model if hate_valid[i] else None,
                "readability_score": r["readability_score"],
                "readability_metric": r["readability_metric"],
                "readability_valid": r["readability_valid"],
                "readability_reason": r["readability_reason"],
                "scored_at": scored_at,
                "valid": bool(tone_valid[i] or hate_valid[i] or r["readability_valid"]),
            }
        )

    post_scores = pd.DataFrame(rows)
    write_parquet(post_scores, "post_scores")
    replace_table_from_df(con, "post_scores", post_scores)

    # summary
    summary = {
        "n": len(post_scores),
        "tone_valid_pct": float(post_scores["tone_valid"].mean() * 100),
        "hate_valid_pct": float(post_scores["hate_valid"].mean() * 100),
        "read_valid_pct": float(post_scores["readability_valid"].mean() * 100),
        "tone_mean": float(post_scores.loc[post_scores["tone_valid"], "tone_score"].mean())
        if post_scores["tone_valid"].any()
        else None,
        "hate_mean_raw": float(post_scores.loc[post_scores["hate_valid"], "hate_score"].mean())
        if post_scores["hate_valid"].any()
        else None,
        "sentiment_model": SENTIMENT_MODEL,
        "toxicity_model": tox_model,
        "caveat": "Hate scores uncalibrated — war/terror corpus over-fires on reported violence.",
    }
    (REPORTS_DIR / "nlp_scores_summary.json").write_text(json.dumps(summary, indent=2, default=str))
    print(json.dumps(summary, indent=2, default=str))
    con.close()
    print("Layer 3 complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
