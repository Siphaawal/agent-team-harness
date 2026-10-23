---
name: comment-after-values
description: Inline comments after values must not change what a value means.
metadata:
  type: lesson
  tags:
    - parsing
    - yaml
  status: active # still live
  author: fixture
  created: 2026-10-22
  trigger: "Does this parser agree with the other parser?"
  tier0: false   # keep it out of TRIGGERS
---
Two parsers for one format will disagree on the first inline comment.
Keep exactly one and ask it.
