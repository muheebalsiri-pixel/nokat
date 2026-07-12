# -*- coding: utf-8 -*-
"""
سكريبت نقل الرسائل من قنوات تيليجرام عامة إلى قناة واحدة عبر بوت.
- الحساب الشخصي (Telethon) يقرأ الرسائل فقط.
- البوت هو من يقوم بالنشر الفعلي في القناة الهدف.
- يتم استبعاد أي رسالة تحتوي على: روابط, معرّفات (@), أزرار, استفتاءات,
  رسائل محوّلة (Forwarded), أو أي كلمة من الكلمات الممنوعة.
- لا يتم "تنظيف" الرسائل من هذه العناصر، بل يتم استبعاد الرسالة بالكامل.
- يتم استبعاد أي فيديو يزيد حجمه عن 5 ميجابايت لحماية دقائق الخطة المجانية.
"""

import asyncio
import hashlib
import io
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import requests
from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.types import MessageMediaPoll

import config

URL_PATTERN = re.compile(
    r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+)", re.IGNORECASE
)
MENTION_PATTERN = re.compile(r"@[A-Za-z0-9_]{3,}")


def log(msg):
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def load_state():
    if os.path.exists(config.STATE_FILE):
        try:
            with open(config.STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"تحذير: فشل قراءة state.json ({e})، سيتم إنشاء حالة جديدة")
    return {"forwarded_ids": {}, "content_hashes": {}, "last_run": None}


def save_state(state):
    with open(config.STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def prune_content_hashes(state):
    cutoff = datetime.now(timezone.utc) - timedelta(
        hours=config.CONTENT_HASH_RETENTION_HOURS
    )
    kept = {}
    for h, ts in state.get("content_hashes", {}).items():
        try:
            if datetime.fromisoformat(ts) >= cutoff:
                kept[h] = ts
        except Exception:
            continue
    state["content_hashes"] = kept


def prune_forwarded_ids(state):
    for ch, ids in state.get("forwarded_ids", {}).items():
        if len(ids) > config.MAX_STORED_IDS_PER_CHANNEL:
            state["forwarded_ids"][ch] = ids[-config.MAX_STORED_IDS_PER_CHANNEL :]


def normalize_text(text):
    if not text:
        return ""
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text


def content_hash(text):
    norm = normalize_text(text).lower()
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def contains_banned_word(text):
    if not text:
        return False
    for word in config.BANNED_WORDS:
        if word and word in text:
            return True
    return False


def contains_link_or_mention(message):
    text = message.raw_text or ""
    if URL_PATTERN.search(text):
        return True
    if MENTION_PATTERN.search(text):
        return True

    entities = message.entities or []
    for ent in entities:
        ent_name = type(ent).__name__
        if ent_name in (
            "MessageEntityUrl",
            "MessageEntityTextUrl",
            "MessageEntityMention",
            "MessageEntityMentionName",
        ):
            return True
    return False


def is_poll(message):
    return isinstance(message.media, MessageMediaPoll)


def has_buttons(message):
    return bool(message.buttons)


def is_forwarded(message):
    return message.fwd_from is not None


def should_skip(message):
    """يرجع (True, السبب) إذا وجب استبعاد الرسالة، أو (False, None) إذا كانت صالحة للنقل."""
    if is_forwarded(message):
        return True, "رسالة محوّلة (Forwarded)"

    if is_poll(message):
        return True, "استفتاء (Poll)"

    if has_buttons(message):
        return True, "تحتوي أزرار (Buttons)"

    if contains_link_or_mention(message):
        return True, "تحتوي رابط أو معرّف (@)"

    text = message.raw_text or ""
    if contains_banned_word(text):
        return True, "تحتوي كلمة ممنوعة"

    if not text and not message.media:
        return True, "رسالة فارغة بدون نص أو وسائط"

    return False, None


def send_text(text):
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMessage"
    resp = requests.post(
        url,
        data={
            "chat_id": f"@{config.TARGET_CHANNEL}"
            if not config.TARGET_CHANNEL.lstrip("-").isdigit()
            else config.TARGET_CHANNEL,
            "text": text,
        },
        timeout=30,
    )
    return resp.ok, resp.text


def send_media(file_bytes, filename, caption, media_kind):
    endpoint_map = {
        "photo": ("sendPhoto", "photo"),
        "video": ("sendVideo", "video"),
        "document": ("sendDocument", "document"),
        "audio": ("sendAudio", "audio"),
    }
    endpoint, field = endpoint_map.get(media_kind, ("sendDocument", "document"))
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/{endpoint}"

    chat_id = (
        f"@{config.TARGET_CHANNEL}"
        if not config.TARGET_CHANNEL.lstrip("-").isdigit()
        else config.TARGET_CHANNEL
    )

    files = {field: (filename, file_bytes)}
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption

    resp = requests.post(url, data=data, files=files, timeout=120)
    return resp.ok, resp.text


def send_media_group(media_list):
    """إرسال مجموعة وسائط (ألبوم) عبر البوت"""
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/sendMediaGroup"
    
    chat_id = (
        f"@{config.TARGET_CHANNEL}"
        if not config.TARGET_CHANNEL.lstrip("-").isdigit()
        else config.TARGET_CHANNEL
    )
    
    files = {}
    media_json = []
    
    for i, item in enumerate(media_list):
        attach_name = f"media_{i}"
        files[attach_name] = (item["filename"], item["bytes"])
        
        media_entry = {
            "type": item["media_kind"],
            "media": f"attach://{attach_name}"
        }
        # التلجرام يقبل الشرح (caption) على عناصر الألبوم وعادةً نضعه على العنصر الأول أو حيثما وجد النص
        if item["caption"]:
            media_entry["caption"] = item["caption"]
            
        media_json.append(media_entry)
        
    data = {
        "chat_id": chat_id,
        "media": json.dumps(media_json)
    }
    
    resp = requests.post(url, data=data, files=files, timeout=180)
    return resp.ok, resp.text


def detect_media_kind(message):
    if message.photo:
        return "photo"
    if message.video:
        return "video"
    if message.audio or message.voice:
        return "audio"
    if message.document:
        return "document"
    return None


async def process_channel(client, channel_username, state, since):
    forwarded_ids = state.setdefault("forwarded_ids", {})
    channel_ids = set(forwarded_ids.get(channel_username, []))

    sent_count = 0
    skipped_count = 0

    try:
        entity = await client.get_entity(channel_username)
    except Exception as e:
        log(f"  خطأ: تعذر الوصول للقناة {channel_username}: {e}")
        return sent_count, skipped_count

    # جلب الرسائل أولاً لتجميع الألبومات بناءً على grouped_id
    raw_messages = []
    async for message in client.iter_messages(entity, limit=config.MESSAGES_PER_CHANNEL):
        raw_messages.append(message)

    # تجميع الرسائل التي تنتمي لنفس الألبوم
    # المفتاح: grouped_id، القيمة: قائمة الرسائل التابعة له
    albums = {}
    # قائمة لحفظ الترتيب الأصلي للرسائل (الفردية والألبومات مجتمعة ككتل)
    processed_blocks = []
    seen_groups = set()

    for msg in raw_messages:
        if msg.grouped_id:
            if msg.grouped_id not in albums:
                albums[msg.grouped_id] = []
            albums[msg.grouped_id].append(msg)
            if msg.grouped_id not in seen_groups:
                processed_blocks.append(("album", msg.grouped_id))
                seen_groups.add(msg.grouped_id)
        else:
            processed_blocks.append(("single", msg))

    # معالجة الكتل بالترتيب المجلوب
    for block_type, block_data in processed_blocks:
        if block_type == "single":
            message = block_data
            
            if message.date is None or message.date < since:
                continue
            if message.id in channel_ids:
                continue

            channel_ids.add(message.id)

            skip, reason = should_skip(message)
            if skip:
                skipped_count += 1
                log(f"  [تجاهل] {channel_username}#{message.id}: {reason}")
                continue

            text = message.raw_text or ""
            h = content_hash(text) if text else None

            if h and h in state.get("content_hashes", {}):
                skipped_count += 1
                log(f"  [تجاهل] {channel_username}#{message.id}: محتوى مكرر (قناة أخرى)")
                continue

            media_kind = detect_media_kind(message)
            
            if media_kind == "video" and message.video:
                if message.video.size > 3 * 1024 * 1024:
                    skipped_count += 1
                    log(f"  [تجاهل] {channel_username}#{message.id}: حجم الفيديو أكبر من 3 ميجابايت ({round(message.video.size / (1024*1024), 2)} MB)")
                    continue

            ok = False
            try:
                if media_kind:
                    buf = io.BytesIO()
                    await client.download_media(message, file=buf)
                    buf.seek(0)
                    filename = f"{channel_username}_{message.id}"
                    ok, resp_text = send_media(buf, filename, text, media_kind)
                elif text:
                    ok, resp_text = send_text(text)
                else:
                    skipped_count += 1
                    continue

                if ok:
                    sent_count += 1
                    log(f"  [تم النشر] {channel_username}#{message.id}")
                    if h:
                        state.setdefault("content_hashes", {})[h] = datetime.now(timezone.utc).isoformat()
                else:
                    log(f"  [فشل الإرسال] {channel_username}#{message.id}: {resp_text}")

            except Exception as e:
                log(f"  [خطأ أثناء الإرسال] {channel_username}#{message.id}: {e}")

            await asyncio.sleep(1.5)

        elif block_type == "album":
            grouped_id = block_data
            # رسائل الألبوم الواحد تأتي مرتبة عكسياً من الأحدث للأقدم، نعيد ترتيبها لتصبح من الأقدم للأحدث (الترتيب الطبيعي للألبوم)
            album_messages = sorted(albums[grouped_id], key=lambda m: m.id)
            
            # التحقق مما إذا كانت كل رسائل الألبوم قد تم معالجتها سابقاً أو خارج النطاق الزمني
            valid_album_messages = []
            skip_album = False
            skip_reason = ""

            for msg in album_messages:
                if msg.date is None or msg.date < since:
                    continue
                if msg.id in channel_ids:
                    continue
                
                channel_ids.add(msg.id)
                
                # فحص الفلاتر لكل رسالة داخل الألبوم
                skip, reason = should_skip(msg)
                if skip:
                    skip_album = True
                    skip_reason = f"أحد عناصر الألبوم تم استبعاده بسبب: {reason}"
                    break
                
                text = msg.raw_text or ""
                h = content_hash(text) if text else None
                if h and h in state.get("content_hashes", {}):
                    skip_album = True
                    skip_reason = "محتوى نص الألبوم مكرر (قناة أخرى)"
                    break
                
                media_kind = detect_media_kind(msg)
                # لا يمكن إرسال ألبوم يحتوي على مستندات أو صوتيات مختلطة بالصور/الفيديو عبر البوت بسهولة، ويفضل فقط photo و video للألبومات الرياضية
                if media_kind not in ("photo", "video"):
                    skip_album = True
                    skip_reason = f"نوع وسائط غير مدعوم في الألبومات المجمعة ({media_kind})"
                    break
                    
                if media_kind == "video" and msg.video:
                    if msg.video.size > 3 * 1024 * 1024:
                        skip_album = True
                        skip_reason = f"أحد فيديوهات الألبوم أكبر من 3 ميجابايت ({round(msg.video.size / (1024*1024), 2)} MB)"
                        break
                
                valid_album_messages.append(msg)

            if skip_album:
                skipped_count += len(album_messages)
                log(f"  [تجاهل ألبوم] {channel_username} (Group: {grouped_id}): {skip_reason}")
                continue

            if not valid_album_messages:
                continue

            # تنزيل وسائط الألبوم وتجهيزها للإرسال الجماعي
            media_to_send = []
            main_hash = None
            try:
                for msg in valid_album_messages:
                    text = msg.raw_text or ""
                    if text and not main_hash:
                        main_hash = content_hash(text)
                        
                    media_kind = detect_media_kind(msg)
                    buf = io.BytesIO()
                    await client.download_media(msg, file=buf)
                    buf.seek(0)
                    filename = f"{channel_username}_{msg.id}"
                    
                    media_to_send.append({
                        "bytes": buf,
                        "filename": filename,
                        "caption": text,
                        "media_kind": media_kind
                    })

                # إرسال الألبوم بالكامل
                ok, resp_text = send_media_group(media_to_send)
                
                if ok:
                    sent_count += 1 # نحسبها عملية نشر واحدة كألبوم
                    log(f"  [تم نشر ألبوم بالكامل] {channel_username} (Group: {grouped_id}) يحتوي على {len(media_to_send)} عناصر")
                    if main_hash:
                        state.setdefault("content_hashes", {})[main_hash] = datetime.now(timezone.utc).isoformat()
                else:
                    log(f"  [فشل إرسال الألبوم] {channel_username} (Group: {grouped_id}): {resp_text}")
                    
            except Exception as e:
                log(f"  [خطأ أثناء إرسال الألبوم] {channel_username} (Group: {grouped_id}): {e}")

            await asyncio.sleep(2.0)

    forwarded_ids[channel_username] = list(channel_ids)
    return sent_count, skipped_count


async def main():
    if not config.API_ID or not config.API_HASH or not config.SESSION_STRING:
        log("خطأ: TG_API_ID / TG_API_HASH / TG_SESSION غير مضبوطة")
        sys.exit(1)

    if not config.BOT_TOKEN:
        log("خطأ: BOT_TOKEN غير مضبوط")
        sys.exit(1)

    state = load_state()
    since = datetime.now(timezone.utc) - timedelta(hours=config.HOURS_WINDOW)

    client = TelegramClient(
        StringSession(config.SESSION_STRING), config.API_ID, config.API_HASH
    )

    total_sent = 0
    total_skipped = 0

    async with client:
        for channel in config.SOURCE_CHANNELS:
            log(f"فحص قناة: {channel}")
            sent, skipped = await process_channel(client, channel, state, since)
            total_sent += sent
            total_skipped += skipped

    prune_content_hashes(state)
    prune_forwarded_ids(state)
    state["last_run"] = datetime.now(timezone.utc).isoformat()
    save_state(state)

    log(f"انتهى التشغيل. تم النشر: {total_sent} | تم التجاهل: {total_skipped}")


if __name__ == "__main__":
    asyncio.run(main())
