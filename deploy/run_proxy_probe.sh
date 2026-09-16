#!/bin/bash
# Path reachability probe: explicit HTTP proxy (:1082) vs transparent fake-IP, per venue host, plus a
# control host the proxy routes without its remote node.
#
# WHY THIS EXISTS.  Every live-loop cycle failure to date is a transport error on the path to the venue
# (3x ProxyError 503, 1x ConnectError, 1x ConnectTimeout).  The proposed remedy - put the venue hosts in
# NO_PROXY - needs evidence before it is worth the switch, and a single ping is not evidence.
#
# WHAT THE FIRST TWO ARMS DO *NOT* MEASURE.  Not "direct vs proxy".  There is no measurable direct path
# to the VENUE from this host: the system resolver answers fapi/demo-fapi with 198.18.0.x (RFC 2544,
# Shadowrocket's fake-IP range) and external DNS is unreachable, so both arms terminate inside the same
# proxy app.
#   explicit     - force HTTP(S)_PROXY at 127.0.0.1:1082; this is the path the armed loop uses today
#                  and the one that produced every ProxyError.
#   transparent  - no proxy variables; the name resolves to a fake IP that is routed transparently.
#                  This is exactly what adding the hosts to NO_PROXY would switch the loop to.
# So the question those two answer is "is the transparent path more reliable than the explicit one",
# which is the question the NO_PROXY change actually turns on.
#
# THE CONTROL ARM (added 2026-09-16), and the question it exists to split.  18 hours of the two arms
# above said the failures are 3.8%-4.6% on BOTH hosts and BOTH arms - near-identical, so the fault is
# the path and not the venue, the demo credentials, or the loop - and that they come in bursts: 27 of
# them, 15s to 580s each, about 5% of wall-clock time.  What that cannot say is WHERE on the path, and
# the two answers have different prices: replacing the proxy node is cheap, and chasing the uplink or
# the tunnel app is not.
#   direct_routed - captive.apple.com, no proxy variables.  Everything on this host goes through the
#                   tunnel app's resolver and packet handling, so this arm is NOT "the proxy removed";
#                   what it removes is the app's REMOTE NODE.  Evidence that it does: measured
#                   2026-09-16, this host answers in 82-90ms across repeated samples while the venue
#                   leg takes 470-700ms, a gap that a shared remote node could not produce.  That is
#                   evidence of a different route, not proof of one - the app's rules are not readable
#                   from here - so read a SINGLE clean sample as suggestive and the burst overlap as
#                   the finding.
# How to read it, once there are bursts to compare:
#   control fails during the same bursts  -> local: the uplink or the tunnel app itself.  Changing
#                                            proxy nodes buys nothing.
#   control stays clean through them      -> remote: the node or its leg to Binance.  Changing nodes
#                                            is the cheap fix, and NO_PROXY is not one at all, since
#                                            the transparent arm fails at the same rate.
# It is deliberately NOT a venue host: the point is a host the app routes WITHOUT the node, and both
# venue names are the ones it must send through it.
#
# Read-only and unauthenticated: /fapi/v1/ping carries request weight 1 and needs no key;
# /hotspot-detect.html is Apple's own reachability endpoint and ~70 bytes.  Never writes to the repo,
# the venue, or any live state directory - one appended JSON line per sample per arm.
set -uo pipefail
OUT="$HOME/Library/Application Support/beidou/proxy-probe.jsonl"
mkdir -p "$(dirname "$OUT")"
PROXY="http://127.0.0.1:1082"
stamp() { date -u '+%Y-%m-%dT%H:%M:%SZ'; }

sample() {  # host arm path
  local host="$1" arm="$2" path="$3" out code total rc
  # -w writes "code time" on success; on a transport failure curl prints nothing and exits non-zero,
  # which is the observation we are here to count, so failures are recorded rather than swallowed.
  if [ "$arm" = explicit ]; then
    out=$(HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY" NO_PROXY="" \
      curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 20 "https://$host$path" 2>/dev/null)
    rc=$?
  else
    out=$(env -u HTTP_PROXY -u HTTPS_PROXY -u http_proxy -u https_proxy -u ALL_PROXY -u all_proxy \
      curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 20 "https://$host$path" 2>/dev/null)
    rc=$?
  fi
  code=$(printf '%s' "$out" | awk '{print $1}')
  total=$(printf '%s' "$out" | awk '{print $2}')
  # `10#` strips the leading zeros of curl's `000`-on-failure, which is what a failed sample reports and
  # is NOT valid JSON: every row written before 2026-09-16 whose request failed - 170 of the first 4,020 -
  # breaks `json.loads` at that field, so the first analysis of this file had to re-parse it with a regex.
  # A log written to be read by a parser has to parse; the rest of the schema is unchanged.
  printf '{"at":"%s","host":"%s","arm":"%s","curl_rc":%d,"http":%d,"seconds":%s}\n' \
    "$(stamp)" "$host" "$arm" "$rc" "$((10#${code:-0}))" "${total:-0}" >> "$OUT"
}

for host in fapi.binance.com demo-fapi.binance.com; do
  for arm in explicit transparent; do
    sample "$host" "$arm" /fapi/v1/ping
  done
done
sample captive.apple.com direct_routed /hotspot-detect.html
