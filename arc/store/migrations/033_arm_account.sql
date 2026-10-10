-- 033_arm_account.sql — multi-arm shared experiment account (E15.1, D69).
--
-- arm_identity gains how the arm uses its broker account and WHICH account it is:
--   account_mode    'dedicated' (one paper account per arm, D44) or 'shared' (many
--                   arms, of one or several experiments, on one account; D69)
--   account_sha256  sha256 hex of the broker account_number at t0 (never the raw
--                   number); NULL on a store started before D69 or offline (fixtures
--                   with no probe). Every arms tick and every arm broker compares the
--                   live account with it: a swapped key pair halts the arm
--                   (account_changed) before anything runs.
--   account_last4   the account_number's last 4 characters, for messages.
-- Written once with the identity (the no-update / no-delete triggers stay).
ALTER TABLE arm_identity ADD COLUMN account_mode TEXT NOT NULL DEFAULT 'dedicated'
    CHECK (account_mode IN ('dedicated', 'shared'));
ALTER TABLE arm_identity ADD COLUMN account_sha256 TEXT;
ALTER TABLE arm_identity ADD COLUMN account_last4 TEXT;
