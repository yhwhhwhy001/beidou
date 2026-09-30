# Validation: tsmom — FAIL

## Range

| key | value |
| --- | --- |
| start | 2021-03-02 01:00:00+00:00 |
| end | 2026-09-28 16:00:00+00:00 |
| bars | 48880 |

## Holdout (KILL-006)

| key | value |
| --- | --- |
| months | 0 |
| note | no tail reserved: every bar was available to this run |

## Book guards / exits (the layers the loop applies)

| key | value |
| --- | --- |
| guards | max_weight=0.1500; max_gross=2.0000; daily_loss_pause=-0.0500 |
| exits | stop_loss=6.0000; stop_loss_price_cap=0.0000; trailing_stop=0.0000; trailing_activate=0.0000; take_profit=6.0000; cooldown_bars=24; vol_halflife=48; bars_per_day=24; min_unit=0.0050; unit_mode=entry; regime_window=0; regime_er_cut=0.0500; regime_tp_scale=0.5000; regime_side=low; stale_carry_bars=2 |
| margin_buffer | structural bound, not a measurement: buffer = (1 + r - c) / (gross * maintenance_margin_rate), and gross <= max_gross, so at mmr 0.005 and max_gross 2.0 it cannot fall below about 100.  Reaching the liquidation line at 1.0 would take one bar losing ~99%, so `liquidation_touches: 0` is arithmetic rather than evidence.  The channel that can actually liquidate this account is collateral repricing (52% non-USDT, KILL-AR-05) and this replay models zero collateral. |

## Best params (full sample)

| key | value |
| --- | --- |
| horizons | 168, 336, 720 |
| horizon_weights | 0.2000, 0.3000, 0.5000 |
| return_weight | 0.4000 |
| slope_weight | 0.2500 |
| persistence_weight | 0.1000 |
| return_scale | 0.2000 |
| slope_scale | 0.0100 |
| entry_threshold | 0.2000 |
| vol_window | 400 |
| crowding_window | 72 |
| crowding_cut | 0.7000 |
| crowding_penalty | 0.5000 |
| momentum_mode | fixed |
| conviction_mode | sign |

## Full sample

| key | value |
| --- | --- |
| bars | 49600 |
| gross_return | 6.0089 |
| net_return | 4.6277 |
| annualized_sharpe | 1.9152 |
| annualized_sharpe_gross | 2.1479 |
| max_drawdown | -0.1039 |
| average_absolute_exposure | 0.5048 |
| turnover_units | 295.1 |
| nonzero_target_bars | 49409 |
| hit_rate | 0.5104 |
| cost_share_of_gross | 0.1084 |
| skew | 1.0012 |
| kurtosis | 28.3989 |
| sortino | 2.8298 |
| risk_free_rate | 0.0000 |
| guards | replayed=yes; gross_capped_bars=0; daily_loss_pause_bars=0; bars=49600; min_margin_buffer=107.3; liquidation_touches=0 |
| cagr_full_sample | 0.3568 |
| calmar_full_sample | 3.4357 |
| payoff_ratio_bar | 1.0204 |
| cagr_caliber | full-sample drift of the selected book, not the honest-drift bootstrap CAGR median the k decision reads beside vol_target in config/live.demo.yaml |

## Walk-forward (out of sample)

| key | value |
| --- | --- |
| folds | 5 |
| oos_sharpe | 1.5840 |
| oos_windows | 62 |
| oos_window_sharpe_q10 | -2.2772 |
| oos_return | 2.7053 |
| oos_max_drawdown | -0.1661 |
| oos_bars | 44880 |
| oos_t_stat | 3.4931 |
| oos_t_lags | 15 |
| fold_sharpes | 1.8410, 0.2250, 2.4520, 1.2630, 2.2937 |
| fold_consistency | 1.0000 |
| selection_consistent | no |
| oos_is_full_sample_tail | no |
| oos_cagr | 0.2913 |
| oos_calmar | 1.7542 |
| best_key_oos_sharpe | 1.8029 |
| selection_exercised | folds exercised a choice: this OOS is a selection procedure's out-of-sample record |

## CPCV

| key | value |
| --- | --- |
| paths | 15 |
| oos_sharpe_mean | 1.8764 |
| oos_sharpe_q05 | 1.3312 |
| oos_sharpe_min | 1.2937 |
| fraction_negative | 0.0000 |
| embargo | 50 bars, which is shorter than the model's feature lookback (tsmom max(horizons)=720, AlphaModel.warmup_bars 1442).  Training bars AFTER a test block are therefore computed from a window covering it, and `cpcv_evaluate` picks parameters on exactly those bars.  The returns stay causal, so this is selection contamination rather than look-ahead - it makes `fraction_negative`, one of D-020's hard gates, easier to pass than it should be.  Open boundary on the record: see `cpcv_splits`' docstring and docs/analysis/2026-09-13-full-repo-review.md. |

## Multiple testing

| key | value |
| --- | --- |
| n_trials | 357 |
| grid_trials | 16 |
| grid_effective_trials | 8.0000 |
| prior_trials | 172 |
| sharpe_variance_period | 0.0000 |
| candidate_sharpe_annual | 1.8787 |
| expected_max_sharpe_annual | 1.6279 |
| dsr | 0.7250 |
| dsr_p_value | 0.2750 |
| pbo | 0.0354 |
| pbo_combinations | 5000 |
| degradation_slope | -0.9912 |
| prob_oos_loss | 0.0038 |
| noise_null | sharpe_variance_period=0.0000; expected_max_sharpe_annual=1.2378; dsr_p_value=0.0633 |
| ledger_trials | 169 |
| pbo_is_informative | enforced: grid_trials=16 >= 4 |

## Selection-deflated OOS threshold (D-028)

| key | value |
| --- | --- |
| n_obs | 44880 |
| n_trials | 357 |
| alpha | 0.0500 |
| gate | max_sharpe_quantile |
| variance | 0.0000 |
| threshold_annual | 1.5984 |
| expected_max_annual | 1.3001 |
| p_family | 0.0565 |
| oos_sharpe_annual | 1.5840 |
| caliber | ENFORCED at N=357 (the per-strategy bucket): threshold 1.60, margin -0.01.  REPORTED and never enforced at N=21652 (the whole library): threshold 2.02, margin -0.43, p_family 0.9706.  Which N is the gate is R0 / KILL-AR-01, an operator ruling rather than a property of the arithmetic, and both numbers are printed so the ruling stays arguable from the artefact alone. |

## Power of that gate (D-020 + D-028): what it would have detected

| key | value |
| --- | --- |
| standard error of the OOS Sharpe (annual) | 0.4408 |
| gate (max of the two halves) | 1.5984  [selection binds] |
|   D-028 selection threshold | 1.5984 at N=357, alpha=0.05 |
|   D-020 pass line | 1.0000 |
| P(clear | true annual Sharpe = 1.0) | 8.7% |
| P(clear | true annual Sharpe = 1.2) | 18.3% |
| P(clear | true annual Sharpe = 1.5) | 41.2% |
| P(clear | true annual Sharpe = 2.0) | 81.9% |
| not included in the above | cpcv_fraction_negative, pbo, fold_consistency, cost_stress_x2 (so the true joint power is LOWER) |

## The other caliber (R0: reported, never enforced)

| key | value |
| --- | --- |
| n_obs | 44880 |
| n_trials | 21652 |
| alpha | 0.0500 |
| gate | max_sharpe_quantile |
| variance | 0.0000 |
| threshold_annual | 2.0169 |
| expected_max_annual | 1.7831 |
| p_family | 0.9706 |
| oos_sharpe_annual | 1.5840 |
| power | n_trials=21652; alpha=0.0500; se_annual=0.4408; pass_line_annual=1.0000; selection_threshold_annual=2.0169; gate_annual=2.0169; binding=selection; detects=true_sharpe_annual=1.0000; power=0.0105, true_sharpe_annual=1.2000; power=0.0319, true_sharpe_annual=1.5000; power=0.1204, true_sharpe_annual=2.0000; power=0.4847; excludes=cpcv_fraction_negative, pbo, fold_consistency, cost_stress_x2 |

## Cost stress against that gate (same folds, same N, best_key basis)

| key | value |
| --- | --- |
| x1 | oos=1.80 threshold=1.59 margin=0.21 clears=1.00 |
| x1.5 | oos=1.69 threshold=1.59 margin=0.10 clears=1.00 |
| x2 | oos=1.58 threshold=1.59 margin=-0.01 clears=0.00 |

## Slippage stress against that gate (fee held fixed - the grid the question is about)

| key | value |
| --- | --- |
| slip2 | oos=1.80 threshold=1.59 margin=0.21 clears=1.00 |
| slip4.43 | oos=1.73 threshold=1.59 margin=0.14 clears=1.00 |
| slip5.5 | oos=1.69 threshold=1.59 margin=0.10 clears=1.00 |
| slip9.2 | oos=1.57 threshold=1.59 margin=-0.02 clears=0.00 |

## Stability

| key | value |
| --- | --- |
| time_split_sharpes | 1.1490, 1.4735, 1.5290, 2.1721 |
| worst_neighbour_degradation | 0.1201 |
| parameter_neighbourhood | entry_threshold=down=1.70 base=1.92 up=1.69; return_scale=down=1.80 base=1.92 up=1.83; vol_window=down=1.92 base=1.92 up=1.92 |
| parameter_neighbourhood_not_perturbed | horizons |

## Sharpe by benchmark-volatility regime (reported, never enforced)

| key | value |
| --- | --- |
| low (annualised vol 0.40-0.70) | sharpe=3.10  bars=14960 |
| mid (annualised vol 0.70-0.87) | sharpe=0.91  bars=14960 |
| high (annualised vol 0.87-2.23) | sharpe=0.51  bars=14960 |
| basis: series | walk_forward oos_returns, the series time_split_sharpes splits |
| basis: state | annualised std, over vol_window_days, of benchmark_returns: equal-weight, zero cost, over the symbols the book could hold at each bar (pit members; every panel symbol if static) |
| basis: vol_window_days | 30 |
| basis: label_on_bar_t_reads | benchmark bars through t-1: what was known when bar t's position was decided |
| basis: cut_points | this sample's own terciles: the state is ex-ante, the cut points are not.  Volatility trends over years, so a tercile is partly a calendar period: read it against time_split_sharpes |

## Against its own basket, out of sample (N2; reported, never enforced)

| key | value |
| --- | --- |
| bars | 44880 |
| book (fold-selected, out of sample) | return=+270.534%  cagr=+29.130%  sharpe=1.58  max_drawdown=-16.606% |
| basket (pit members at each bar, equal weight, zero cost, rebalanced every bar) | return=-93.818%  cagr=-41.917%  sharpe=-0.20  max_drawdown=-97.357% |
| BTCUSDT buy and hold | return=+82.111%  cagr=+12.412%  sharpe=0.48  max_drawdown=-77.271% |
| constant: book ~ basket (passive market exposure) | beta=-0.02 (t -3.29)  alpha=0.305 bps/bar (t 3.45)  NW lags 48 |
| conditional: book ~ exposure x basket (timing counts as beta here) | beta=0.74 (t 55.64)  alpha=0.219 bps/bar (t 3.97)  NW lags 48 |
| signal state | all long 9.9%  all short 11.3%  two-sided 78.5%  flat 0.3%  net -0.00  gross 0.52 |
| basis: returns | simple per-bar returns; the live D-045 page regresses log returns |
| basis: basket | equal weight, zero cost, rebalanced every bar: a volatile basket's compounded return is mostly drag, so compare Sharpe, not return |
| basis: conditional | exposure is the book's own net exposure, so this fit counts the signal's timing as beta; whether the book is passive market exposure is the constant fit |
| basis: signal_state | signs of the executed positions, flat bars included; the live page counts the signal's +/-1 |

## Concentration by symbol (N3; reported, never enforced)

| key | value |
| --- | --- |
| full sample | top1 +10.141%  top3 +24.296%  of summed net returns +1.8062  (largest: BTCUSDT +0.1832, BNBUSDT +0.1309, SOLUSDT +0.1248) |
| out of sample | top1 +9.219%  top3 +24.683%  of summed net returns +1.3843  (largest: SOLUSDT +0.1276, ADAUSDT +0.1204, AVAXUSDT +0.0937) |
| OOS Sharpe without its most important name | 1.51 without HYPEUSDT (book 1.58) |
| basis: units | sums of per-bar simple net returns, not compounded; a share exceeds 100% when other names lost |
| basis: leave_one_out | one symbol's net column dropped; nothing re-priced, weights not renormalised, guards and exits not replayed, so the error has no known sign |

## Grid (full-sample Sharpe per configuration)

- entry_threshold=0.2, horizons=[168, 336, 720], return_scale=0.2: sharpe=1.88
- entry_threshold=0.2, horizons=[168, 336, 720], return_scale=0.3: sharpe=1.69
- entry_threshold=0.3, horizons=[168, 336, 720], return_scale=0.2: sharpe=1.37
- entry_threshold=0.3, horizons=[24, 72, 168], return_scale=0.2: sharpe=1.21
- entry_threshold=0.3, horizons=[168, 336, 720], return_scale=0.3: sharpe=1.20
- entry_threshold=0.3, horizons=[24, 72, 168], return_scale=0.3: sharpe=1.13
- entry_threshold=0.2, horizons=[24, 72, 168], return_scale=0.3: sharpe=1.01
- entry_threshold=0.2, horizons=[24, 72, 168], return_scale=0.2: sharpe=0.97
- entry_threshold=0.3, horizons=[5, 20, 50], return_scale=0.3: sharpe=0.92
- entry_threshold=0.3, horizons=[5, 20, 50], return_scale=0.2: sharpe=0.49
- entry_threshold=0.2, horizons=[336, 720, 1440], return_scale=0.3: sharpe=0.39
- entry_threshold=0.2, horizons=[5, 20, 50], return_scale=0.3: sharpe=0.37
- entry_threshold=0.2, horizons=[5, 20, 50], return_scale=0.2: sharpe=0.33
- entry_threshold=0.3, horizons=[336, 720, 1440], return_scale=0.2: sharpe=0.33
- entry_threshold=0.3, horizons=[336, 720, 1440], return_scale=0.3: sharpe=0.33
- entry_threshold=0.2, horizons=[336, 720, 1440], return_scale=0.2: sharpe=0.30

## Cost stress (Sharpe)

| key | value |
| --- | --- |
| x1 | 1.9152 |
| x1.5 | 1.8055 |
| x2 | 1.6958 |
| break-even multiple m* (never enforced) | full_sample=9.74  oos=9.10  (mean net return is 0 with turnover and carry costs x m*) |

## Slippage stress (Sharpe; fee fixed)

| key | value |
| --- | --- |
| slip2 | 1.9152 |
| slip4.43 | 1.8391 |
| slip5.5 | 1.8055 |
| slip9.2 | 1.6895 |

## Same book under close_to_close

| key | value |
| --- | --- |
| annualized_sharpe | 1.8865 |

## Verdict

| key | value |
| --- | --- |
| verdict | FAIL |
| reasons | oos_sharpe 1.58 < the deflated threshold 1.60 at 357 trials, p_family=0.0565 |
