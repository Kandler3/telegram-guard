"""Named chat operations. No caller-selected RPCs or raw TL objects."""
import asyncio
import hashlib
import json
import os
import secrets
from dataclasses import replace
from pathlib import Path
from .attachments import MAX_FILE_BYTES
from .files import checked
from .model import GuardError, Send, chat_id, integer, nonempty
from .transport import input_peer


def message_row(m):
    import base64
    reply = getattr(m, "reply_to", None)
    file = getattr(m, "file", None)
    poll_media=getattr(m,"poll",None)
    poll=poll_media.poll if poll_media is not None else None
    return {"id": m.id, "chat_id": m.chat_id, "sender_id": m.sender_id,
            "outgoing": bool(m.out), "date": m.date.isoformat() if m.date else None,
            "text": m.message or "", "reply_to_message_id": getattr(reply, "reply_to_msg_id", None),
            "topic_id": getattr(reply, "reply_to_top_id", None),
            "media_type": type(m.media).__name__ if m.media else None,
            "filename": getattr(file, "name", None), "size": getattr(file, "size", None),
            "mime_type": getattr(file, "mime_type", None), "grouped_id": getattr(m, "grouped_id", None),
            "poll": {"question":getattr(poll.question,"text",str(poll.question)),
                     "answers":[{"text":getattr(a.text,"text",str(a.text)),"option":base64.b64encode(a.option).decode('ascii')} for a in poll.answers]} if poll else None}


def peer_ids(peers, policy):
    from telethon import utils
    return [cid for p in peers if policy.can_read(cid := utils.get_peer_id(p))]


def folder_row(f, policy):
    return {"id": getattr(f,"id",0), "title": getattr(getattr(f, "title", None), "text", None) or "Все чаты",
            "emoticon": getattr(f, "emoticon", None),
            "include_chat_ids": peer_ids(getattr(f, "include_peers", []), policy),
            "exclude_chat_ids": peer_ids(getattr(f, "exclude_peers", []), policy),
            "pinned_chat_ids": peer_ids(getattr(f, "pinned_peers", []), policy),
            "rules": {k: bool(getattr(f, k, False)) for k in
                      ("contacts", "non_contacts", "groups", "broadcasts", "bots", "exclude_muted", "exclude_read", "exclude_archived")}}


def in_folder(dialog,folder):
    from telethon import types,utils
    from datetime import datetime,timezone
    if isinstance(folder,types.DialogFilterDefault):return True
    cid=dialog.id
    if cid in [utils.get_peer_id(p) for p in getattr(folder,"exclude_peers",[])]:return False
    if cid in [utils.get_peer_id(p) for p in [*getattr(folder,"include_peers",[]),*getattr(folder,"pinned_peers",[])]]:return True
    entity=dialog.entity
    if isinstance(entity,types.User):
        kind='bots' if entity.bot else ('contacts' if entity.contact else 'non_contacts')
    elif isinstance(entity,types.Chat) or getattr(entity,'megagroup',False):kind='groups'
    else:kind='broadcasts'
    if not getattr(folder,kind,False):return False
    if getattr(folder,'exclude_archived',False) and getattr(dialog,'archived',False):return False
    d=getattr(dialog,'dialog',None)
    if getattr(folder,'exclude_read',False) and not (getattr(dialog,'unread_count',0) or getattr(d,'unread_mark',False)):return False
    mute=getattr(getattr(d,'notify_settings',None),'mute_until',None)
    if getattr(folder,'exclude_muted',False) and mute:
        until=mute.timestamp() if isinstance(mute,datetime) else mute
        if until>datetime.now(timezone.utc).timestamp():return False
    return True


async def read_operation(runtime, operation, target=None, **args):
    from telethon import functions, types, utils
    if target is not None:
        target = chat_id(target)
        runtime.policy().require_read(target)
    async with runtime.mutex:
        async with runtime.connection() as broker:
            client = broker.transport.client
            peer = input_peer(await broker.read_peer(target)) if target is not None else None
            if operation == "folders":
                result = await client(functions.messages.GetDialogFiltersRequest())
                membership={getattr(f,"id",0):[] for f in result.filters}
                count=0
                async for dialog in client.iter_dialogs():
                    count+=1
                    if count>10000:raise GuardError("too_many_chats",422)
                    if runtime.policy().can_read(dialog.id):
                        for f in result.filters:
                            if in_folder(dialog,f):membership[getattr(f,"id",0)].append(dialog.id)
                policy=runtime.policy()
                return {"folders": [dict(folder_row(f,policy),chat_ids=[cid for cid in membership[getattr(f,"id",0)] if policy.can_read(cid)])
                                    for f in result.filters], "untrusted_content": True}
            if operation == "search":
                query = nonempty(args["query"], 200)
                limit = integer(args.get("limit", 20), 1, 50)
                offset = integer(args.get("offset_id", 0), 0, 2**31-1)
                if target is None and offset:
                    raise GuardError("global_search_use_next_offset")
                # Telethon iter_messages(None, search=...) uses searchGlobal and its complete offset tuple.
                kwargs = {"search": query, "limit": limit if peer else 500}
                if peer:
                    kwargs["offset_id"] = offset
                else:
                    token = args.get("next_offset")
                    if token:
                        # Opaque, server-held continuation; never a caller-supplied TL object.
                        raise GuardError("global_search_pagination_not_supported")
                found = []
                async for m in client.iter_messages(peer, **kwargs):
                    if runtime.policy().can_read(m.chat_id):
                        found.append(message_row(m))
                        if len(found) >= limit:
                            break
                rows = [m for m in found if runtime.policy().can_read(m["chat_id"])]
                return {"messages": rows, "next_offset_id": rows[-1]["id"] if peer and len(rows)==limit else None,
                        "global_result_limit": 500 if peer is None else None, "untrusted_content": True}
            if operation == "message":
                mid = integer(args["message_id"], 1, 2**31-1)
                m = await client.get_messages(peer, ids=mid)
                if not m or m.chat_id != target:
                    raise GuardError("message_not_found", 404)
                result = {"message": message_row(m), "untrusted_content": True}
            elif operation == "replies":
                mid = integer(args["message_id"], 1, 2**31-1)
                limit = integer(args.get("limit", 20), 1, 50)
                offset = integer(args.get("offset_id", 0), 0, 2**31-1)
                values = await client.get_messages(peer, reply_to=mid, offset_id=offset, limit=limit)
                # Discussion results may belong to the linked discussion group, with separate read rights.
                rows = [message_row(m) for m in values if runtime.policy().can_read(m.chat_id)]
                result = {"messages": rows, "next_offset_id": values[-1].id if len(values)==limit else None,
                          "untrusted_content": True}
            elif operation == "history":
                limit=integer(args.get("limit",20),1,50)
                offset=integer(args.get("offset_id",0),0,2**31-1)
                reply=args.get("reply_to")
                if reply is not None:integer(reply,1,2**31-1)
                kwargs={"limit":limit,"offset_id":offset}
                if reply is not None:kwargs["reply_to"]=reply
                values=await client.get_messages(peer,**kwargs)
                result={"messages":[message_row(m) for m in values if runtime.policy().can_read(m.chat_id)],
                        "next_offset_id":values[-1].id if len(values)==limit else None,"untrusted_content":True}
            elif operation == "info":
                entity = await client.get_entity(peer)
                if utils.get_peer_id(entity) != target:
                    raise GuardError("chat_identity_mismatch")
                if isinstance(entity, types.User):
                    full = await client(functions.users.GetFullUserRequest(peer))
                    data = full.full_user
                elif isinstance(entity, types.Channel):
                    data = (await client(functions.channels.GetFullChannelRequest(peer))).full_chat
                else:
                    data = (await client(functions.messages.GetFullChatRequest(entity.id))).full_chat
                result = {"chat_id": target, "title": getattr(entity,"title",None),
                          "first_name": getattr(entity,"first_name",None), "last_name": getattr(entity,"last_name",None),
                          "username": getattr(entity,"username",None), "phone": getattr(entity,"phone",None),
                          "about": getattr(data,"about",None), "participants_count": getattr(data,"participants_count",None),
                          "pinned_message_id":getattr(data,"pinned_msg_id",None),
                          "linked_chat_id": -(10**12+data.linked_chat_id) if getattr(data,"linked_chat_id",None) else None,
                          "forum": bool(getattr(entity,"forum",False)), "broadcast": bool(getattr(entity,"broadcast",False)),
                          "admin_rights":getattr(entity,"admin_rights",None).to_dict() if getattr(entity,"admin_rights",None) else None,
                          "creator":bool(getattr(entity,"creator",False)),
                          "can_read": True, "can_write": runtime.policy().can_write(target), "untrusted_content": True}
            elif operation == "members":
                limit = integer(args.get("limit", 50), 1, 100)
                offset = integer(args.get("offset", 0), 0, 10000)
                query = args.get("search", "")
                if not isinstance(query,str) or len(query)>200:
                    raise GuardError("invalid_search")
                # ChannelParticipantsSearch supports exact numeric offset and a bounded server-side query.
                if isinstance(peer, types.InputPeerChannel):
                    page = await client(functions.channels.GetParticipantsRequest(
                        channel=peer, filter=types.ChannelParticipantsSearch(query), offset=offset, limit=limit, hash=0))
                    users, total = page.users, page.count
                else:
                    if offset:
                        raise GuardError("participant_offset_requires_supergroup")
                    users = await client.get_participants(peer, limit=limit, search=query)
                    total = getattr(users,"total",len(users))
                result = {"members":[{"id":u.id,"first_name":u.first_name,"last_name":u.last_name,
                                      "username":u.username,"bot":bool(u.bot)} for u in users],
                          "next_offset": offset+len(users) if users and offset+len(users)<total else None,
                          "total": total, "untrusted_content":True}
            elif operation == "topics":
                limit=integer(args.get("limit",50),1,100)
                date=integer(args.get("offset_date",0),0,2**31-1)
                query=args.get("search","")
                if not isinstance(query,str) or len(query)>200:raise GuardError("invalid_search")
                from datetime import datetime,timezone
                page=await client(functions.messages.GetForumTopicsRequest(
                    peer=peer,offset_date=datetime.fromtimestamp(date,timezone.utc) if date else None,
                    offset_id=integer(args.get("offset_id",0),0,2**31-1),
                    offset_topic=integer(args.get("offset_topic",0),0,2**31-1),limit=limit,q=query or None))
                topics=[{"id":t.id,"title":getattr(t,"title",None),"top_message":getattr(t,"top_message",None),
                         "closed":bool(getattr(t,"closed",False)),"hidden":bool(getattr(t,"hidden",False))} for t in page.topics]
                last=page.topics[-1] if page.topics else None
                last_date=getattr(last,"date",None)
                if last and not getattr(page,"order_by_create_date",False):
                    last_date=next((m.date for m in getattr(page,"messages",[]) if m.id==getattr(last,"top_message",None)),last_date)
                result={"topics":topics,"next_offset":{"offset_date":int(last_date.timestamp()) if last_date else 0,
                        "offset_id":getattr(last,"top_message",0),"offset_topic":last.id} if last and len(topics)==limit else None,
                        "untrusted_content":True}
            elif operation == "download":
                return await download(runtime,client,peer,target,args)
            elif operation not in ("message","replies"):
                raise GuardError("unsupported_read_operation")
            runtime.policy().require_read(target)
            return result


async def download(runtime,client,peer,target,args):
    mid=integer(args["message_id"],1,2**31-1)
    m=await client.get_messages(peer,ids=mid)
    if not m or m.chat_id!=target:
        raise GuardError("message_not_found",404)
    if not m.media:
        raise GuardError("message_has_no_downloadable_media")
    if getattr(m,"noforwards",False):
        raise GuardError("protected_content_download_refused",403)
    size=getattr(getattr(m,"file",None),"size",0) or 0
    if size>MAX_FILE_BYTES:
        raise GuardError("download_too_large")
    directory=runtime.download_root
    checked(directory.parent,directory=True)
    checked(directory,directory=True,missing=True)
    directory.mkdir(exist_ok=True)
    checked(directory,directory=True)
    # Never use a Telegram-supplied filename as a path; no caller-selected output directory.
    suffix=getattr(getattr(m,"file",None),"ext","") or ".bin"
    if len(suffix)>12 or not suffix.startswith('.') or not suffix[1:].isalnum():
        suffix=".bin"
    dest=directory/(secrets.token_hex(16)+suffix)
    count=0; digest=hashlib.sha256()
    try:
        with dest.open('xb') as out:
            async for chunk in client.iter_download(m.media):
                runtime.policy().require_read(target)
                count+=len(chunk)
                if count>MAX_FILE_BYTES:
                    raise GuardError("download_too_large")
                out.write(chunk);digest.update(chunk)
        runtime.policy().require_read(target)
        return {"chat_id":target,"message_id":mid,"path":str(dest),"size":count,"sha256":digest.hexdigest(),
                "filename":getattr(getattr(m,"file",None),"name",None),"untrusted_content":True}
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


async def mutate(runtime,operation,target,request_id,authorization,**args):
    from telethon import functions, types
    from .engine import send_failure_code
    target=chat_id(target)
    runtime.policy().require_write(target)
    intent=Send.parse({"chat_id":target,"request_id":request_id,"authorization":authorization,"text":"chat operation"})
    intent=replace(intent,options={"operation":operation,"arguments":args})
    intent.check_expiry()
    mid=integer(args["message_id"],1,2**31-1) if "message_id" in args else None
    ids=args.get("message_ids",[mid] if mid else [])
    if not isinstance(ids,list) or not 1<=len(ids)<=100 or len(set(ids))!=len(ids):
        raise GuardError("invalid_message_ids")
    for item in ids: integer(item,1,2**31-1)
    if operation=="edit":
        nonempty(args["text"],4096)
        if len(args["text"].encode('utf-16-le'))//2>4096: raise GuardError("text_too_long")
    if operation=="reaction":
        if not isinstance(args["emoji"],str) or len(args["emoji"])>32: raise GuardError("invalid_reaction")
    if operation=="vote":
        options=args["options"]
        if not isinstance(options,list) or len(options)>10: raise GuardError("invalid_poll_options")
        import base64
        try:
            values=[base64.b64decode(o,validate=True) for o in options]
            if any(not 1<=len(v)<=100 for v in values): raise ValueError()
        except (ValueError,TypeError): raise GuardError("invalid_poll_options") from None
    async with runtime.mutex:
        async with runtime.connection() as broker:
            def guard():
                intent.check_expiry()
                return input_peer(runtime.policy().require_write(target))
            peer=guard();client=broker.transport.client
            old=broker.store.replay(intent)
            if old:return dict(old,state="applied",operation=operation)
            if operation=="edit":
                request=functions.messages.EditMessageRequest(peer=peer,id=mid,message=args["text"],no_webpage=True,entities=[])
            elif operation=="delete":
                if isinstance(peer,types.InputPeerChannel):
                    request=functions.channels.DeleteMessagesRequest(channel=peer,id=ids)
                else:
                    # Non-channel deleteMessages has no peer parameter. Bind every global ID to this chat first.
                    found=await client.get_messages(peer,ids=ids)
                    if len(found)!=len(ids) or any(not m or m.chat_id!=target or m.id!=i for m,i in zip(found,ids)):
                        raise GuardError("message_chat_mismatch",403)
                    request=functions.messages.DeleteMessagesRequest(id=ids,revoke=True)
            elif operation=="reaction":
                request=functions.messages.SendReactionRequest(peer=peer,msg_id=mid,
                          reaction=[types.ReactionEmoji(args["emoji"])] if args["emoji"] else [])
            elif operation=="pin":
                request=functions.messages.UpdatePinnedMessageRequest(peer=peer,id=mid,unpin=args["unpin"],silent=args["silent"])
            elif operation=="read":
                request=(functions.channels.ReadHistoryRequest(channel=peer,max_id=mid) if isinstance(peer,types.InputPeerChannel)
                         else functions.messages.ReadHistoryRequest(peer=peer,max_id=mid))
            elif operation=="vote":
                request=functions.messages.SendVoteRequest(peer=peer,msg_id=mid,options=values)
            else:
                raise GuardError("unsupported_write_operation")
            guard()
            old=broker.store.reserve(intent,runtime.policy())
            if old: return dict(old,state="applied",operation=operation)
            try:
                guard()
                await asyncio.wait_for(client(request),30)
                broker.store.finish(intent.request_id,ids)
            except BaseException as error:
                broker.store.unknown(intent.request_id,send_failure_code(error))
                if isinstance(error,asyncio.CancelledError):raise
                raise GuardError("delivery_unknown_do_not_resend",409) from None
            return {"state":"applied","operation":operation,"chat_id":target,"request_id":intent.request_id,
                    "message_ids":ids,"replayed":False}
