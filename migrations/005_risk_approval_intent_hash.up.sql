-- Bind the exact venue order material into every new risk approval.
-- This is forward-only and preserves legacy rows for forensic review; runtime
-- readiness rejects approvals that still lack the new digest.
ALTER TABLE v3_risk_approvals
    ADD COLUMN IF NOT EXISTS intent_hash TEXT;

UPDATE v3_risk_approvals
SET intent_hash = ''
WHERE intent_hash IS NULL;

ALTER TABLE v3_risk_approvals
    ALTER COLUMN intent_hash SET DEFAULT '',
    ALTER COLUMN intent_hash SET NOT NULL;
