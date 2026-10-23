---
name: retry-policy
description: Retries use exponential backoff with jitter, three attempts at most.
status: active
metadata:
  type: decision
  tags: [reliability]
  status: active
  author: sam
  created: 2026-10-01
---
Three attempts, base delay 200 ms, full jitter.
