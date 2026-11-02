# Architecture and state

CustomerWorld separates model decisions, UI execution, and reporting. This keeps a participant's stated intent distinct from what the interface actually showed.

## Execution flow

```text
Source brief → profile generation → participant profile
                                      ↓
Own previous world + own latest memory → daily world context
                                      ↓
                              model chooses next turn
                                      ↓
                         host-operated UI broker
                                      ↓
                   actual observation + local evidence
                                      ↓
                         next turn or completion
                                      ↓
                        aggregate report and checks
```

The model supplies behavior at runtime. Profiles, world contexts, tool choices, actions, and completion decisions are validated against explicit schemas. The framework does not substitute a scripted customer journey when inference or a tool fails.

## Participant boundaries

Profile generation receives only the participant's assigned source. A daily world request receives that participant's profile, previous world, and latest memory from an actually completed session. It receives no cohort summary or another participant's memory.

Each participant-day has a unique session identity. Tool observations are stored separately from the participant's final memory and stated decision. Observations form an append-only hash chain, so a memory claim does not become UI evidence merely because the participant wrote it.

The project contains `config.json`, an integrity ledger, and participant directories under `actors/<source_id>/`. Each participant directory contains its source, generated profile, observation log, and dated journals under `journals/<date>.json`. UI handoff files live under `broker/`, with separate `pending`, `responses`, `completed`, and `evidence` directories.

These are logical data boundaries. Workers are trusted processes with access to their execution environment; CustomerWorld is not an operating-system sandbox. A command worker or host operator must be trusted with the local project and any interface it can access.

## Daily execution and resumption

The configured timezone determines the current date. A run accepts only that date, holds a project run lock, and records progress per participant. Restarting an interrupted run continues unfinished work and skips completed participant-days. It does not manufacture historical activity or rerun a completed day.

The default concurrency is three workers. Model work can proceed concurrently, while the UI broker serializes physical UI requests across participants. The host must maintain one isolated browser context per participant/session across all tool requests. It can reuse one tab within that context or track every tab it creates. A shared interface must not mix participants' actions or observations.

## UI broker

The broker writes a request to `broker/pending/<request_id>.json` and waits for a corresponding response in `broker/responses/<request_id>.json`, relative to the project directory. The host operator performs the action using a real interface, then provides an observation and evidence.

A successful response must match the request, participant, session, and tool identities. It must include nonempty observed text, the resulting URL, and real screenshot and accessibility files within the project. The broker checks file types, hashes, path boundaries, and the expected URL origin. It rejects symlink evidence paths. Successful completion requires these checks; an unproved visit is not counted as an observed visit.

When a participant finishes, the operator must close its entire browser context, including tabs from earlier tool choices, capture the resulting state, and explicitly attest to closure. The closure response identifies the last tool; its scope covers the whole participant/session context. An interrupted handoff can be replayed by logical operation identity; this does not guarantee that a physical UI action happened only once.

Evidence supports an observation at a particular moment. Hash, identity, and closure checks do not establish that the operator acted correctly or that a screenshot is semantically true. The operator remains part of the trust boundary.

## Reporting and integrity

Aggregate reporting uses recorded execution state. It distinguishes generated profiles and contexts from decisions and proved tool visits, and separates primary-product visits from alternative-tool visits. Voluntary nonvisits are separate from broker failures, tool errors, and action-limit interruptions.

An undated summary counts distinct participant identities across all recorded dates. A participant may belong to multiple intention or error categories across those dates, so the categories are not a partition. `actor_days` separately counts recorded participant-day sessions. Use a dated report when comparing outcomes by day.

`customer-world verify` checks recorded content hashes and identity paths. It can detect inconsistency within the stored ledger; it is not a signed attestation or a substitute for reviewing the evidence. The default report export excludes participant journals and screenshots.
