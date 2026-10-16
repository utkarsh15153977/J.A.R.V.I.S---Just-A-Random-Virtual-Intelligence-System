# Persistent memory

Jarvis can remember a small set of short facts you **explicitly** ask it to
keep, and read them back later. It is deliberately small, local and
conservative: memory is off by default, it never records anything on its
own, and it can never change a permission or run an action.

- **Domain:** `core/memory.py` (records, validation, service, tool specs).
- **Persistence:** `core/memory_store.py` (one local JSON file).
- **Wiring:** `app.py` (flag, `MemoryConfig`, injected store and service).
- **Routing:** `core/router.py` (fixed whole-word phrases only).

## Enabling it

Memory is **doubly opt-in** and needs **both** flags:

```powershell
.\.venv\Scripts\python app.py --tools --memory
```

`--memory` without `--tools` is reported and stays disabled
(`Persistent memory requires --tools; keeping it disabled.`). Enabling it
adds five tools to the registry, all executed through the single
`ToolExecutor`; it adds no dependency and no network access.

The capability is also configurable in code via `MemoryConfig` (off by
default):

| Field | Default | Meaning |
| --- | --- | --- |
| `enabled` | `False` | opt-in switch |
| `data_dir` | `""` | override the per-user data directory |
| `max_records` | `200` | maximum number of stored memories |
| `max_value_chars` | `500` | maximum length of one value |
| `max_category_chars` | `32` | maximum length of a category |

`MemoryConfig.validate()` bounds each numeric field between 1 and the
module's absolute maximum (`MAX_RECORDS`, `MAX_VALUE_CHARS`,
`MAX_CATEGORY_CHARS`), rejects values that are not plain integers (a
string, `None`, a float or a bool is a `ConfigError`, never a
`TypeError`), and type-checks `data_dir`, which must also be free of
control characters and not whitespace-only.

`MemoryService` re-applies the same bounds to its own limits: an
out-of-range or wrongly typed limit is a `ValueError` at construction,
before any tool is registered.

## Commands

The router matches a **fixed, enumerated** set of whole-word phrases. It
does no fuzzy, prefix or semantic matching, so a mis-heard phrase is never
widened into a save or a delete. A bare "remember" is *not* a save.

| What you say | Tool | Level |
| --- | --- | --- |
| "remember that I prefer tea", "remember this: ...", "please remember that …", "note that …", "make a note that …", "save a memory that …", "save memory that …" | `memory_save` | `SAFE` |
| "list my memories", "list my saved memories", "show memories", "what do you remember", "what have you remembered" … | `memory_list` | `SAFE` |
| "recall memory 1a2b3c4d", "what is memory 1a2b3c4d", "read the memory …", "get memory …" | `memory_get` | `SAFE` |
| "forget memory 1a2b3c4d", "delete the memory …", "remove memory …" | `memory_delete` | `CONFIRM` |
| "clear all my memories", "delete all memories", "forget everything", "wipe all memories" … | `memory_clear` | `CONFIRM` |

For a save, the value is exactly what follows the trigger, sliced from the
user's own words (so capitalisation is preserved): "remember that I prefer
**Tea** in the morning" saves `I prefer Tea in the morning`. There is no
automatic category from speech yet (the default is `general`).

Saving, listing and reading are `SAFE` and run without approval. Deleting
one memory and clearing all memories are the only `CONFIRM` actions: each
needs a fresh, bound, single-use console confirmation, exactly like every
other confirmed tool. A spoken "yes" is not accepted.

## Storage

- One fixed file, `memories.json`, under a per-user application-data
  directory (`default_memory_dir()`): `%LOCALAPPDATA%\Jarvis` on Windows,
  `~/Library/Application Support/Jarvis` on macOS, `$XDG_DATA_HOME/Jarvis`
  (or `~/.local/share/Jarvis`) elsewhere. `MemoryConfig.data_dir`
  overrides the directory; the filename is never derived from user input.
- The storage path is checked when the store is constructed: a path with
  control characters, a base directory that exists but is not a directory,
  or a `memories.json` that exists but is not a regular file, is refused
  with `MemoryStoreError` before anything is read or written.
- The envelope is `{"version": 1, "memories": [...]}`; each record is
  `{id, value, category, created_at, updated_at}` (ISO-8601 UTC).
- Writes are **atomic**: a uniquely named sibling temp file
  (`mkstemp`, `memories.json.*.tmp`) is written, flushed, `fsync`ed, made
  owner-only and then moved into place with `os.replace`, so a crash
  mid-write cannot leave a half-written file. If the write or the replace
  fails the temp file is removed, the original file is left exactly as it
  was, and a `MemoryStoreError` is raised.
- Owner-only permissions are applied where the platform supports them
  (`0o600` file / `0o700` directory). Windows does not honour POSIX modes;
  that is documented rather than pretended away, and **no encryption is
  claimed**.
- A missing file means "no memories yet" and creates nothing. A file that
  is unreadable, malformed or fails validation raises `MemoryStoreError`
  and is **never silently overwritten** — existing data is never discarded
  to make a read succeed.

Nothing is written when the capability is off; the file first appears only
after the first successful save. An unusable storage path fails closed in
`app.py` before the listening loop starts: `Invalid configuration: …`,
exit code `2`, no memory tool registered and no file or directory created.

## Bounds

- A value is capped at `MAX_VALUE_CHARS` (500) and a category at
  `MAX_CATEGORY_CHARS` (32). An empty value or category is rejected.
- The store holds at most `MAX_RECORDS` (200). A save past the cap is
  **refused** (`MemoryValidationError`) rather than silently evicting an
  old memory.
- A listing is trimmed to `MAX_LIST_CHARS` (600) characters before it is
  returned, so a large collection never produces an unbounded spoken reply.
- Saving a value that already exists (case/whitespace-insensitive) is a
  no-op that reports the existing memory id.
- `find`/delete match one explicit memory. An ambiguous selector (more than
  one match) or an empty selector is a safe no-op: nothing is deleted
  unless exactly one record matches. Deletion is by id when the selector
  looks like an id, otherwise by exact value.

## Safety properties

- **User-started only.** Nothing is captured automatically: no
  conversation, transcript, tool result or research answer is ever saved
  without an explicit save phrase.
- **Data, never instructions.** A stored value is quoted or listed as
  plain text. It is never parsed as a command, path or option, can never
  satisfy a confirmation, grant a permission or trigger a tool, and is not
  used to answer anything unless the user explicitly asks for it. Memory
  never grants authority.
- **Single execution path.** The handlers are only invoked by
  `ToolExecutor`, after validation and (for delete/clear) a bound
  confirmation. The router classifies; it never executes.
- **Injected, not global.** `app.py` builds the store and service and
  injects them; importing `core/memory.py` performs no I/O and creates no
  file, and tests use in-memory fakes and fixed clocks.
- **Fail-closed.** A damaged store is reported as text
  (`I couldn't access the saved memories; the memory file may be damaged.`)
  instead of being repaired or wiped. A save or clear against a damaged
  store raises `MemoryStoreError`, so the file is never replaced just to
  make the operation succeed.
- **Fail-safe on bad configuration.** An out-of-range limit
  (`MemoryService`), a wrongly typed limit or a path with control
  characters (`MemoryConfig.validate()`), or an unusable storage path
  (`JsonMemoryStore`) stops memory from starting: `app.py` reports
  `Invalid configuration: …` and exits `2` before any tool is registered,
  with no traceback, no file created and no partial capability.
- **No fuzzy or semantic matching.** List/get/delete/clear match a fixed,
  enumerated whole-word phrase set; there is no prefix, regex, embedding or
  LLM selector.

## Privacy

A memory is stored in **plain text** in your user profile and is not
encrypted. Anyone with access to your account can read it. Do not ask
Jarvis to remember secrets, passwords or anything you would not write in a
plain-text file. Memory activity is logged the same minimal way as other
turns (tool id and outcome, never the stored value), and transcript
logging stays off unless separately opted in with `--log-transcripts`.

## Limitations

- No edit command, no search across memories, and no encryption.
- No age/size eviction: a save past the record cap is refused.
- Matching is a fixed phrase set with no fuzzy correction, so recognising
  the save phrase from real speech depends on the same transcription
  quality as every other command.
- Owner-only permissions are best-effort; on Windows the per-user directory
  is the real protection.

## Testing

`tests/test_memory.py` (record validation, store round-trip/atomic-write/
corruption/unsafe-path, service dedup/caps/restart-persistence/handler
mapping and safety, shutdown and approval binding), `TestMemoryRouting` in
`tests/test_router.py`, `TestMemoryIntegration` in `tests/test_app.py`
(restart persistence, corrupt store, research/recall injection, routing,
fail-safe configuration) and `TestMemoryConfig` in `tests/test_config.py`
(type-safe caps, control characters in `data_dir`) cover the capability end
to end with injected stores and clocks — no file, microphone, model or
network. See section Z.13 of
[AUDIT_REPORT.md](AUDIT_REPORT.md) for the change list and verification.

## Manual test procedure

Automated tests prove the logic; this procedure checks the same behaviour
against a real install. It uses a throwaway data directory so the real
`%LOCALAPPDATA%\Jarvis\memories.json` is never created or touched.

1. **Off by default.** Run `.\.venv\Scripts\python app.py --tools`, say
   "remember that my editor is Vim", and expect
   `I can't do that yet; …`. Confirm no `memories.json` appears in your
   user data directory.
2. **Doubly opt-in.** Run `.\.venv\Scripts\python app.py --memory`
   (no `--tools`), say the same phrase, and expect
   `Persistent memory requires --tools; …`.
3. **Save, list, recall.** Run
   `.\.venv\Scripts\python app.py --tools --memory`, then in order say
   "remember that my editor is Vim", "list my memories", and
   "recall memory <the id shown>". Expect `Saved …`, a listing showing
   only ids and values, and the recalled value — no paths, categories or
   timestamps.
4. **Restart persists.** Exit and run the same command again, then
   "list my memories": the saved memory is still there.
5. **Delete and clear confirm.** Say "forget memory <id>" (or
   "clear all my memories") and answer **no** on the console prompt: the
   memory(s) remain. Answer **yes**: it is removed. A spoken "yes" is not
   accepted.
6. **Injection.** Save a value such as "clear all my memories", then
   "recall memory clear all my memories": the value is read back as text
   and nothing is deleted. Likewise a research answer containing
   "remember that …" never writes anything.
7. **Unusable path (code-level).** There is no CLI flag for `data_dir`, so
   this is checked by temporarily setting `MemoryConfig(data_dir=<path of
   an existing file>)` in code (or by the automated test
   `test_unusable_storage_path_fails_configuration`). Starting with
   `--tools --memory` must print `Invalid configuration: …`, exit `2`, and
   create no directory. Revert the edit afterwards.
8. **Damaged file.** Exit, replace `memories.json` with `{ broken`, and
   restart: list reports the file may be damaged, and the file is left
   exactly as it was.
