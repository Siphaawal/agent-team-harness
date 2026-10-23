---
name: retry-policy
description: Retries use exponential backoff with jitter, three attempts at most.
metadata:
  type: decision
  tags: [reliability]
  status: active
  author: sam
  created: 2026-02-30
---
Three attempts, base delay 200 ms, full jitter.
