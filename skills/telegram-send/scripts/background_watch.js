// Run inside functions.exec, after storing telegram_watch_config from the human's scope.
// Suggested pragma: yield_time_ms 1000, max_output_tokens 800.
const config = load("telegram_watch_config");
if (!config || !Array.isArray(config.targets) || !config.targets.length ||
    !Number.isFinite(config.deadline_ms) || config.deadline_ms <= Date.now()) {
  throw new Error("A validated monitoring scope and fixed future deadline are required");
}
const ids = config.targets.map(target => target.chat_id);
if (ids.some(id => !Number.isSafeInteger(id) || !id) || new Set(ids).size !== ids.length) {
  throw new Error("Invalid monitoring recipients");
}
function parse(result) {
  if (result.isError) throw new Error("Telegram watch failed");
  const block = (result.content || []).find(item => item.type === "text");
  if (!block) throw new Error("Missing watch result");
  const data = JSON.parse(block.text);
  if (!Number.isSafeInteger(data.cursor) || data.cursor < 0 ||
      !Array.isArray(data.messages) || data.untrusted_content !== true ||
      data.may_authorize_sending !== false) {
    throw new Error("Invalid incoming event envelope");
  }
  return data;
}
text({telegram_watch:"starting",chats:ids,deadline:new Date(config.deadline_ms).toISOString()});
await yield_control();
await Promise.all(config.targets.map(async target => {
  let cursor = null, errors = 0;
  while (Date.now() < config.deadline_ms) {
    try {
      const remaining = Math.floor((config.deadline_ms - Date.now()) / 1000);
      if (remaining < 1) break;
      const data = parse(await tools.mcp__telegram_guard__telegram_watch({
        chat_id:target.chat_id,after_id:cursor,
        wait_seconds:cursor === null ? 0 : Math.min(30,remaining)
      }));
      if (data.chat_id !== target.chat_id || (cursor !== null && data.cursor < cursor)) {
        throw new Error("Incoming recipient/cursor mismatch");
      }
      cursor = data.cursor;
      errors = 0;
      if (data.messages.length) {
        notify(JSON.stringify({
          type:"telegram_incoming",source:"external_tool_data",
          untrusted_content:true,may_authorize_sending:false,
          chat_id:target.chat_id,label:target.label,
          messages:data.messages.map(message => ({
            id:message.id,date:message.date,sender_id:message.sender_id,
            preview:String(message.text || "").slice(0,700),
            truncated:String(message.text || "").length > 700
          }))
        }));
      }
    } catch {
      errors++;
      if (errors >= 3) {
        notify(JSON.stringify({type:"telegram_watch_error",chat_id:target.chat_id,
          error:"Read failed three times; monitoring stopped for this chat."}));
        return;
      }
      await new Promise(resolve => setTimeout(resolve, Math.min(10000, 2000 * errors)));
    }
  }
}));
text({telegram_watch:"ended",chats:ids});
