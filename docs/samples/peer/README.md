# Sample peer records

What `multithread peer claude --tools … --json` actually prints, which is also what it writes to
`--output-dir/result.json`. Test a consumer against these instead of hand-written fakes.

| File | Call |
|---|---|
| `claude-restricted-returned.json` | A restricted call that returned; `attachments` is `[]` |
| `claude-restricted-returned-with-attachment.json` | The same with one `--attach` image |
| `claude-restricted-withheld.json` | A fault after the answer: `state: uncertain`, no result, no answer text anywhere |
| `claude-restricted-refused.json` | Refused before launch (managed settings found): `state: unavailable`, `provider_started: false` |

The repository's own tests produce each record through the real CLI, with a stand-in provider. They normalize the
values (paths become `/path/to`; IDs and times become fixed placeholders), so treat the values as illustrations and
the keys and types as the contract. `tests/runtime/test_peer_samples.py` fails whenever a live record's key paths or
value types drift from its sample, so the samples always match the source at their commit. Pin them by that commit.
