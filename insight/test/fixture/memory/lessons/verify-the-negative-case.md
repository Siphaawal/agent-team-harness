---
name: verify-the-negative-case
description: A check that cannot fail proves nothing; run it once against a known-bad input.
metadata:
  type: lesson
  tags: [verification]
  status: active
  author: fixture
  created: 2026-10-20
  trigger: "What would this check show if the thing were broken?"
  tier0: true
---
A test that passes against both the fixed and the broken code is decoration.
Feed it the bad input once and watch it fail before trusting the green run.
