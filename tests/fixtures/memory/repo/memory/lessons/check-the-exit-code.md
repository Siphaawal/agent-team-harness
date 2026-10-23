---
name: check-the-exit-code
description: A script that prints success can still have failed; read its exit code, not its last line.
metadata:
  type: lesson
  tags: [verification]
  status: active
  author: alex
  created: 2026-10-05
  trigger: "Did I read the exit code, or only the output?"
---
A wrapper printed "done" after its child process crashed. The exit code was 1 the whole time.
