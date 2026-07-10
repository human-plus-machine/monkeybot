# User stories: prettier CLI demo

## Presenter-ready readiness

As a presenter, when I run `validate` and `doctor` on a healthy fake scaffold,
I see a short, color-coded success report that reads as “ready” at a glance —
not a wall of identical `!` marks or `: pass` placeholders.

## Product-feeling chat

As a presenter, when I open `monkeybot chat`, I immediately see which
provider/model/gateway I am talking to, then the familiar spinner → tool
activity → 🐵 streamed reply loop, with lightly styled markdown on a TTY so
headings and code do not look like raw markup.

## Automation unchanged

As a script or CI job, `--json` and non-TTY pipes stay plain, stable, and
parseable; visual polish never becomes a required dependency for machines.
