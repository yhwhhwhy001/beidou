#!/bin/sh
# Legacy compatibility wrapper. It validates arguments, then delegates to the
# canonical launcher without loading secrets, changing environment variables,
# creating files, starting dependencies, or producing certificates.

set -eu

usage() {
    echo "usage: start_beidou.sh start --mode MODE --symbols SYMBOLS [launcher options]" >&2
    echo "       start_beidou.sh doctor|status|stop [launcher options]" >&2
}

if [ "$#" -lt 1 ]; then
    usage
    exit 64
fi

action=$1
case "$action" in
    start|doctor|status|stop) ;;
    *)
        usage
        exit 64
        ;;
esac

if [ "$action" = "start" ]; then
    mode_seen=false
    symbols_seen=false
    mode_count=0
    symbols_count=0
    expect_mode=false
    expect_symbols=false
    for argument in "$@"; do
        if [ "$expect_mode" = true ]; then
            [ -n "$argument" ] || { usage; exit 64; }
            mode_seen=true
            expect_mode=false
            continue
        fi
        if [ "$expect_symbols" = true ]; then
            [ -n "$argument" ] || { usage; exit 64; }
            symbols_seen=true
            expect_symbols=false
            continue
        fi
        case "$argument" in
            --mode)
                mode_count=$((mode_count + 1))
                expect_mode=true
                ;;
            --mode=?*)
                mode_count=$((mode_count + 1))
                mode_seen=true
                ;;
            --symbols)
                symbols_count=$((symbols_count + 1))
                expect_symbols=true
                ;;
            --symbols=?*)
                symbols_count=$((symbols_count + 1))
                symbols_seen=true
                ;;
        esac
    done
    if [ "$expect_mode" = true ] || [ "$expect_symbols" = true ] || [ "$mode_seen" != true ] || [ "$symbols_seen" != true ] || [ "$mode_count" -ne 1 ] || [ "$symbols_count" -ne 1 ]; then
        usage
        exit 64
    fi
fi

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
launcher=$script_dir/.venv/bin/beidou
if [ ! -x "$launcher" ]; then
    echo "canonical launcher is unavailable: $launcher" >&2
    exit 69
fi

exec "$launcher" "$@"
