"""Typed media -> Telegram requests. All uploads/sends share the broker guard."""
import asyncio
import mimetypes
import secrets
from telethon import functions, types, utils
from .model import GuardError
from .transport import input_peer


def entities(values):
    simple = {"bold": types.MessageEntityBold, "italic": types.MessageEntityItalic,
              "underline": types.MessageEntityUnderline, "strike": types.MessageEntityStrike,
              "spoiler": types.MessageEntitySpoiler, "code": types.MessageEntityCode,
              "blockquote": types.MessageEntityBlockquote}
    result = []
    for value in values:
        args = {"offset": value["offset"], "length": value["length"]}
        kind = value["kind"]
        if kind in simple:
            entity = simple[kind](**args)
        elif kind == "text_url":
            entity = types.MessageEntityTextUrl(**args, url=value["url"])
        elif kind == "pre":
            entity = types.MessageEntityPre(**args, language=value.get("language") or "")
        elif kind == "custom_emoji":
            entity = types.MessageEntityCustomEmoji(**args, document_id=value["document_id"])
        result.append(entity)
    return result


def receipt(result, random_ids):
    if len(random_ids) == 1 and isinstance(result, types.UpdateShortSentMessage):
        return [result.id]
    found = {u.random_id: u.id for u in getattr(result, "updates", []) if isinstance(u, types.UpdateMessageID)}
    if not all(rid in found for rid in random_ids):
        raise RuntimeError("No complete delivery receipt")
    return [found[rid] for rid in random_ids]


async def file_media(client, item, files, guard):
    async def upload(path, filename=None):
        guard()
        record = files[path]
        record.stream.seek(0)
        async def progress(sent, total):
            guard()
        return await asyncio.wait_for(client.upload_file(
            record.stream, file_name=filename or record.filename, file_size=record.size,
            progress_callback=progress), timeout=1800)

    name = item["filename"] or files[item["path"]].filename
    kind = item["kind"]
    mime = item["mime_type"] or mimetypes.guess_type(name)[0] or "application/octet-stream"
    if name.lower().endswith(".tgs") and item["mime_type"] is None:
        mime = "application/x-tgsticker"
    uploaded = await upload(item["path"], name)
    if kind == "photo":
        return types.InputMediaUploadedPhoto(uploaded, spoiler=item["spoiler"], ttl_seconds=item["ttl_seconds"])
    attrs = [types.DocumentAttributeFilename(name)]
    if kind in ("audio", "voice"):
        attrs.append(types.DocumentAttributeAudio(int(item["duration"]), voice=kind == "voice",
                                                 title=item["title"], performer=item["performer"]))
    if kind in ("video", "video_note") or (kind == "animation" and mime.startswith("video/")):
        attrs.append(types.DocumentAttributeVideo(item["duration"], item["width"], item["height"],
                     round_message=kind == "video_note", supports_streaming=kind != "video_note"))
    if kind == "animation":
        attrs.append(types.DocumentAttributeAnimated())
    if kind == "sticker":
        attrs.append(types.DocumentAttributeSticker(item["emoji"], types.InputStickerSetEmpty()))
        if item["width"] and item["height"]:
            attrs.append(types.DocumentAttributeImageSize(item["width"], item["height"]))
    thumb = await upload(item["thumbnail"]) if item["thumbnail"] else None
    return types.InputMediaUploadedDocument(uploaded, mime, attrs, thumb=thumb,
                force_file=kind == "document", spoiler=item["spoiler"], ttl_seconds=item["ttl_seconds"])


def structured_media(content, source):
    kind = content["type"]
    if kind == "contact":
        return types.InputMediaContact(content["phone_number"], content["first_name"], content["last_name"], content["vcard"])
    if kind in ("location", "venue"):
        point = types.InputGeoPoint(content["latitude"], content["longitude"], accuracy_radius=content.get("accuracy_radius"))
        if kind == "venue":
            return types.InputMediaVenue(point, content["title"], content["address"], content["provider"],
                                         content["venue_id"], content["venue_type"])
        if content["live_period"]:
            return types.InputMediaGeoLive(point, period=content["live_period"], heading=content["heading"])
        return types.InputMediaGeoPoint(point)
    if kind == "dice":
        return types.InputMediaDice(content["emoticon"])
    if kind == "poll":
        poll = types.Poll(id=0, hash=0, question=types.TextWithEntities(content["question"], []),
              answers=[types.PollAnswer(types.TextWithEntities(answer, []), bytes([i]))
                       for i, answer in enumerate(content["answers"])],
              public_voters=not content["anonymous"], multiple_choice=content["multiple_choice"],
              quiz=content["quiz"], close_period=content["close_period"])
        return types.InputMediaPoll(poll, correct_answers=content["correct_answers"] or None,
               solution=content["solution"] or None, solution_entities=[] if content["solution"] else None)
    if kind == "checklist":
        return types.InputMediaTodo(types.TodoList(title=types.TextWithEntities(content["title"], []),
               list=[types.TodoItem(i + 1, types.TextWithEntities(title, [])) for i, title in enumerate(content["items"])],
               others_can_append=content["others_can_append"], others_can_complete=content["others_can_complete"]))
    if kind == "story":
        return types.InputMediaStory(input_peer(source), content["story_id"])
    raise GuardError("unsupported_content")


async def send_content(client, intent, files, guard):
    peer, source = guard()
    options = intent.options or {}
    content = intent.content
    kind = content["type"] if content else "text"
    media = None
    prepared_album = []
    if kind == "file":
        media = await file_media(client, content, files, guard)
    elif kind == "album":
        # Uploaded media must be converted into reusable media before sendMultiMedia.
        for item in content["items"]:
            uploaded = await file_media(client, item, files, guard)
            peer, _ = guard()
            saved = await asyncio.wait_for(client(functions.messages.UploadMediaRequest(
                peer=input_peer(peer), media=uploaded)), timeout=60)
            converted = utils.get_input_media(saved)
            if hasattr(converted, "spoiler"):
                converted.spoiler = item["spoiler"]
            prepared_album.append(converted)
    elif kind == "existing_media":
        _, source = guard()
        message = await asyncio.wait_for(client.get_messages(input_peer(source), ids=content["message_id"]), timeout=20)
        guard()
        if message is None or not getattr(message, "media", None):
            raise GuardError("source_media_not_found")
        if getattr(message, "noforwards", False):
            raise GuardError("source_content_protected")
        media = utils.get_input_media(message.media)
    elif kind not in ("text", "forward"):
        media = structured_media(content, source)

    sender = None
    peer, source = guard()
    if peer.kind == "channel":
        sender = await asyncio.wait_for(client.get_me(input_peer=True), timeout=15)
        if not isinstance(sender, types.InputPeerUser):
            raise RuntimeError("Cannot pin personal sender")
    peer, source = guard()  # Expiry and whitelist checked after uploads and sender lookup.
    reply = None
    if options.get("reply_to") or options.get("top_msg_id"):
        reply = types.InputReplyToMessage(options.get("reply_to") or options["top_msg_id"],
                                          top_msg_id=options.get("top_msg_id"))
    random_ids = []
    while len(random_ids) < intent.message_count:
        value = secrets.randbits(63) or 1
        if value not in random_ids:
            random_ids.append(value)
    common = dict(peer=input_peer(peer), silent=options.get("silent", False), send_as=sender, reply_to=reply)
    if kind == "text":
        request = functions.messages.SendMessageRequest(**common, message=intent.text, random_id=random_ids[0],
                  no_webpage=not options.get("link_preview", False), entities=entities(options.get("entities", [])))
    elif kind == "album":
        request = functions.messages.SendMultiMediaRequest(**common, multi_media=[
            types.InputSingleMedia(media=m, random_id=rid, message=item["caption"], entities=entities(item["entities"]))
            for m, rid, item in zip(prepared_album, random_ids, content["items"])])
    elif kind == "forward":
        common.pop("peer")
        request = functions.messages.ForwardMessagesRequest(**common, from_peer=input_peer(source), to_peer=input_peer(peer),
                  id=content["message_ids"], random_id=random_ids, drop_author=content["drop_author"],
                  drop_media_captions=content["drop_media_captions"], top_msg_id=options.get("top_msg_id"))
    else:
        caption = content["caption"] if kind == "file" else intent.text
        formatting = content["entities"] if kind == "file" else options.get("entities", [])
        request = functions.messages.SendMediaRequest(**common, media=media, message=caption,
                  random_id=random_ids[0], entities=entities(formatting))
    # Catch local serialization errors before invoking the network request.
    bytes(request)
    guard()
    return receipt(await asyncio.wait_for(client(request), timeout=30), random_ids)
