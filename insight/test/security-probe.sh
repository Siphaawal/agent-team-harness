#!/usr/bin/env bash
# security-probe.sh PORT - Host / Origin / token / traversal probes against a RUNNING Insight (read-only requests).
# The token comes from the INSIGHT_TOKEN environment variable (the value you started the server with, or the
# one in the URL it printed):   INSIGHT_TOKEN=... bash insight/test/security-probe.sh 7717
# Each line prints the HTTP status and the status it must have. Exit 1 if any line is wrong.
P=$1
[ -n "$P" ] || { echo "usage: INSIGHT_TOKEN=<token> security-probe.sh PORT" >&2; exit 2; }
[ -n "$INSIGHT_TOKEN" ] || { echo "INSIGHT_TOKEN is not set (copy it from the ?token= URL the server printed)" >&2; exit 2; }
BAD=0
B="http://127.0.0.1:$P"
AUTH="Authorization: Bearer $INSIGHT_TOKEN"
c(){ curl -s -o /dev/null -w "%{http_code}" "$@"; }
check(){ # name want got
  if [ "$2" = "$3" ]; then echo "ok    $1 -> $3"; else echo "FAIL  $1 -> $3 (want $2)"; BAD=1; fi
}
check "state no token"           401 "$(c $B/api/state)"
check "state wrong token"        401 "$(c -H "Authorization: Bearer ${INSIGHT_TOKEN}x" $B/api/state)"
check "state short token"        401 "$(c -H "Authorization: Bearer a" $B/api/state)"
check "stream no token"          401 "$(c -m 2 $B/api/stream)"
check "state token in query"     401 "$(c "$B/api/state?token=$INSIGHT_TOKEN")"
check "state bearer"             200 "$(c -H "$AUTH" $B/api/state)"
check "state cookie"             200 "$(c -H "Cookie: insight=$INSIGHT_TOKEN" $B/api/state)"
check "token URL -> redirect"    302 "$(c "$B/?token=$INSIGHT_TOKEN")"
check "state good host"          200 "$(c -H "$AUTH" -H "Host: 127.0.0.1:$P" $B/api/state)"
check "state localhost"          200 "$(c -H "$AUTH" -H "Host: localhost:$P" $B/api/state)"
check "page /"                   200 "$(c $B/)"
check "page via link (x-site)"   200 "$(c -H 'Sec-Fetch-Site: cross-site' $B/)"
check "state forged host"        403 "$(c -H "$AUTH" -H "Host: evil.example:$P" $B/api/state)"
check "state wrong port"         403 "$(c -H "$AUTH" -H "Host: 127.0.0.1:1" $B/api/state)"
check "stream forged host"       403 "$(c -m 2 -H "$AUTH" -H "Host: evil.example:$P" $B/api/stream)"
check "state cross-site"         403 "$(c -H "$AUTH" -H 'Sec-Fetch-Site: cross-site' $B/api/state)"
check "state Origin evil"        403 "$(c -H "$AUTH" -H 'Origin: https://evil.example' $B/api/state)"
check "state same-origin"        200 "$(c -H "$AUTH" -H 'Sec-Fetch-Site: same-origin' $B/api/state)"
check "sibling traversal"        403 "$(c --path-as-is "$B/..%2Fpublic-zz%2Fx")"
check "parent traversal"         403 "$(c --path-as-is "$B/..%2Fserver.mjs")"
check "malformed escape"         400 "$(c --path-as-is "$B/%E0%A4%A")"
check "removed route /api/say"   404 "$(c -H "$AUTH" "$B/api/say?t=hi")"
check "removed route /api/snip"  404 "$(c -H "$AUTH" -X POST --data-binary x "$B/api/snip")"
CSP="$(curl -s -D - -o /dev/null $B/ | tr -d '\r' | grep -i '^content-security-policy:' | head -1)"
case "$CSP" in *"default-src 'self'"*"frame-ancestors 'none'"*) check "CSP header on /" ok ok ;; *) check "CSP header on /" ok "missing" ;; esac
exit $BAD
