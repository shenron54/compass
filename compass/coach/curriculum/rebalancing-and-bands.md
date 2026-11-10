---
title: Rebalancing with bands
order: 11
summary: Selling what ran up and buying what fell, without doing it too often.
indicators: []
---
Over time your winners grow into a bigger share of the portfolio. **Rebalancing** brings the
weights back to their targets, which means trimming what went up and adding to what went down.
It enforces "buy low, sell high" without any forecast.

**When to rebalance:**
- **Calendar:** once or twice a year, regardless of drift. Simple and low-effort.
- **Bands (thresholds):** only when a weight drifts outside a band around its target. A common
  rule of thumb (popularised by Larry Swedroe) is the "5/25" band: rebalance when an asset is 5
  percentage points away from its target, or 25% away in relative terms for small positions.
  Studies generally find bands trade less often than calendar rebalancing, with similar or better
  results.

compass marks each position "over" or "under" when it leaves its band, using the `bands` in your
portfolio config. The allocation-band rule turns that into an alert for the measures you care
about most, such as total look-through tech.

**Cheaper ways to rebalance:**
- Direct new contributions and dividends to the underweight holdings first, so you may never need
  to sell.
- In Hong Kong there is no capital gains tax for individuals, so selling to rebalance has no tax
  cost. That's an advantage over investors in many other countries, but commissions and FX
  spreads still apply.

**Rebalancing and momentum pull against each other.** Trimming a stock that has doubled feels
wrong, and sometimes it is: the strongest companies can compound for a decade. That's why the trim
rule waits for two conditions together: the position is overweight *and* far above its trend. Its
history view shows how often trimming gave up further gains.
