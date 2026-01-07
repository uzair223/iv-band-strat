CALL DOLT_CLONE('post-no-preference/options');
CALL DOLT_CHECKOUT('-b', 'setup');

CREATE INDEX idx_act_symbol_date_delta ON option_chain (act_symbol, date, delta);

CALL DOLT_COMMIT('-Am', 'Added index idx_act_symbol_date_delta on option_chain table');
CALL DOLT_MERGE('master');