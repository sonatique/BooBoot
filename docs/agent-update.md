# Update runbook for agents

This file is written for an agent that runs shell commands on the user's
computer. The user points it here to update a BooBoot unit that they look
after, and the BooBoot tools on their computer, to the latest release. For
example:

```text
Update BooBoot: https://raw.githubusercontent.com/sonatique/BooBoot/main/docs/agent-update.md
The unit is at USER@booboot.local.
```

For the agent:

1. **What the user asks.** Giving you this file means that the user looks
   after the unit themselves, and wants it updated: rule 5 of the usage
   runbook allows you to change it. You still ask before each step that
   changes something, as the steps say.
2. **The unit.** Its address comes from the user's message, with the SSH
   user (`USER@HOST`). Without an SSH user, ask for it: the update needs
   SSH. Without an address, use `booboot.local`.
3. **Read** the usage runbook completely:
   `https://raw.githubusercontent.com/sonatique/BooBoot/main/docs/agent-use.md`.
   Its values (`HOST`, `USER`, `TOOLS`, `pi`, `bb`), rules and steps apply
   here.
4. **Do** its steps U1 and U2, then its section 6, "Update".
5. **Then** go on with the task the user gave with this file, if any.
