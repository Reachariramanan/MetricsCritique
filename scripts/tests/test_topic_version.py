#!/usr/bin/env python3
"""Readers must take the topic table from `lib/topic_version.py`, never hardcode one.

The dashboard build read `post_topic_v2` while the drill-down API read `post_topic_v4`,
so the two could disagree (review item #15). This fails if any reader's SQL names a
versioned topic table directly in a FROM/JOIN.

Run:  .venv/bin/python scripts/tests/test_topic_version.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
READERS = ["UI/api_server.py", "UI/build_dashboard.py", "scripts/07_metrics_v2.py",
           "scripts/16_assign_new_posts_topics.py"]
_HARDCODED = re.compile(r"\b(?:FROM|JOIN)\s+(?:\w+\.)?(?:post_topic|topic_dim|topic_parent)_v\d\b", re.I)


def test_no_hardcoded_topic_version():
    bad = []
    for rel in READERS:
        for n, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), 1):
            if _HARDCODED.search(line):
                bad.append(f"{rel}:{n}: {line.strip()}")
    assert not bad, "hardcoded topic table version:\n" + "\n".join(bad)


if __name__ == "__main__":
    test_no_hardcoded_topic_version()
    print("ok test_no_hardcoded_topic_version")
