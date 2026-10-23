---
name: api-timeouts
description: Outbound API calls use a 10s connect timeout and a 30s read timeout, set in one shared client.
metadata:
  type: decision
  tags:
    - api
    - reliability
  status: active
  author: sam
  created: 2026-10-10
---
Every outbound HTTP call goes through the shared client, which sets a 10 second connect timeout and a
30 second read timeout. A per-call override needs a comment saying why.

Replaces [[api-timeouts-legacy]].
