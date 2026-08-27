# Agent prompt — resume vertical-slice implementer

Start only after checkpoint acceptance. Use the declared narrow scope and one worker. Record the candidate family before execution so restart cannot redefine it.

Implement the smallest real `--resume` path through ledger, checkpoint, runner, and CLI. Fault-inject each boundary and compare against an uninterrupted run using canonical normalized results and raw evidence hashes.

Do not fake candidates, rebuild missing queues, filter failed attempts out of the denominator, accept a corrupt checkpoint, or add parallel scheduling. Any ambiguous recovery is `NOT_VERIFIABLE`/FAIL. Return the complete matrix to an independent determinism reviewer.
