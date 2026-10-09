# Cleanup plan: worktrees, branches and stashes (2026-10-08)

A **plan only**. Nothing here has been deleted. Gathered on 2026-10-08 at about 22:00 PDT from
`git worktree list --porcelain`, `git branch -vv`, `git branch -r`, `git branch --merged origin/main`,
`git branch --merged origin/gatekeeper-p1`, `git branch -r --contains <sha>`, `git for-each-ref` (committer dates),
`git stash list`, `gh pr list` and the directories' modification times. `origin/main` = `53112da`,
`origin/gatekeeper-p1` = `217bda3`. No pull requests are open.

**What this plan could not check:** whether each worktree has **uncommitted changes**. It was written from an
isolated agent worktree, and the harness refuses `git -C <other worktree> status` from there. Before deleting any
worktree, run this from `~/Desktop/openmob` and treat any output as a stop:

```sh
git worktree list --porcelain | awk '/^worktree /{print $2}' | while read -r wt; do
  n=$(git -C "$wt" status --short 2>/dev/null | wc -l | tr -d ' '); echo "$n  $wt"; done | sort -rn
```

Rules applied, from the owner's instructions:
- never delete `main`, `gatekeeper-p1`, `archive/*`, or the scratchpad `wt-*` worktrees (`wt-gk` and `wt-int` are
  in use by the main session);
- never delete worktrees modified today by active agents, nor `test/phase5-evil-server` or
  `perf/first-action-polish` (the latter does not exist locally or on `origin` yet: the lane hasn't pushed);
- "merged" means every commit is reachable from `origin/main` or `origin/gatekeeper-p1`. Branches whose tip is a
  *merge commit on main* (the `worktree-agent-*` branches that sat at a PR merge) count as merged too;
- a branch that only ever pointed at a commit already on `main` (no commits of its own) is listed as "empty".

## Summary

| Recommendation | Worktrees | Local branches | Remote branches (`origin/`) | Stashes |
|---|---|---|---|---|
| Keep / in use | 17 | 9 | 4 | 0 |
| Safe to delete | 20 | 59 | 37 | 0 |
| Owner decision | 2 | 8 | 1 | 2 |
| **Total** | **39** | **76** | **42** (plus `origin/HEAD`) | **2** |

The totals count the 39 registered worktrees (the main checkout included), the 76 local branches (this plan's
`docs/agents-md-2026-10-08` included) and the 42 remote branches. "Safe to delete" always means *after* the dirty
check above comes back empty for that worktree.

The deletions, if the owner agrees, come to: `git worktree remove <path>` for 20 worktrees (then
`git worktree prune`), `git branch -d` for 59 local branches, and `git push origin --delete` for 37 remote
branches. Most of the remote ones are the heads of PRs #1–#21, which GitHub would have deleted on merge had the
"delete branch on merge" setting been on; turning that on avoids this list growing again.

## Worktrees (39)

| Path | Branch / HEAD | Last commit | Dir modified | Merged into | Recommendation |
|---|---|---|---|---|---|
| `~/Desktop/openmob` | `main` `53112da` | 2026-10-08 | — | — | **keep** (the standard environment) |
| `~/Desktop/openmob-gatekeeper` | detached `217bda3` (= `gatekeeper-p1`) | 2026-10-08 | 2026-10-08 20:07 | gk | **keep** (the gatekeeper environment; the owner's server runs here) |
| `…/c43c1ed7…/scratchpad/wt-gk` | `gatekeeper-p1` `217bda3` | 2026-10-08 | 2026-10-08 20:03 | gk | **in use** (main session) |
| `…/c43c1ed7…/scratchpad/wt-int` | detached `7aa74b8` "int" | 2026-10-08 | 2026-10-08 19:59 | **neither** (local-only integration commit, on no remote branch) | **in use** (main session) |
| `…/c43c1ed7…/scratchpad/wt-bench` | detached `4a892b0` "bench: gatekeeper (fixed) + fewer-model-calls" | 2026-10-08 | 2026-10-08 12:36 | **neither** (on no remote branch) | **keep** (scratchpad rule); note the commit is reachable only from this worktree, so removing it would orphan it. Main session's call |
| `…/c43c1ed7…/scratchpad/wt-clean` | `cleanup/remove-voice-memory` `2111cd4` | 2026-10-08 | 2026-10-08 00:51 | main, gk (PR #2) | **keep** (scratchpad rule); done, main session's call |
| `…/c43c1ed7…/scratchpad/wt-haiku` | `tune/haiku-high-effort` `a10051a` | 2026-10-08 | 2026-10-08 08:53 | main, gk (PR #6) | **keep** (scratchpad rule); done, main session's call |
| `…/c43c1ed7…/scratchpad/wt-inv` | `feature/signup-invites` `8cc1993` | 2026-10-08 | 2026-10-08 08:47 | main, gk (PR #5) | **keep** (scratchpad rule); done, main session's call |
| `…/c43c1ed7…/scratchpad/wt-om` | `chore/om-launcher` `23623ec` | 2026-10-08 | 2026-10-08 00:58 | main, gk (PR #4) | **keep** (scratchpad rule); done, main session's call |
| `…/c43c1ed7…/scratchpad/wt-prov` | `feature/model-providers` `20a544c` | 2026-10-08 | 2026-10-08 00:54 | main, gk (PR #3) | **keep** (scratchpad rule); done, main session's call |
| `…/c43c1ed7…/scratchpad/wt-uifix` | `ui/review-fixes` `97969e6` | 2026-10-08 | 2026-10-08 00:24 | main, gk (PR #1) | **keep** (scratchpad rule); done, main session's call |
| `…/cbf6ddf8…/scratchpad/wt-modes` | `feature/permission-modes` `c514ed4` | 2026-10-05 | 2026-10-05 20:10 | main, gk | **keep** (scratchpad rule); an older session's, done. Main session's call |
| `…/cbf6ddf8…/scratchpad/wt-release` | detached `ee17ef3` | 2026-10-05 | 2026-10-05 22:30 | main, gk | **keep** (scratchpad rule); an older session's, done. Main session's call |
| `/private/tmp/openmob-landing-auth` | `ui/landing-auth` `48aa85b` | 2026-10-07 | 2026-10-06 23:58 | main, gk | **safe to delete** (Codex's UI worktree; merged via `d42cb16`) |
| `/private/tmp/openmob-ui-merge-check` | detached `229f535` (= `ui/recent-fixes`) | 2026-10-07 | 2026-10-07 01:04 | main, gk | **safe to delete** (a merge check; merged) |
| `.claude/worktrees/agent-a04d0b072541f72d7` | `docs/agents-md-2026-10-08` (this plan) | 2026-10-08 | today, **locked** | not yet (its commit is the handoff rewrite) | **in use** (this housekeeping agent); remove after its PR |
| `.claude/worktrees/agent-a50274e357295e79c` | `worktree-agent-a50274e357295e79c` `53112da` | 2026-10-08 | today 21:59, **locked** | main (empty branch) | **in use** (an agent started today; likely the latency-polish lane before its first commit) |
| `.claude/worktrees/agent-a61cdb596b329c044` | `test/phase5-evil-server` `217bda3` | 2026-10-08 | today 21:59, **locked** | gk (no commits of its own yet) | **in use** (the Phase 5 lane) |
| `.claude/worktrees/agent-a4c1e42f423b662e4` | `feature/app-links` `cadc255` | 2026-10-08 | today 19:37, **locked** | main, gk (PR #16) | **keep for now** (locked by a live agent process; merged, so once the lock clears it is safe) |
| `.claude/worktrees/agent-a6943e7892f901dfc` | `perf/fewer-model-calls` `d0b8b13` | 2026-10-08 | today 13:05 | main, gk (PR #13) | **owner decision**: modified today, not locked, merged. Delete if its agent is gone |
| `.claude/worktrees/agent-a7f9e89aeef89be15` | `feature/deep-links-gk` `0603e0e` | 2026-10-08 | today 13:23 | gk | **owner decision**: modified today, not locked, merged into gk. Delete if its agent is gone |
| `.claude/worktrees/agent-a090160410fc3e7df` | `no-install-ask-gk` `80b2fee` | 2026-10-06 | 2026-10-06 | gk | **safe to delete** |
| `.claude/worktrees/agent-a144ad0044823fb32` | `worktree-agent-a144ad0044823fb32` `f980c3b` | 2026-10-05 | 2026-10-05 | main, gk | **safe to delete** |
| `.claude/worktrees/agent-a181deb4f5b5930b4` | `worktree-agent-a181deb4f5b5930b4` `d0c803e` | 2026-10-05 | 2026-10-05 | main, gk | **safe to delete** |
| `.claude/worktrees/agent-a1a5318d7c22f1577` | `worktree-agent-a1a5318d7c22f1577` `442eec9` | 2026-10-05 | 2026-10-05 | main, gk | **safe to delete** |
| `.claude/worktrees/agent-a21eb11d13741444e` | detached `fb2c286` (hidden permission dialogs, gk) | 2026-10-06 | 2026-10-06 | gk | **safe to delete** |
| `.claude/worktrees/agent-a27f52f1492fa5ff8` | `worktree-agent-a27f52f1492fa5ff8` `7b64743` | 2026-10-05 | 2026-10-05 | main, gk | **safe to delete** |
| `.claude/worktrees/agent-a33830f4ee1ec1c86` | `wait-until-gk` `1081fd0` | 2026-10-06 | 2026-10-06 | gk | **safe to delete** |
| `.claude/worktrees/agent-a48b8d780fbd1e418` | `worktree-agent-a48b8d780fbd1e418` `55934e5` | 2026-10-05 | 2026-10-05 | main, gk | **safe to delete** |
| `.claude/worktrees/agent-a4ad909dd39ee1a85` | `worktree-agent-a4ad909dd39ee1a85` `881c0a7` | 2026-10-05 | 2026-10-05 | main, gk | **safe to delete** |
| `.claude/worktrees/agent-a557b325c7608f076` | `archive/mirror-draft-2026-10-04` `59ed0bd` | 2026-10-06 | 2026-10-04 | neither (archive; pushed) | **safe to delete the worktree**, keep the branch (archive rule). The branch is on `origin` |
| `.claude/worktrees/agent-a8915fa3cbe145a63` | `gatekeeper-b7` `121f053` | 2026-10-06 | 2026-10-05 | gk | **safe to delete** |
| `.claude/worktrees/agent-a94442e39f4f9e8a1` | `worktree-agent-a94442e39f4f9e8a1` `266f28e` | 2026-10-05 | 2026-10-05 | main, gk | **safe to delete** |
| `.claude/worktrees/agent-aa50554eed72fe025` | `worktree-agent-aa50554eed72fe025` `64c479c` | 2026-10-05 | 2026-10-05 | main, gk | **safe to delete** |
| `.claude/worktrees/agent-aafda1b7f5ea89a28` | `work` `b34e54d` (= `feature/easy-connect`) | 2026-10-05 | 2026-10-05 | main, gk | **safe to delete** |
| `.claude/worktrees/agent-ab4c5049c2618b04b` | `worktree-agent-ab4c5049c2618b04b` `17c4e63` | 2026-10-05 | 2026-10-05 | main, gk | **safe to delete** |
| `.claude/worktrees/agent-abd823bf6d198a12f` | `worktree-agent-abd823bf6d198a12f` `766393a` | 2026-10-05 | 2026-10-05 | main, gk | **safe to delete** |
| `.claude/worktrees/agent-ae04c587c30db8118` | `worktree-agent-ae04c587c30db8118` `4732096` | 2026-10-05 | 2026-10-05 | main, gk | **safe to delete** |
| `.claude/worktrees/agent-af42369fe7739525c` | `gaps-fix` `6b51d40` | 2026-10-05 | 2026-10-05 | main, gk | **safe to delete** |

Counts: **keep / in use 17** (the two environments, the 11 scratchpad `wt-*`, and the four locked agent worktrees:
this one, a50274…, a61cdb…, a4c1e4…); **owner decision 2** (a6943e…, a7f9e8…: modified today, unlocked, merged);
**safe to delete 20** (the two `/private/tmp` ones and 18 agent worktrees from 2026-10-04 to 10-06), after the
dirty check. Of the four locked ones, a4c1e4… (`feature/app-links`) is merged and becomes safe the moment its
lock clears.

Note on detached `wt-bench` (`4a892b0`) and `wt-int` (`7aa74b8`): those two commits exist on no branch. If the
main session wants to keep them past the worktrees, it should put a branch or tag on them (`git branch keep/bench
4a892b0`); otherwise `git gc` will eventually drop them once the worktrees are gone.

## Local branches (76)

Dates are committer dates. "upstream" is from `git branch -vv`. Merged = reachable from `origin/main` (main) or
`origin/gatekeeper-p1` (gk).

### Keep (9)
| Branch | Tip | Date | Why |
|---|---|---|---|
| `main` | `53112da` | 2026-10-08 | rule |
| `gatekeeper-p1` | `217bda3` | 2026-10-08 | rule |
| `archive/mirror-draft-2026-10-04` | `59ed0bd` | 2026-10-06 | archive rule; also on `origin` |
| `test/phase5-evil-server` | `217bda3` | 2026-10-08 | active lane (rule) |
| `docs/agents-md-2026-10-08` | this plan's commit | 2026-10-08 | this handoff, awaiting PR |
| `worktree-agent-a04d0b072541f72d7` | `53112da` | 2026-10-08 | this agent's worktree branch (empty); goes with the worktree |
| `worktree-agent-a50274e357295e79c` | `53112da` | 2026-10-08 | a live, locked agent (empty so far) |
| `worktree-agent-a61cdb596b329c044` | `53112da` | 2026-10-08 | the Phase 5 agent's worktree branch (empty; its worktree is on `test/phase5-evil-server`) |
| `worktree-agent-a4c1e42f423b662e4` | `4a71875` | 2026-10-08 | a live, locked agent (tip is a PR merge on main) |

### Owner decision (8)
| Branch | Tip | Date | Merged | Why it needs a decision |
|---|---|---|---|---|
| `parked/webrtc-live-view` | `1890901` | 2026-10-04 | **no** (local + `origin`) | parked work: "WebRTC live view (plus the in-progress work it builds on)". Keep parked or archive it as `archive/webrtc-live-view` |
| `perf/settle-trace` | `744d0c9` | 2026-10-08 | **no**, not pushed | PR #10's title says the opt-in settle trace shipped, but `OPENMOB_SETTLE_TRACE` is not on `main` and this commit is on no remote branch. Either the trace landed under another name, or this branch holds work that didn't ship. Check before deleting |
| `cleanup/remove-unused-memory` | `d053ef6` | 2026-10-06 | empty (points at a main commit) | no commits of its own; safe, but the name suggests an intended effort that became PR #2. Delete unless someone planned to reuse it |
| `feature/gpt-luna` | `d053ef6` | 2026-10-06 | empty | same: an unstarted branch (GPT provider work shipped in PR #3) |
| `feature/multi-provider-model-picker` | `d053ef6` | 2026-10-06 | empty | same (shipped in PR #3) |
| `feature/qwen-grok` | `d053ef6` | 2026-10-06 | empty | same (shipped in PR #3) |
| `worktree-agent-a6943e7892f901dfc` / `worktree-agent-a7f9e89aeef89be15` | `3ccca0c` / `da55029` | 2026-10-08 | main | the two unlocked worktrees modified today (see above); their branches are empty PR-merge pointers and go with the worktrees |

(The four empty branches above are in practice safe to delete; they are listed as a decision only because they
were created by hand with intent, not by the agent harness.)

### Safe to delete (59)
All merged into `origin/main` or `origin/gatekeeper-p1`; none has an open PR.

PR heads, merged 2026-10-08 (21, all with a matching `origin/` branch): `ui/review-fixes` (#1),
`cleanup/remove-voice-memory` (#2), `feature/model-providers` (#3), `chore/om-launcher` (#4),
`feature/signup-invites` (#5), `tune/haiku-high-effort` (#6), `fix/claude-fallback-warning` (#7),
`perf/multi-action` (#8), `perf/parallel-observe` (#9), `perf/settle-fixes` (#10), `fix/stale-current-app` (#11),
`docs/latency-targets` (#12), `perf/fewer-model-calls` (#13), `feature/deep-links` (#14), `perf/effort-by-screen`
(#15), `feature/app-links` (#16), `fix/loading-percent-link-settle` (#17), `fix/type-text-replace` (#18),
`fix/google-link-browser` (#19), `fix/play-page-by-name` (#20), `fix/app-name-acronym` (#21).

Gatekeeper-side twins, merged into `gatekeeper-p1` (5): `feature/deep-links-gk`, `dialog-back-gk`,
`no-install-ask-gk`, `tight-loop-gk`, `wait-until-gk`.

Pre-PR work merged by hand, 2026-10-04 to 2026-10-07 (14): `dialog-back`, `no-install-ask`, `tight-loop`,
`wait-until`, `gatekeeper-b7`, `gaps-fix`, `work` (= `feature/easy-connect`), `feature/easy-connect`,
`feature/agent-platform`, `feature/permission-modes`, `feature/vision-speed`, `release/mvp-1`, `ui/landing-auth`,
`ui/phone-live-view`, `ui/recent-fixes`.

Agent worktree branches, all merged (18): `worktree-agent-a090160410fc3e7df`, `-a144ad0044823fb32`,
`-a181deb4f5b5930b4`, `-a1a5318d7c22f1577`, `-a21eb11d13741444e`, `-a27f52f1492fa5ff8`, `-a33830f4ee1ec1c86`,
`-a48b8d780fbd1e418`, `-a4ad909dd39ee1a85`, `-a557b325c7608f076`, `-a8915fa3cbe145a63`, `-a94442e39f4f9e8a1`,
`-aa50554eed72fe025`, `-aafda1b7f5ea89a28`, `-ab4c5049c2618b04b`, `-abd823bf6d198a12f`, `-ae04c587c30db8118`,
`-af42369fe7739525c`.

(21 + 5 + 15 + 18 = 59. `work` and `feature/easy-connect` are two branches on the same commit; both go.)

`git branch -d` refuses a branch it considers unmerged against the *current* branch; use `git branch -d` from
`main` for the main-merged ones and `-d` from a `gatekeeper-p1` checkout for the `-gk` ones, and never `-D` without
looking at what it complains about.

## Remote branches (42, plus `origin/HEAD`)

### Keep (4)
`origin/main`, `origin/gatekeeper-p1`, `origin/archive/clef-replacement` (`b49c733`, 2026-10-07: the Clef
comparison build, the owner's archive of the dropped classifier), `origin/archive/mirror-draft-2026-10-04`.

### Owner decision (1)
| Branch | Tip | Date | Why |
|---|---|---|---|
| `origin/parked/webrtc-live-view` | `1890901` | 2026-10-04 | not merged; the decision is whether "parked" stays, becomes `archive/webrtc-live-view`, or goes |

### Safe to delete (37)
All merged into `origin/main` or `origin/gatekeeper-p1`, none with an open PR:

the 21 PR heads listed above (`origin/ui/review-fixes` … `origin/fix/app-name-acronym`); the gatekeeper twins
`origin/feature/deep-links-gk`, `origin/dialog-back-gk`, `origin/no-install-ask-gk`, `origin/tight-loop-gk`,
`origin/wait-until-gk`; the pre-PR work `origin/dialog-back`, `origin/no-install-ask`, `origin/tight-loop`,
`origin/wait-until`, `origin/gatekeeper-b7`, `origin/feature/easy-connect`, `origin/feature/agent-platform`,
`origin/feature/permission-modes`, `origin/feature/vision-speed`, `origin/release/mvp-1`; and
`origin/gatekeeper-wait` (`fd10b7a`, 2026-10-06, merged into `gatekeeper-p1`, no local branch).

That is 21 + 5 + 10 + 1 = 37.

## Stashes (2; none dropped)

| Entry | SHA | Date | Message | Size | Recommendation |
|---|---|---|---|---|---|
| `stash@{0}` | `7e9710e` | 2026-10-08 | "On ui/phone-live-view: ui/phone-live-view uncommitted work (Codex), saved 2026-10-08; now merged to main via PRs #1-#4" | 58 files, +1119/−1816 | **owner decision.** The message says its content reached `main` through PRs #1–#4, which is plausible (those PRs carry the UI review fixes, memory removal, providers and the launcher). Nobody has diffed the stash against `main` to prove it; do that (`git stash show -p stash@{0}` against `main`) before dropping |
| `stash@{1}` | `b1c998d` | 2026-10-08 | "On (no branch): Codex uncommitted gatekeeper changes, saved 2026-10-08 before moving to b51be31" | 30 files, +803/−482 | **owner decision.** Gatekeeper-side changes of unknown status; nothing says they were merged. Diff against `gatekeeper-p1` before deciding |

Both touch `tests/test_models.py` and `tests/test_runner_server.py`. The stash stack is shared by every worktree
and every agent session; `git stash pop` from any of them would apply someone else's work, so apply by SHA
(`git stash apply 7e9710e`) on a scratch branch if the owner wants to inspect them.

## Suggested order, once the owner says yes

1. Run the dirty check at the top; stop on any worktree that reports changes.
2. Remove the 20 worktrees marked safe (`git worktree remove <path>`), then `git worktree prune`.
3. From `main`: `git branch -d` the main-merged local branches; from `~/Desktop/openmob-gatekeeper` (detached, so
   `git branch -d` works against `HEAD` = `gatekeeper-p1`): the `-gk` ones.
4. `git push origin --delete` the 37 remote branches, in one command.
5. Leave the stashes until the two diffs have been read.
6. Turn on "Automatically delete head branches" in the GitHub repository settings so PR heads stop piling up.
