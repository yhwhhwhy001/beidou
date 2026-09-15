#!/bin/bash
# Path reachability probe: explicit HTTP proxy (:1082) vs transparent fake-IP, per venue host.
#
# WHY THIS EXISTS.  All four live-loop cycle failures to date are transport errors on the path to the
# venue (2x ProxyError 503, 1x ConnectError, 1x ConnectTimeout; 4/346 armed cycles = 1.16%, about one
# every 3.1 days).  The proposed remedy - put the venue hosts in NO_PROXY - needs evidence before it is
# worth the switch, and a single ping is not evidence.
#
# WHAT IT DOES *NOT* MEASURE.  Not "direct vs proxy".  There is no measurable direct path from this
# host: the system resolver answers fapi/demo-fapi with 198.18.0.x (RFC 2544, Shadowrocket's fake-IP
# range) and external DNS is unreachable, so BOTH arms below terminate inside the same proxy app.
#   explicit     - force HTTP(S)_PROXY at 127.0.0.1:1082; this is the path the armed loop uses today
#                  and the one that produced every ProxyError.
#   transparent  - no proxy variables; the name resolves to a fake IP that is routed transparently.
#                  This is exactly what adding the hosts to NO_PROXY would switch the loop to.
# So the question this answers is "is the transparent path more reliable than the explicit one", which
# is the question the NO_PROXY change actually turns on.  Whether Binance is reachable with the proxy
# app OFF is a different question and cannot be answered from inside it.
#
# Read-only and unauthenticated: /fapi/v1/ping carries request weight 1 and needs no key.  Never writes
# to the repo, the venue, or any live state directory - one appended JSON line per sample per arm.
set -uo pipefail
OUT="$HOME/Library/Application Support/beidou/proxy-probe.jsonl"
mkdir -p "$(dirname "$OUT")"
PROXY="http://127.0.0.1:1082"
stamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

sample() {  # host arm
  local host="$1" arm="$2" out code total rc
  # -w writes "code time" on success; on a transport failure curl prints nothing and exits non-zero,
  # which is the observation we are here to count, so failures are recorded rather than swallowed.
  if [ "$arm" = explicit ]; then
    out=$(HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY" NO_PROXY="" \
      curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 20 "https://$host/fapi/v1/ping" 2>/dev/null)
    rc=$?
  else
    out=$(env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY -u all_proxy \
      curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 20 "https://$host/fapi/v1/ping" 2>/dev/null)
    rc=$?
  fi
  code=$(printf '%s' "$out" | awk '{print $1}')
  total=$(printf '%s' "$out" | awk '{print $2}')
  printf '{"at":"%s","host":"%s","arm":"%s","curl_rc":%d,"http":%s,"seconds":%s}\n' \
    "$(stamp)" "$host" "$arm" "$rc" "${code:-0}" "${total:-0}" >> "$OUT"
}

for host in fapi.binance.com demo-fapi.binance.com; do
  for arm in explicit transparent; do
    sample "$host" "$arm"
  done
done
