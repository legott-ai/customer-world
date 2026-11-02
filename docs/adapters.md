# Provider and UI adapters

CustomerWorld asks a model what a participant does and asks a host operator what the interface actually shows. These are separate contracts.

## Model providers

The Python provider interface is:

```python
complete(task: str, payload: dict, schema: dict) -> Completion
```

`Completion` contains `data` and optional `model` and `usage` metadata. `data` must satisfy the supplied schema. Profile, world, and turn schemas require every declared field and reject additional fields. Use empty strings for turn fields that do not apply.

The built-in OpenAI provider uses the Responses API. It reads `OPENAI_API_KEY` only when explicitly instantiated, and requires a model selected by the user. Importing the package does not make a network request. API access and any inference charges belong to the user's configured provider.

### Command transport

Pass an argument vector as JSON:

```sh
customer-world generate --project .customer-world \
  --provider command --command-json '["your-worker"]'
```

For each completion, the adapter starts a fresh process without a shell. It sends one JSON object to standard input with these fields:

| Field | Meaning |
| --- | --- |
| `task` | The requested model operation. |
| `payload` | The participant-scoped context for that operation. |
| `schema` | The JSON Schema the returned data must satisfy. |

The worker must return one JSON data object on standard output, without a `Completion` wrapper. Standard error is discarded; avoid emitting credentials or source content in diagnostics. A nonzero exit, timeout, invalid JSON, or invalid result fails the request. The command adapter's default timeout is 120 seconds.

The child receives only a minimal system `PATH` and locale settings. Parent environment variables, including credentials, are not forwarded. Arrange model access within the trusted worker and use an executable the adapter can resolve.

A worker should use the supplied payload and schema to obtain a real model response. A fixed output script can exercise transport in a test, but does not implement autonomous customer behavior.

## Host-operated UI transport

The file broker's default response timeout is 300 seconds. Pending requests and responses live in `broker/pending/` and `broker/responses/` within the project. Use `customer-world pending --project .customer-world` to inspect the pending requests. A request contains:

| Field | Meaning |
| --- | --- |
| `request_id` | Unique request identity. |
| `actor_id` | The participant making the request. |
| `session_id` | That participant's current session. |
| `tool_id` | The configured tool to operate. |
| `action` | The requested operation, including opening or finishing a session. |
| `target` | The action target, or an empty string. |
| `value` | The action input, or an empty string. |
| `expected_url` | The URL whose origin the successful observation must match. |

The host operator reads a pending request, uses the actual interface, captures a screenshot and an accessibility text snapshot, and returns the resulting observed text and URL. Maintain one isolated browser context per participant/session across requests and tools. Reuse a single tab within that context or track every tab the context creates. CustomerWorld has no built-in browser driver. The host may supply its own browser or desktop automation, subject to this contract.

The built-in model adapter receives the observation text and URL, not the screenshot pixels. Images remain local proof and are not automatically uploaded for model vision. Supply complete, usable accessibility information and observed controls in `--text-file`; a short status summary can hide the very interface the participant needs to operate. Describe what is visible without adding hidden-feature hints or instructions about which choice to make.

Responses must repeat the exact `request_id`, `actor_id`, `session_id`, and `tool_id`. The remaining fields are:

| Field | Meaning |
| --- | --- |
| `status` | `ok` or `error`. |
| `observation` | An object containing `text`, `url`, and `tool_id`, plus explicit closure information for a successful `finish`. |
| `evidence` | A list of objects containing `path` and `sha256`. |

An `ok` response requires nonempty observed text, the matching tool, a URL at the expected origin, and both an accessibility `.txt` file and a PNG or JPEG screenshot. Evidence files must be real, nonsymlink files within the project, with matching SHA-256 hashes. Empty files or invented paths do not prove a visit.

The broker's `respond` helper copies supplied proof into the project and writes the response atomically. Host integrations should use this helper instead of constructing partially written responses. An error response reports failure and does not count as a voluntary nonvisit or a successful interaction.

### Closing a session

A `finish` request is a UI operation. Close the participant's entire browser context, including all tabs created for earlier tool choices, then capture the resulting interface and provide a closure observation:

```sh
customer-world respond --project .customer-world \
  --request REQUEST_ID \
  --text-file closure.txt \
  --url '' \
  --screenshot closed.png \
  --ax closed.txt \
  --closed
```

Use the actual resulting URL when one exists; use an empty URL for a blank state. A closure state may be at a different origin. The response records `closed: true` and `closed_tool_url` in its observation. The latter identifies the last tool, while `--closed` attests that the entire participant/session context has been closed. This is an operator attestation backed by captured evidence; the broker cannot verify browser-context destruction at the operating-system level.

If the requested operation cannot be completed, use `respond --project .customer-world --request REQUEST_ID --error 'Unable to complete the requested operation.'`. Report the failure instead of inventing a successful observation.

The broker serializes requests across workers, but does not sandbox the operator or command processes or guarantee exactly-once physical execution after an interruption. Use a dedicated, appropriately scoped product session and keep the local project private when it contains sensitive material.
