# WhyMoved benchmark

Synthetic data only. Seeds were not used to choose any threshold. Rates show the 95% Wilson interval in brackets. A good score here means the engine works on simple planted causes; it says little about real business data.

## Finding planted causes

| scenario | runs | first finding right | exactly right | all causes in top 3 | wrong first finding | said no driver |
|---|---|---|---|---|---|---|
| single | 100 | 100% (96-100%) | 100% (96-100%) | 100% (96-100%) | 0% (0-4%) | 0% (0-4%) |
| noisy | 100 | 100% (96-100%) | 100% (96-100%) | 100% (96-100%) | 0% (0-4%) | 0% (0-4%) |
| budget_miss | 100 | 100% (96-100%) | 100% (96-100%) | 100% (96-100%) | 0% (0-4%) | 0% (0-4%) |
| mix_only | 100 | 100% (96-100%) | 100% (96-100%) | 100% (96-100%) | 0% (0-4%) | 0% (0-4%) |
| double | 100 | 100% (96-100%) | 77% (68-84%) | 100% (96-100%) | 0% (0-4%) | 0% (0-4%) |

"First finding right" counts a coarser or narrower description of the planted segment (for example `device=Mobile` for Mobile in two regions). "Exactly right" does not.

## No real change planted

- Said "no clear driver": 100% (96-100%) of 100 runs
- Reported a driver anyway (false alarm): 0% (0-4%)

## Bridges

| scenario | runs | success | what counts |
|---|---|---|---|
| pvm | 100 | 100% (96-100%) | price effect located in Fashion, interval above zero |
| cost_rise | 100 | 100% (96-100%) | cost effect located in Electronics, interval below zero |
| share_loss | 100 | 100% (96-100%) | lost share located in the planted region, interval below zero |

## Placebo test (random split of weeks)

0 of 40 random splits produced a reported driver (0%, 95% interval 0-9%). Every one is a false alarm.

## How small a drop is still found?

One region x device segment's conversion falls by the stated amount. "Found" means the first finding is that segment (or a coarser/narrower description). Noise is the extra day-level noise added by the generator (0.05 is the default).

| extra noise | drop in segment conversion | runs | found | said no driver | wrong first finding |
|---|---|---|---|---|---|
| 0.05 | 5% | 50 | 0% (0-7%) | 100% (93-100%) | 0% (0-7%) |
| 0.05 | 10% | 50 | 20% (11-33%) | 80% (67-89%) | 0% (0-7%) |
| 0.05 | 15% | 50 | 78% (65-87%) | 22% (13-35%) | 0% (0-7%) |
| 0.05 | 20% | 50 | 96% (87-99%) | 2% (0-10%) | 2% (0-10%) |
| 0.05 | 30% | 50 | 98% (90-100%) | 0% (0-7%) | 2% (0-10%) |
| 0.05 | 50% | 50 | 100% (93-100%) | 0% (0-7%) | 0% (0-7%) |
| 0.20 | 5% | 50 | 0% (0-7%) | 100% (93-100%) | 0% (0-7%) |
| 0.20 | 10% | 50 | 18% (10-31%) | 82% (69-90%) | 0% (0-7%) |
| 0.20 | 15% | 50 | 56% (42-69%) | 44% (31-58%) | 0% (0-7%) |
| 0.20 | 20% | 50 | 88% (76-94%) | 12% (6-24%) | 0% (0-7%) |
| 0.20 | 30% | 50 | 100% (93-100%) | 0% (0-7%) | 0% (0-7%) |
| 0.20 | 50% | 50 | 100% (93-100%) | 0% (0-7%) | 0% (0-7%) |
| 0.40 | 5% | 50 | 0% (0-7%) | 100% (93-100%) | 0% (0-7%) |
| 0.40 | 10% | 50 | 2% (0-10%) | 98% (90-100%) | 0% (0-7%) |
| 0.40 | 15% | 50 | 26% (16-40%) | 72% (58-83%) | 2% (0-10%) |
| 0.40 | 20% | 50 | 62% (48-74%) | 36% (24-50%) | 2% (0-10%) |
| 0.40 | 30% | 50 | 96% (87-99%) | 4% (1-13%) | 0% (0-7%) |
| 0.40 | 50% | 50 | 100% (93-100%) | 0% (0-7%) | 0% (0-7%) |

## Speed (engine only, 4 dimensions, 400 resamples)

| rows | seconds |
|---|---|
| 96,768 | 0.6 |
| 508,032 | 2.2 |
| 991,872 | 4.0 |

Large tables are the synthetic table repeated with +-5% jitter, so the segment count stays fixed; many more segments would cost more.

## Failure gallery

No failures in these runs.
## Right place, different description

- double: 23 runs reported a broader or narrower segment than planted, e.g. seed 20001: planted region=West Bengal & device=Mobile + region=Bengaluru & device=Mobile, reported device=Mobile; seed 20008: planted region=Bengaluru & device=Mobile + region=Mumbai & device=Mobile, reported device=Mobile; seed 20018: planted region=Gujarat & device=Mobile + region=Tamil Nadu & device=Mobile, reported device=Mobile
