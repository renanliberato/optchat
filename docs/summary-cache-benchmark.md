# Single-message summary cache benchmark — 2026-10-07

Implemented a frozen history prefix per 32 nodes at each level, a fresh recent
context tail, shared OpenRouter session routing, and an explicit breakpoint on
the shared prefix for GPT-5.6+/GPT-6. Unique inputs are outside that breakpoint.
One real summary warms a cold prefix before other workers proceed in parallel.
Short messages still copy verbatim for free. No padding or extra warm-up calls.

Reference: the user's exported `session-ses_ee81.md`, especially its distinction
between cross-node reuse, same-node correction hits, and identical-prompt replay.

## Live method

`scripts/benchmark-summary-cache.py` streamed distinct messages into isolated
copies of the durable log. It appended each source, generated its summary, then
installed available archived parent summaries to reproduce view coarsening.
Archived merges were not paid calls or generated fidelity evidence.

Both arms used `openai/gpt-6-luna`, low reasoning, 512 bytes, five size attempts,
128,000 context bytes, mandatory ZDR/data-collection-deny. All calls routed to
Azure. Separate system-prefix namespaces ensured neither the daemon nor an
identical earlier replay warmed either arm. Each arm generated each leaf once;
corrections remained in its own conversation. The baseline used the changing
context layout and automatic caching; both arms used historical cutoff splitting
to exclude future messages. The shared prefix reserved 8,192 bytes for fresh
context, so a small portion of savings also comes from a slightly shorter input.

Commands (paid; reports contain private source text and stay outside the repo):

```sh
python3 scripts/benchmark-summary-cache.py --start 41448 --count 8 \
  --budget .10 --output /tmp/optchat-cache-benchmark.json
python3 scripts/benchmark-summary-cache.py --start 40246 --count 8 \
  --budget .10 --output /tmp/optchat-cache-benchmark-reference.json
```

## Results

| Sample | Paid leaves | Baseline first-attempt cache share | New first-attempt cache share, including cold start | Baseline total cost | New total cost | Savings including corrections |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 41448–41455 | 4 | 0% | 62.3% | $0.02020445 | $0.00785613 | 61.1% |
| 40246–40253 (reference session's range) | 7 | 0% | 75.1% | $0.03467627 | $0.015090075 | 56.5% |

After the first cold summary, the cached shared prefixes were **31,986** and
**31,435** tokens respectively, with **zero new cache-write tokens** on subsequent
first attempts. Warm first-attempt cache shares were 79.2% and 88.1%; large unique
tool/report sources account for much of the remaining input. No duplicate-prompt
replays were counted as cross-message hits.

The reference sample's first-attempt cost fell $0.032230225 → $0.009651275 (70.1%).
It required 12 baseline calls versus 13 new-layout calls including corrections;
the cost advantage survives that extra correction. Its all-call input cost fell
$0.03337277 → $0.013930075 (58.3%). Across both samples, total cost was
$0.05488072 → $0.022946205 (58.2% less); the benchmark spent $0.077826925 total.

Latency was mixed: first-attempt median was 4.42s → 4.10s for the first sample and
3.63s → 4.90s for the reference sample. This demonstrates input-cost savings,
not a reliable latency improvement. Every saved summary in both arms fit 512
bytes. Five short messages were copied without a model call across each arm's
combined 16-message sample.

## Fidelity and verification

Manual review compared generated summaries to the original sources and archived
summaries. New summaries retained the central VFX gaps, approved omissions,
read-only/no-runtime-test status, and partial failure of the hit-stop patch.
The baseline falsely said owner-exclusion was already implemented for one
unconfirmed tool call; the new summary correctly said the edit was attempted.
Both layouts sometimes retained literal cutoff bars from existing contextual
summaries and omitted lower-priority details under the byte limit. The small
sample supports using the new layout; it is not a broad quality equivalence test.
The reference's `user` records include framed subagent reports; generated
summaries correctly tagged those as `work` rather than human instructions.

Offline tests cover stable prefixes during coarsening, fresh decisions, pending
predecessors, out-of-order jobs, no future leakage, merge contexts, UTF-8 budgets,
bounded snapshot storage, correction breakpoint preservation, session sharing,
unsupported-model fallback, cold-worker coordination/failure/expiry, and benchmark
accounting/history isolation/cost-stop behavior.

Provider behavior: [OpenRouter prompt caching](https://openrouter.ai/docs/guides/best-practices/prompt-caching)
documents session sticky routing, cache usage fields, and explicit OpenAI cache
breakpoints. Caches may expire or be evicted; short prefixes below the minimum
remain uncached. Default in-memory cache behavior is used; extended retention is
not requested. Set `summary_cache_window: 0` to restore changing context or
`openrouter_explicit_cache: false` to use automatic caching.
