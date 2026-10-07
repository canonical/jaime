# AI diagnosis

Jaime uses an AI provider (Gemini or OpenRouter) in two places: to generate
the machine diagnostics plan, and, in `suggest` mode, to diagnose an incident
from its report. AI output is always advisory.

## Suggest mode

Jaime writes the Markdown report **before** consulting the model. In
`suggest` mode it sends that stored report to the provider and attaches the
returned root-cause description and a single suggested command to the
incident, retrievable with `get-suggestion`. The suggestion is not merged into
the report, and nothing is executed.

## Grounding

The report lists the workload's **available actions** and its **documentation
links** (machine charm), and the prompt instructs the model to base its answer
on those sources and never invent an action or URL. Because statuses always
come from the controller API, the Kubernetes charm captures the workload
status message automatically; on the machine charm that requires the observer
grant (see
[Grant the charm read access to the controller API](../how-to/grant_controller_access.md)).

## Cost and usage

Every LLM call records usage metadata (tokens, model, cost when reported) and
is aggregated per model by the `show-usage` action.

## Act mode

`act` mode is **not implemented**. Setting `mode=act` puts the charm in a
blocked state; no command is ever executed. It stays blocked until command and
policy allowlisting, bounded execution, a dry-run control, a full audit trail,
and rollback metadata exist.
