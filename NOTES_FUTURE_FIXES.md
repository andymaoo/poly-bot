# Future fixes (after runs / trading data)

Tweak based on trading data and logging. Do not implement until we have runs to analyze.

1. **Use actual unwind fill price**  
   If the order/fill API exposes execution price for the unwind sell, use it for P&L and journal instead of `ws_feed.get_best_bid()`.

2. **Handle failed unwind replacement**  
   When we cancel and replace the unwind order (stale, near expiry), if the replacement `sell_at_bid` fails we should not mark the market as effectively exited. Keep state so we retry or handle the remaining exposure.

3. **Journal-based probability estimates**  
   Replace heuristic `p_both`, `p_one`, `p_fill(Δ)` (and similar) with estimates derived from trade journal / fill history (e.g. fill rates by time remaining, book state).
