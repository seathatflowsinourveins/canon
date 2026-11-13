---
status: accepted
date: 2026-11-13
decision-makers: repo owner (approved Redis, 2026-11-13, as the lead relays it); master orchestrator (Claude) for the composition, under the owner's delegation (ADR 0074)
review-by: 2027-02-11
---

# Redis carries the live mover list across processes on NautilusTrader's external message bus, installed on request from its own mise environment; open: custom data's encoding across nodes and a paper run

## Context and Problem Statement

The live list of names a mover strategy may trade is built in one process (the screener) and read by
the nodes that trade, each its own process with its own feed. Today it crosses by file. The owner
approved Redis on 2026-11-13 so that the list crosses processes on the pinned NautilusTrader's own
external message bus, whose built-in backing is Redis.

## Decision Drivers

* Upstream first: the pin already ships the producer and consumer roles and their Redis backing.
* Nothing installed around the repository's installer and its permission prompt.
* A plain install and hosted CI must not start pulling a server they do not use.

## Considered Options

1. Redis as the pin's external message bus backing: the screener's node publishes, the trading nodes
   name the stream in `external_streams`.
2. Valkey or another Redis-compatible server in its place.
3. Keep the file handoff.

## Decision Outcome

Chosen: **option 1**, on the owner's approval. The pin's docs name Redis as "the built-in external
backing for serializable messages", with a minimum of 6.2 for the `MINID` trimming autotrim uses, and
show a producer node writing a stream and a consumer node reading it through `external_streams`.
Python at the pin exposes `LiveNodeBuilder.with_external_msgbus_factory` and `RedisMessageBusConfig`
(host, port, timeouts, retries), so the paper nodes' builder can take it. Option 2 is compatible in
name only as far as read here: the pin's docs name Redis and no other server (`UNVERIFIED` for Valkey).
Option 3 is what the bus replaces.

* **Pin.** Redis `8.10.3`, the newest stable release on 2026-11-13 (8.12 is at a release candidate),
  registered as a `tool`, track `tag:8.10.3`.
* **Install.** From mise's `bus` config environment (`mise.bus.toml`), selected only by `-E bus` or
  `MISE_ENV=bus`, which a plain install and hosted CI never set: `mise -E bus install redis`. Unlike the
  gateway's (ADR 0056) it has no lockfile yet; whether the first install writes `mise.bus.lock` is
  `UNVERIFIED`. mise's first registry backend for `redis` is `conda:redis-server`, so the binary is
  conda-forge's 8.10.3 build, not one built from the tag the lock pins. The install asks the owner; no
  lane installs it any other way.
* **Scope.** Loopback only (`127.0.0.1`), with the bus's `autotrim_mins` bounding each stream. The
  built-in ingress starts each stream at the current time and does not replay what was written before
  a node started; the pin points to cache recovery or the event store for that, and how a trading node
  gets the list current at its start is `UNVERIFIED`. A consumer needs two registrations, its external
  client (`LiveDataEngineConfig.external_clients`) and the custom data class
  (`register_custom_data_class`): "Both registrations are required".
* **License.** Redis 8 is offered under RSALv2, SSPLv1 or AGPLv3, at the user's choice; 7.4 was RSALv2
  or SSPLv1, and 7.2 and earlier BSD-3. That running it locally, unmodified, as a backing service is
  within each is a reading of the licences, `UNVERIFIED`.

### Consequences

* Good: the list crosses processes by the engine's own publish and subscribe, with no file to poll.
* Good: nothing changes for a plain install, CI or any node until its builder names the factory.
* Bad: a server to run. The bus's ingress gives no backlog, so a node started late does not see the
  past entries.
* Bad: Redis 8's licences are not the BSD-3 of 7.2 and earlier.
* Open: how a custom Python data type is encoded across two nodes (the docs require JSON or
  MessagePack for custom payloads and an envelope whose `type` matches) is `UNVERIFIED` until a
  paper run publishes the list from one node and reads it in another; the code path exists at the pin
  (`py_publish_data` forwards `CustomData` to external egress). ADR 0030's limit is about the cache
  database backing with custom Python clients; the bus backing's `close()` also blocks its calling
  thread, so whether it stalls a node with Canon's custom Python clients is `UNVERIFIED` until that run.

### Confirmation

* `scripts/canon lint` holds the registry entry and this record together.
* A paper run, once installed: the screener's node publishes the list, a trading node on another
  feed receives it, and the stream key and its trim are read back from Redis. Until then the
  composition is `UNVERIFIED`.

## Evidence

Read 2026-11-13.

* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v2.0.0rc8/docs/concepts/message_bus.md —
  "Redis is the built-in external backing for serializable messages", the 6.2 minimum, producer and
  consumer nodes, `external_streams`, `stream_per_topic`, `autotrim_mins`, `types_filter`, and the
  ingress warning that pre-start entries are not replayed — fetched, and read at the pin from
  `scripts/canon src nautilus-trader` (`docs/concepts/message_bus.md:333-337,517-544,591-655,690-750`).
* `python/nautilus_trader/live/__init__.pyi:1170-1171` and
  `python/nautilus_trader/infrastructure/__init__.pyi:107-121` at the pin —
  `with_msgbus_config`, `with_external_msgbus_factory` and `RedisMessageBusConfig`'s fields — `resolved`.
* https://raw.githubusercontent.com/nautechsystems/nautilus_trader/v2.0.0rc8/docs/getting_started/installation.md —
  Redis is optional, needed only as a cache database or message bus backing — fetched.
* https://github.com/redis/redis/releases/tag/8.10.3 — the release and its notes; the tag resolves to a
  commit; `LICENSE.txt` at that tag states the tri-licence and the BSD-3 of 7.2 and earlier — fetched
  through the GitHub API.
* https://mise.jdx.dev/configuration/environments.html — a config environment loads only when selected
  (`MISE_ENV` or `-E`) — fetched; `mise ls-remote redis` lists 8.10.3 — run.
* Second reader (`evidence-verifier`), 2026-11-13: 16 of 20 claims held; corrected the install's
  likeness to the gateway (no lockfile, conda-forge's build), the licence of 7.4, the unsourced start
  read and ADR 0030's bearing, and added the two registrations a consumer needs
  (`message_bus.md:430-433,671-678`; `crates/infrastructure/src/redis/msgbus.rs:385-390`).
* The lead's archive, scout report on live market-wide lists (2026-11-13), section 5 — NautilusTrader's
  bus as the shipped means of moving a list between nodes — read; it rests on Nautilus's own docs only.
