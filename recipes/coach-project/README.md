# Recipe: coach Project on claude.ai

A Claude Project whose instructions make Claude a training coach that uses this server's tools.
It works on claude.ai in a browser and in the Claude apps.

1. On claude.ai in a browser, create a Project from the chat sidebar (**Projects → New
   project**), not one under claude.ai/code: those projects' main conversation has no connectors.
2. **Set project instructions:** paste [instructions.md](instructions.md), fill in the "My
   details" lines, then **Save instructions**. Keep your filled-in copy as
   `instructions.local.md` in this folder: git ignores `*.local.md`, so your profile stays off
   GitHub.
3. In each new chat in the project, check Garmin is on under **+ → Connectors**. The project
   works on your phone too.

In `key` mode some tools named in the instructions are not on the server
([Tools](../../docs/tools.md)); Claude then says so instead of guessing.
