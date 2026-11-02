# Sources and provenance

Sources ground a simulated participant's goal, constraints, and incumbent workflow. They do not turn that participant into a real customer.

## JSONL format

Provide one JSON object per line:

```json
{"id":"fictional-workshop","brief":"A fictional workshop organizer needs a simple way to coordinate shared equipment before weekend sessions.","provenance":{"kind":"fictional","reference":"Constructed example brief; not survey data."}}
```

Each row requires exactly `id`, `brief`, and `provenance`. Use a unique ID of up to 80 letters, digits, underscores, or hyphens, starting with a letter or digit. The brief must be nonempty. Provenance requires exactly `kind` and a nonempty `reference`.

The requested participant count cannot exceed the number of distinct source rows. Each participant is grounded in its own assigned source; profiles do not receive the other sources.

The included [example sources](../examples/sources.jsonl) are three fictional product-use briefs. They are suitable for trying the workflow, not for making claims about a customer population.

## Trust classes

| `kind` | Intended use | Interpretation |
| --- | --- | --- |
| `fictional` | A deliberately constructed brief. | A design assumption, with no empirical customer evidence implied. |
| `public` | Material available publicly, with a useful reference. | A source-grounded scenario; public availability does not establish representativeness or permission for every use. |
| `consented` | Material supplied with permission for this use. | Permission and scope must be established by the source owner; the label alone does not verify them. |

These classes are provenance declarations supplied by the project owner. CustomerWorld does not verify consent, retrieve the reference, establish ownership, or validate the truth of a brief. Use a reference that lets an authorized reviewer understand where the source came from without embedding secrets or unnecessary personal details.

## Preparing briefs

Describe the role, task, constraints, existing workflow, and reason a product might be relevant. Keep assumptions visible. Remove identifiers and details that the research does not need. Use only material you are authorized to process with the selected model provider.

Do not treat quoted interview text or a source label as a model instruction. Review input briefs before use: untrusted text can attempt to redirect a model, and schema validation alone cannot prevent that. The strongest default for a public example is a plainly fictional brief.
