# Methodology and limits

CustomerWorld explores how persistent simulated customers might approach a product under changing daily circumstances. It records model choices and actual interface observations separately so a team can investigate why a session progressed, stalled, or never began.

## What a day means

A participant begins with a profile grounded in its own source. On the current date in the project's timezone, the model creates a world context from that profile, the participant's own previous world, and its latest completed memory. The world contains an event, available time, energy, and unresolved items.

The participant initially chooses the primary product, a configured alternative, or neither. Choosing neither is a voluntary nonvisit. Choosing a tool requests a real UI session through the broker. The model may act on observed UI, switch tools, or finish with a decision and memory. Once a UI session has opened, finishing also passes through the broker.

The runner uses the actual current date. Past-date backfills and future-date runs are rejected. An interrupted day can resume unfinished participants, while completed participant-days remain complete. Resumption is continuation, not an independent repetition of the experiment.

## Reading outcomes

A generated profile is not an active session. A generated daily context is not a visit. A stated intention is not an observed UI action. Primary-product visits, alternative-tool visits, and voluntary nonvisits must be read separately.

Failures also need their own interpretation. A broker timeout or tool failure does not show that a participant rejected the product. Reaching the action cap means the environment censored the session; it does not establish a voluntary stopping choice. Review these categories before drawing conclusions from totals.

The automatic report exposes aggregate execution outcomes. It does not export raw participant journals, source briefs, or screenshots by default. When explaining a particular behavior, inspect the local session evidence and distinguish a participant's memory from the observations that support it.

Without a date filter, counts describe distinct identities across all recorded dates. The same identity can appear in several intention or error categories, while `actor_days` counts the recorded sessions. Use `report --date YYYY-MM-DD` for daily comparisons; do not add overlapping categories or interpret an undated total as a session count.

## Limits

- **Synthetic behavior.** Model choices are not human responses and do not measure conversion, retention, willingness to pay, or market size.
- **Source dependence.** Weak, narrow, or fictional briefs constrain what a result can support. More participants do not repair an unrepresentative source set.
- **Model dependence.** Provider, model, prompt, and interface changes can alter outcomes. A resumed run may include work from different model configurations if the operator changes them.
- **No causal estimate.** A sequence of daily sessions is not a randomized experiment or evidence of a product's effect on people.
- **Operator dependence.** UI evidence relies on the host performing and capturing the requested action faithfully. Hash checks establish stored-file consistency, not semantic authenticity.
- **Limited isolation.** Participant context is logically scoped, but trusted workers and operators are not isolated by an operating-system sandbox.
- **Local, current-day state.** The runner does not recreate missing history or provide arbitrary time travel. Review incomplete sessions and censored runs before comparing days.

Use results to find questions worth investigating, reproduce possible friction, and improve research plans. Validate consequential product claims with appropriate human research and real usage evidence.

## Future directions

World branching, counterfactual timelines, and social interaction between participants are roadmap ideas. They are not implemented. Current participants do not share memory or receive a cohort summary, and the runner does not simulate a social network.
