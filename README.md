# CustomerWorld

Autonomous customer research for product teams.

Used at [Habitude](https://habitude.ai).

CustomerWorld runs persistent, source-grounded **simulated customers** against a product. A language model creates each profile, supplies its daily context, and decides whether to use the product, try an alternative, or do neither. Actual product interaction goes through a host-operated UI broker with screenshot and accessibility evidence.

These are synthetic research participants, not human customers. Their choices can help a team investigate product friction and form hypotheses; they are not survey responses, market demand, or validated predictions of human behavior.

## Quickstart

Requires Python 3.11 or later on macOS or Linux. The runtime uses the Python standard library.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .

customer-world init \
  --project .customer-world \
  --sources examples/sources.jsonl \
  --count 3 \
  --timezone UTC \
  --tool product=http://127.0.0.1:3000
```

Start your product at the configured URL. The example source file contains three fictional briefs; it contains no survey data. Initialization creates project state but does not generate participants or call a model.

For the OpenAI provider, set `OPENAI_API_KEY` in your environment and choose a compatible model:

```sh
customer-world generate --project .customer-world --provider openai --model YOUR_MODEL
customer-world run --project .customer-world --provider openai --model YOUR_MODEL
```

Alternatively, connect a model worker through the command adapter:

```sh
customer-world generate --project .customer-world --provider command --command-json '["your-worker"]'
customer-world run --project .customer-world --provider command --command-json '["your-worker"]'
```

`generate` and `run` request inference through the provider you select. No external inference occurs until you explicitly run one of these commands. There is no canned behavioral runner or bundled model.

While a run is waiting for UI work, a host operator opens each pending request, performs the requested action in the actual interface, and supplies the resulting screenshot and accessibility snapshot. The framework does not include a browser automator: bring a host operator or an integration that follows the [adapter contract](docs/adapters.md).

In a second terminal, inspect the pending requests:

```sh
customer-world pending --project .customer-world
```

After performing an ordinary request, reply with the actual observation and captures:

```sh
customer-world respond --project .customer-world \
  --request REQUEST_ID \
  --text-file observation.txt \
  --url http://127.0.0.1:3000 \
  --screenshot capture.png \
  --ax capture.txt
```

Use the request's ID and the files captured from that action. Keep one isolated browser context for each participant/session across tool requests. A `finish` request requires closing that entire context, including every tab it opened, and supplying `--closed`; see the [closure handoff](docs/adapters.md#closing-a-session). Repeat until the run completes.

```sh
customer-world status --project .customer-world
customer-world report --project .customer-world --output report.html
customer-world verify --project .customer-world
```

The HTML report is aggregate-only. It distinguishes profile generation, daily contexts, model decisions, primary-product visits, alternative-tool visits, voluntary skips, and errors. Screenshots and individual journals are not included in the default export.

## A 100-customer cohort

Supply 100 distinct briefs in `requests.jsonl`, then initialize a separate project:

```sh
customer-world init \
  --project .customer-world-100 \
  --sources requests.jsonl \
  --count 100 \
  --timezone UTC \
  --tool product=http://127.0.0.1:3000
```

Run `generate` and `run` against that project using your chosen provider. Worker concurrency defaults to three and is independent of cohort size. Each profile is grounded in its assigned source; initialization alone does not count as customer generation or participation. The three bundled fictional briefs are a quickstart sample, not a 100-customer dataset.

## How it works

1. Supply distinct source briefs with explicit provenance and configure the tools participants may choose.
2. Generate one profile per assigned source. Each profile sees only its own source.
3. Run the current calendar day in the project's timezone. Each participant receives its own profile, previous world state, and latest completed memory.
4. Let the model choose a tool or a voluntary nonvisit. Tool actions require actual UI observations from the broker.
5. Review aggregate outcomes and inspect local evidence when investigating a particular session.

Generation and daily runs use three workers by default. An interrupted day resumes unfinished participants; completed participants are not run again for that date. Runs cannot backfill past dates or simulate future dates. See [methodology](docs/methodology.md) for what these constraints mean.

## Documentation

- [Architecture and state](docs/architecture.md): participant isolation, resumption, evidence, and trust boundaries.
- [Provider and UI adapters](docs/adapters.md): command transport and the host-operated UI broker.
- [Sources and provenance](docs/sources.md): the input format and source trust classes.
- [Methodology and limits](docs/methodology.md): interpretation, failure handling, and planned directions.
- [Contributing](CONTRIBUTING.md): development and contribution expectations.

CustomerWorld is available under the [MIT license](LICENSE).
