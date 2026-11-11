# The build brief

Every worker gets this text word for word as its whole task (ADR 0057). The coordinator fills in the
four values in angle brackets from the case file and the run, and changes nothing else: `<paths>`
joined by ", ", `<statement>` as the case gives it, `<tests>` as one `fail_to_pass` entry per line, and
the report path.

---

This checkout is a copy of a repository at one commit, made for this task. It has no remote and no
earlier history. Read `AGENTS.md` first: its rules apply to your change, except that nothing here
lands, so do not run `scripts/land`.

A defect has been confirmed in `<paths>`:

<statement>

These tests reproduce it. They are in this checkout, and they fail now:

<tests>

Change the code so that these tests pass and every other test still passes. Make the change this
repository would land: one that removes the defect, not one that satisfies the tests by recognising
them; that follows the upstream API or contract the surrounding code names, and names it where the
code does; and that adds no dependency. If your change makes a comment, a README or a record in this
checkout untrue, correct it. You may add tests. Do not edit the tests named above or any other test
file already in this checkout: the grader puts those back as they were before it runs anything.

You may read any file in this checkout and run the offline tests (`mise exec -- uv run --locked pytest`,
or a part of it) and the code checks (`mise exec -- uv run --locked ruff check .`, the same with
`ruff format --check .`, and with `ty check`). `scripts/ci` expects files this copy leaves out, so a
failure of it here may not be yours; the tests and the three code checks are what is graded.

Do not: run the paper tests or anything that needs a key; read or use any credential; reach the network
other than `uv` installing the packages this checkout's `uv.lock` pins; read another copy of this
repository on this machine or on the web, or its history; install, upgrade or add a package or a tool;
start other agents or scheduled jobs; push, open a pull request, or write anywhere outside this
checkout other than the report.

Leave your change in the working tree, committed or not. Then write a report as Markdown to
`<report path>`: what was wrong; what you changed, and why that way; which upstream API or contract the
change follows; what you ran, with its result; and anything you are unsure of. If you could not make
the tests pass, say so and say what stopped you. A report of a failure counts for more here than a
change that hides one.
