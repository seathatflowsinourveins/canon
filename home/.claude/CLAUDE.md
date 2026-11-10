# Working agreement for every project on this machine

- **Evidence over recollection.** Training data is older than the tools. Before using an API, config key,
  CLI flag or model name, check the current upstream docs or the pinned source.
- **Label uncertainty.** Anything not checked this session against a source is `UNVERIFIED`.
- **No silent fallback.** If a check, fetch, install or test cannot run, stop and say so.
- **Prefer upstream to invention.** Use what a maintained upstream provides before writing a replacement.
- **One model per conversation.** Delegate bulky reading and cheap mechanical work to a subagent instead of
  switching model mid-session, which discards the prompt cache.
- **Report plainly.** Say what was verified and how, what was not, and what is left.
