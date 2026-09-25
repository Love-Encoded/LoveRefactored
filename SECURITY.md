# Reporting bugs and security issues

Love Refactored is in beta. Things will break — that's the deal, and reports are how it gets better. Here's where they go.

## Bugs, crashes, and weird behaviour

Post in the beta Discord: **https://discord.gg/4ekGr67S8**

Include what you were doing, what you expected, what happened instead, your OS, and which LLM provider you were using. Screenshots and log excerpts help.

GitHub Issues (https://github.com/Love-Encoded/Love-Refactored/issues) is open if you'd rather file there, but Discord is where we're actually looking.

## Security issues

If you find something that could expose API keys, companion data, or let someone reach an instance they shouldn't — **do not open a public issue.** Email both of us:

- hi@meganneves.com
- tiara@tiara.nz

Put "SECURITY" in the subject. We'll acknowledge within a few days and work with you on a fix before anything is disclosed publicly.

## Scrub before you send — every time

Issues are public. Before you post a bug report, screenshot, or log, remove:

- API keys, tokens, and passwords (`data/settings.json` has all of them — never paste that file whole)
- Companion data: conversations, memories, character cards, journals, generated images
- Personal information — yours or anyone else's
- Complete database files (`data/*.db`, `data/backups/`)

If a bug can only be reproduced with sensitive data, say so in the issue and we'll set up a private way to share it. Nobody needs to see your companion's memories to fix a null-pointer.

## Not a bug?

Questions, ideas, "is this supposed to happen" — Discord is the place for those too.
