# v12 round notes - trees + metadata

built on your canary tip `3331939` (after your v11 apply, the release
0.8.1 pass, logo/ss, bulk removes + iterator fix). the patch applies
cleanly to it:

    git apply leech-v12.patch

## 1. what landed (your two asks)

**tree todos** - your friend's spec, implemented as written:

> "start tree <name>, makes a tree task, after this starts, next tasks
> are made with the form of (say tree task number is 46) 46a, 46b,
> 46c... and so on. stop tree stops this, makes the next task made 47.
> if you want to add another task to tree after you did stop and added
> more tasks, you do >tree start <tree task number or name>."

- `>tree start oklang rewrite` - creates the root task (numbered like
  any task, say 46) AND arms it. every `>todo add` after that grows as
  `46a`, `46b`, `46c`... (`>todo adds` batches branch too, a, b, c
  in order).
- `>tree stop` - disarms. the next task takes the next fresh number
  (47 in the example - branches never consume numbers).
- `>tree start 46` or `>tree start oklang rewrite` - resumes that tree
  (by number OR by name, case-insensitive): new tasks continue as
  `46d`, `46e`... even if 47, 48, 49 were made in between.
- `>tree list` - shows the trees on the active list, with branch
  counts and which one is growing.
- branches are full tasks: priorities, dues, reminders, `>todo update
  index=46b`, `>todo remove index=46b`, `>todo info index=46b` all
  work. letters go `z` -> `aa` -> `ab` (spreadsheet style) if a tree
  somehow gets past 26 branches.
- trees are scoped per list (arming happens on the active list at
  `start` time), survive reloads, and are undoable like everything
  else. if an armed root task gets removed, adds quietly fall back to
  normal numbering instead of breaking.

**task metadata** - every task now carries `created_at`, `edited_at`,
`edited_count`, bumped on every real mutation (content edits, priority/
due changes, snoozes). see them with `>todo info index=46b`:

    task 46b: fix the lexer once and for all
    priority: 🔴 high
    due: ⏰ in 2h (Mon 11-01 15:00)
    created: Mon 11-01 14:02 (2h ago)
    edited: 3 time(s), last Mon 11-01 15:10 (34m ago)
    tree: branch of 46 'oklang rewrite'
    added by: <@you>
    list: 'main'

times render in your `>todo tz`. tasks older than this feature get
their creation backfilled from their birth stamp (the task id is a
ms epoch); truly ancient ones say `created: unknown`.

## 2. the one visible change to existing behavior: stable numbers

task numbers used to be positions (0-based, shifting on every
removal). for trees to make sense (`46` must keep meaning 46), numbers
are now **1-based and never reused**: every task gets a number at
creation, keeps it forever, and what you see in `>todo list` is what
you type into remove/update/info.

- day one after applying, your existing tasks show position+1 (the
  first task says 1 instead of 0). that shift happens exactly once,
  on first load - then numbers are stable forever.
- `>todo removes start=2 end=5` is now a range of NUMBERS, and tree
  branches ride their root's number - a range covering a tree root
  removes the whole subtree.
- `>remind` list is numbered the same way (`reminder 7 set: ...`).

## 3. compatibility (nothing to run)

- legacy rows (json or supabase) load unchanged: tasks without a
  `number` get position+1 in memory; `created_at` backfills from the
  id. no migrator run, no schema change - the new fields ride the
  existing `todo` namespace rows, jsonb key shuffle and all (the
  battery re-checks the shuffle invariant with an armed tree).
- undo snapshots from before this patch restore fine (they just lack
  the new fields, same shim applies).
- `>todo list` shows tree roots with a 🌳 and branches with their
  label - nothing else about rendering changed.

## 4. verification

- repo suite: **144 passed** (126 carried + 18 new in
  `tests/cogs/todo/test_trees_meta.py`: the full friend lifecycle,
  stable numbers, past-z letters, subtree range removal, scoped trees,
  armed-root-vanished fallback, metadata stamps, snooze-counts-as-edit,
  legacy reconcile, jsonb round-trip of an armed tree, undo of a
  branch add).
- `scripts/verify_task46.py`: **GREEN, 8 checks** - the friend's exact
  scenario verbatim, stable numbers, past-z + subtree removal,
  metadata, legacy payload load, armed-root fallback, key-shuffle
  survival, full pytest.
- black'd.

## 5. apply order

    git apply leech-v12.patch   # bot stopped or about to restart
    start the bot               # first load numbers your existing tasks

that's it - no migration, no .env changes.

## 6. not touched

your canary-side work (removes subcommands, iterator fix, OpposingFork
link, logo/ss, the readme passes) is the base, untouched. README got
the two new bullets + the test count. backlog (AI driving the pager,
kaggle TTS verdict, master merge) unchanged.
