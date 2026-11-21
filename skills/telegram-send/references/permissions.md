# Separate read and write access (policy v2)

Reading defaults to allowed for all accessible Telegram chats, including future chats,
except IDs in the read blacklist. History, search, folders, chat/member/topic information,
exact messages, attachment downloads, watch and subscribe use read permission only.
Writes default to denied and need a separate chat whitelist plus the existing explicit
human authorization for each outgoing action. Write also covers message edits/deletions,
reactions, pin/unpin, marking history read and voting. Adding write permission never grants an
instruction to send. A read-only subscription never authorizes replies.

Use telegram_chats(search="name or @username or numeric ID", limit=100, offset=0) to find
chats relevant to the human's task. Results include can_read, can_write and next_offset.
Follow next_offset when needed. Discovery returns no access hashes or credentials.
Do not infer write permission from read permission or from a chat appearing in results.

Forwarding, existing media and shared stories need read access in the source chat and
write access in the destination. A blocked source cannot be read via forwarding.
User authorization must still cover the content, recipient and purpose.

The human editor has separate Reading and Sending columns. With the default-reading
switch enabled, unchecking Reading adds a blacklist exception; enabling Sending adds
only that chat to the write whitelist. “Разрешить чтение всех” enables default reading
and clears read exceptions globally, including for future chats; it never changes writes.
The agent may open the editor but must not operate the real page or change selections.

Migration v1 -> v2 preserves every existing chat record and both send-rate limits. Existing
chats keep both permissions; every other chat becomes read-only by default. The explicit
migration saves the original policy only inside the protected Guard directory. Legacy v1
files are interpreted restrictively until migration; malformed policy fails closed.
