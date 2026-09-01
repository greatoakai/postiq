# Open issues

Snapshot taken 2026-09-01, at the close of the session that fixed TA account
matching and added the open-balance posting path. Everything here is unresolved.
Ordered by what it costs if ignored.

---

## 1. A shared surname suppresses the "check both" caution — `scripts/reconcile.py:171`

```python
def _shares_name_token(a, b):
    return bool(toks(a) & toks(b)) or _shares_surname(a, b)
```

`_shares_name_token` answers *"are these two spellings the same person?"* — it is
the test that decides whether a CSV row and an unmatched poller failure get paired
as one payment. `_shares_surname` answers a different question: *"are these two
people family?"* Since the two-letter floor landed, folding the family test into
the identity test means **a shared surname alone is enough to call two people the
same person.**

It has exactly one caller, `reconcile.py:491`, and its only effect is the
`uncertain` flag. The pairing happens either way; `uncertain` decides whether the
morning report prints the *"look at both and post it once"* caution (`:896`).

So the failure is quiet: when the pairing is wrong — two siblings, one payment
each, same amount, same day — the two are merged into a single line item and the
caution that would have caught it is suppressed. A person sees one payment where
there were two, with nothing telling them to look. The file documents this exact
scenario in its own comments ("Zachary Hahs" / "Iliana Hahs").

Surfaced by a high-effort review of that commit. Not yet fixed.

## 2. `_NAME_NOISE` is defined twice — `scripts/reconcile.py:93` and `:118`

Identical bodies. The first binding is dead; the second silently wins. Harmless
today, and a trap the first time someone edits the wrong one and can't see why
nothing changed. Delete `:93`.

## 3. Commit `abcb1ad`'s message doesn't describe its contents

Message: *"reconcile: fold the stroke letters after the mojibake repair, not
before."* It was a `git commit -a` made while a second session had work in
progress in the same worktree, so it also carries `scripts/bot_v2.py` (+405) and
`scripts/poll_square.py` (+17) — the account-matching fix and the open-balance
posting path, neither of which the message mentions.

Nothing is wrong with the code. `git log` just doesn't lead anyone to it. Fixable
by rewrite while the branch history is still local to the team; leave it if that
isn't worth the churn.

Root cause is recorded so it doesn't recur: **only one session should edit this
repo at a time.** Two worktrees share one `.git`, and `git commit -a` sweeps up
whatever the other session has loose in the tree.

## 4. AWS teardown is half-done

`125eb74` removed the S3 round-trip from `run_daily.sh`; the Square Daily Report
bot now writes straight into `drive-inbox/`. Verified across 08/27–08/30. What is
still outstanding:

- `scripts/sync_inbox.py` is unreferenced dead code. It was the only file in this
  repo that touched AWS.
- The bucket `s3://greatoak-square-reports`, its IAM user, and the long-lived
  access key can be deleted.
- The `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_BUCKET`, `S3_REGION` and
  `S3_PREFIX` lines can come out of `.env`.

Note for anyone migrating AWS profiles: this repo **never used a named profile.**
`sync_inbox.py` read the two keys out of `.env` and passed them straight to
`boto3.client()`, so `AWS_PROFILE` had no effect here.

## 5. The installed reconcile plist header is stale

`~/Library/LaunchAgents/com.greatoak.postiq-reconcile.plist` still describes the
2026-06 shadow proving window and tells the reader to remove the job after
cutover. The schedule and command are **identical** to `launchd/` — the drift is
comments only — but it is the copy someone will read at 3am.

Fix: `cp launchd/com.greatoak.postiq-reconcile.plist ~/Library/LaunchAgents/`
(no reload needed for a comment change).

## 6. Live code runs from a feature branch in a second worktree

`com.greatoak.postiq-poll-live`, `-backfill` and `-reconcile` all execute out of
`/Users/travmegsam/Developer/postiq-dev`, which sits on `feat/refid-account-match`
— not `main`. There is no release step: **a `git checkout` in that directory
changes what posts client payments,** immediately, on the next half-hour fire.

`main` is kept consolidated (`main..feat` is empty), so the branches agree today.
Worth an actual decision: either point the jobs at `main`, or write down that the
feature branch is production.

---

## Parked deliberately — not defects

- **V1 posted-date capture.** V1 fallback postings report `"Posted ✓"` rather than
  the date TA allocated them to; after a V1 save TA returns to the Billing
  dashboard with no confirmation page to scrape, and the pre-save scrape was
  unreliable (see `cb13f95`). Now largely moot: the open-balance path added in
  `62132b1` handles most of what used to fall through to V1.
- **Multi-child payers.** One parent's Square account pays for several TA clients;
  `reference_id` is a single field, so it cannot disambiguate them. Those payments
  are posted by hand on purpose. DOB-based routing is designed but unbuilt.
- **`CHARGES_MODAL_CHOICE=all_open_charges`.** Introduced as a test in June 2026
  and running ever since — payments distribute across open charges oldest-first
  rather than pinning to one appointment. Treat as settled; revert by setting
  `this_appointment` in `.env`.
