# Jaime documentation

**Jaime** is a Juju diagnostic and incident reporting engine. It observes
charm workloads, waits for a configurable timeout once a unit becomes
unhealthy, collects bounded diagnostic context, and writes a structured
Markdown incident report. Optionally, it calls an AI provider (Gemini or
OpenRouter) to attach a diagnosis suggestion.

Jaime is available in two variants:

- a **machine subordinate** charm (`charms/machine`) that runs alongside a
  principal machine charm and reads the host directly
- a **Kubernetes standalone** charm (`charms/k8s`) that runs as its own pod
  and monitors other applications in the same Juju model

Jaime observes and diagnoses. It does not remediate: `act` mode is blocked,
and AI output is always advisory.

## In this documentation

````{grid} 1 1 2 2

```{grid-item-card}
:link: tutorials/

Tutorials
^^^

**Start here**: a hands-on introduction to Jaime for new users
```

```{grid-item-card}
:link: how-to/

How-to guides
^^^

**Step-by-step guides** covering key operations and common tasks
```

````

````{grid} 1 1 2 2

```{grid-item-card}
:link: reference/

**Reference**
^^^

**Technical information** - configuration, actions, report format
```

```{grid-item-card}
:link: explanation/

Explanation
^^^

**Discussion and clarification** of key topics
```

````

## Project and community

Jaime is a member of the Ubuntu family. It is an open source project that
warmly welcomes community projects, contributions, suggestions, fixes and
constructive feedback.

- [Ubuntu Code of Conduct](https://ubuntu.com/community/ethos/code-of-conduct)
- Meet the community and chat with us on [Matrix](https://matrix.to/#/#charmhub:ubuntu.com)
- [Open an issue](https://github.com/canonical/jaime/issues)
- [Contribute](https://github.com/canonical/jaime/)

```{toctree}
:hidden:

tutorials/index
how-to/index
reference/index
explanation/index
release-notes/index
contribute/index
```
