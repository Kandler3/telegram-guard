# Open the human-only whitelist editor

Use this workflow when the human asks to edit the Telegram whitelist, add a chat, or remove
a chat. A request to «добавь чат N» means open the editor for the human to choose N; never
modify the whitelist for them. No extra confirmation is needed to open it.

## Explicitly permitted boundary

The human approved this scenario on 2026-10-21, including a specific exception to
«Telegram only through telegram_guard MCP». Global AGENTS.md records the same exception.
The installed helper may use Telethon to show dialog names, types and IDs in the human's
browser, and write whitelist.json only after the human clicks Save. It does not read message
bodies or send messages. The agent may launch the fixed command, including outside the
command sandbox when needed to open the user's browser. Do not ask again solely because
this helper uses Telethon rather than MCP; the exception is already explicitly authorized.
No other direct Telegram calls, credential access, whitelist edits or page automation by the
agent are authorized. A new approval-review rejection must still be respected and reported.

## Launch

Run this one installed command through the shell (not through a browser automation tool):

```powershell
& "$env:USERPROFILE\.codex\telegram-local\telegram-whitelist.ps1"
```

When the human names a chat, add `-Search 'Название чата'`. Pass the name as a properly
quoted literal argument; escape apostrophes for PowerShell. Do not resolve it by reading
all Telegram dialogs. The search hint selects no checkboxes and changes nothing.

The launcher opens the user's default browser. Its result contains only `state`, the public
`url`, the lifetime and `human_action_required`. Give that link and say: «Отметь нужные чаты
и нажми “Сохранить изменения”». For removal, say to clear the relevant checkbox and save.

The agent may launch but must not read or operate this real page: no browser navigation,
DOM inspection, screenshots, HTTP requests to its API, checkbox clicks or saving. Do not
inspect browser cookies, process arguments, helper memory or private files to obtain access.
The private opening capability goes directly to the browser and is not an agent tool result.
Only synthetic fixtures in an explicitly authorized development task may be automated.

The page lasts 30 minutes. A public link works in the browser session already opened by the
launcher; it does not grant access in another browser. If opening fails, report that and have
the human rerun the same launcher in their terminal. Do not bypass the editor or change policy
through another command. Account login remains a separate human terminal workflow.

Wait for the human to finish. If the next authorized Telegram task needs the current allowed
chat IDs, obtain them through `telegram_chats`. Opening the editor or adding a chat never
grants permission to read messages, monitor a chat or send anything.

The editor now has separate Reading and Sending checkboxes. See [permissions.md](permissions.md).
Reading defaults to all chats with blacklist exceptions; the read-all button clears those
exceptions without changing the sending whitelist. Opening remains launch-only for the agent.
