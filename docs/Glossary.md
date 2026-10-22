# Glossary

The words these docs use for HQ's own parts, each with where it is described in full.

| Word | What it is |
| --- | --- |
| Host | This repository: the application every extension runs inside. It names none of them. |
| Extension | A separate package that adds a domain (its pages, data and work) through `hq_sdk`. Called a plugin in code. [Plugins](Plugins.md) |
| Admission | An extension's own pipeline building its wheel, checking it against the host and signing it. Only an admitted wheel can be composed. |
| Cordon | The tool admission uses to verify a wheel and to lock the set of wheels that may be composed. [Plugins](Plugins.md#cordon-admission) |
| Composition | The host image plus every admitted extension, built by the Compose workflow into the one image production runs. [Deployment](Deployment.md) |
| Provider | An outside system HQ reads or acts on: a DNS host, a container manager, a tailnet, a repository host. |
| Connection | A credential HQ holds for one provider, with what it is allowed to see and do. |
| Reading | What one provider says about one kind of thing at one moment, as stored rows. [Derived Facts](Derived-Facts.md) |
| Observation | The record a reading is stored as, declared for each kind as an `ObservationRecord` in `hq/domains/control_plane/observations/`. |
| Controller | The Go program that takes readings and applies approved changes, run by a timer on the host. [Architecture](Architecture.md) |
| Bridge | How the controller talks to HQ: HTTP on a Unix socket HQ's process serves. |
| Derivation | An answer computed from stored rows (the topology, the queue, a page's figures) and kept until a table it reads changes. |
| Derive-ahead pass | The thread that recomputes what a write made stale, so the next page finds it ready. |
| Finding | A problem a rule finds in the readings, with what to do about it. |
| Queue | Everything that needs a decision, from findings and every domain, ordered by severity and age. |
| Capability | A named action, with the input it takes and who may run it; every interface (web, API, MCP, CLI) acts through one. [API](API.md) |
| Principal | Who is acting, through which interface, with which capabilities. |
| Outbound work | Work a domain does outside the process, declared once and run as a job; it may declare a cadence. [Plugins](Plugins.md) |
| Tailnet | The private network HQ and its machines reach each other on. |
