# RECALL ROUTING - what to LOAD, given the request

No route matched? -> python3 tools/memory_tools.py search "<terms>"
  (ranks memory/**, agent/** and skills; [SUPERSEDED] results are labelled - do not act on them)
Never read "no route" as "nothing recorded".

```
resume|continue|where were we       -> newest memory/log/ files + git status (skip the librarian sweep)
wrap|end of session|save what we    -> SKILL wrap
team|teammates|agents|in parallel   -> SKILL team-lead
review|audit|before I ship          -> spawn subagent_type reviewer
verify|test this|does it work       -> spawn subagent_type verifier
red team|challenge|is this right    -> spawn subagent_type adversary
<words>                             -> <SKILL name | MEM slug | doc path>
```

Index of every memory: memory/_gen/INDEX.md (generated at session start). Lessons' trigger questions are in the
TRIGGERS section of this payload.
