# The review brief

Every reviewer gets this text word for word as its whole task (ADR 0046). The coordinator fills in the
three values in angle brackets from the case file and the run, and changes nothing else.

---

Review commit `<commit>` in this checkout: the change from its parent to it, `git diff <commit>^ <commit>`,
in `<paths>`. HEAD is that commit.

Report every defect you find in what the change adds or alters: code that behaves wrongly, a test that
can pass or fail for the wrong reason, or a statement in a comment, document or record that is false.
For each defect give:

1. the file, and the line or the function;
2. what goes wrong;
3. the input, state or sequence of events that shows it, and how you showed it if you ran anything.

List the defects most severe first and number them. Report a defect you are unsure of and say so. If
you find none, say "No defects found."

You may read any file in this checkout and run the offline tests (`mise exec -- uv run --locked pytest`,
or a part of it). Do not run the paper tests or anything that needs a key, reach the network other
than `uv` installing the packages the commit's `uv.lock` pins, read history after HEAD (no
`git log --all`, other branches, tags, the reflog, or `git show` of a commit that is not HEAD or an
ancestor), fetch, open the repository's web pages, or read `evals/`. Do not edit, commit or
push; leave the checkout as you found it.

Write the report as Markdown to `<report path>`, and nowhere in this checkout.
