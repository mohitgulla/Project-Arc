-- 027_d56_cleanup.sql — D56 cutover cleanup (E13.15).
--
-- The D51 trending tier job (universe.trending), the put_call source and the
-- unusual_options source are gone, so their dispatcher cursors can never advance
-- again. Drop them; no table changes. Stored context rows of the removed kinds stay
-- (readers tolerate them) and expire by TTL.
DELETE FROM routine_state
 WHERE key IN ('cursor:universe.trending', 'cursor:put_call', 'cursor:unusual_options');
