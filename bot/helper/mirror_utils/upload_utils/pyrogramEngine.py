#!/usr/bin/env python3
from asyncio import sleep
from logging import ERROR, getLogger
from os import path as ospath
from os import walk
from re import match as re_match
from re import sub as re_sub
from time import time

from aiofiles.os import path as aiopath
from aiofiles.os import remove as aioremove
from aiofiles.os import rename as aiorename
from natsort import natsorted
from PIL import Image
from pyrogram.errors import FloodWait, RPCError
from pyrogram.types import InputMediaDocument, InputMediaVideo
from tenacity import (RetryError, retry, retry_if_exception_type,
                      stop_after_attempt, wait_exponential)

from bot import (GLOBAL_EXTENSION_FILTER, IS_PREMIUM_USER, bot, config_dict,
                 user, user_data)
from bot.helper.ext_utils.bot_utils import (get_readable_file_size,
                                            sync_to_async)
from bot.helper.ext_utils.fs_utils import (clean_unwanted, get_document_type,
                                           get_media_info, take_ss)
from bot.helper.telegram_helper.button_build import ButtonMaker

LOGGER = getLogger(__name__)
getLogger("pyrogram").setLevel(ERROR)


class TgUploader:

    def __init__(self, name=None, path=None, size=0, listener=None):
        self.name = name
        self.uploaded_bytes = 0
        self._last_uploaded = 0
        self.__listener = listener
        self.__path = path
        self.__start_time = time()
        self.__total_files = 0
        self.__is_cancelled = False
        self.__thumb = f"Thumbnails/{listener.message.from_user.id}.jpg"
        self.__msgs_dict = {}
        self.__corrupted = 0
        self.__is_corrupted = False
        self.__size = size
        self.__button = None
        self.__media_dict = {'videos': {}, 'documents': {}}
        self.__last_msg_in_group = False
        self.__sent_DMmsg = None
        self.__upload_4gb = 0

    async def __upload_progress(self, current, total):
        if self.__is_cancelled:
            if self.__upload_4gb > 0:
                user.stop_transmission()
            bot.stop_transmission()
        chunk_size = current - self._last_uploaded
        self._last_uploaded = current
        self.uploaded_bytes += chunk_size

    async def __user_settings(self):
        user_id = self.__listener.message.from_user.id
        user_dict = user_data.get(user_id, {})
        self.__as_doc = user_dict.get('as_doc') or config_dict['AS_DOCUMENT']
        self.__media_group = user_dict.get('media_group') or config_dict['MEDIA_GROUP']
        self.__lprefix = user_dict.get('lprefix') or config_dict['LEECH_FILENAME_PREFIX']
        if not await aiopath.exists(self.__thumb):
            self.__thumb = None

    async def __msg_to_reply(self):
        if DUMP_CHAT:= config_dict['DUMP_CHAT']:
            if self.__listener.logMessage:
                self.__sent_msg = await bot.copy_message(DUMP_CHAT, self.__listener.logMessage.chat.id, self.__listener.logMessage.id)
            else:
                msg = f'<b><a href="{self.__listener.message.link}">Source</a></b>' if self.__listener.isSuperGroup else self.__listener.message.text
                msg = f'{msg}\n\n<b>#cc</b>: {self.__listener.tag} (<code>{self.__listener.message.from_user.id}</code>)'
                self.__sent_msg = await bot.send_message(DUMP_CHAT, msg, disable_web_page_preview=True)
        elif IS_PREMIUM_USER:
            if not self.__listener.isSuperGroup:
                await self.__listener.onUploadError('Use SuperGroup to leech with User!')
                return
            self.__sent_msg = await bot.get_messages(chat_id=self.__listener.message.chat.id,
                                                          message_ids=self.__listener.uid)
        else:
            self.__sent_msg = self.__listener.message
        if self.__listener.dmMessage:
            self.__sent_DMmsg = self.__listener.dmMessage
        if self.__listener.isSuperGroup or config_dict['DUMP_CHAT']:
            btn = ButtonMaker()
            btn.ibutton('Save Message', 'save', 'footer')
            self.__button = btn.build_menu(1)

    async def __prepare_file(self, up_path, file_, dirpath):
        if self.__lprefix:
            cap_mono = f"{self.__lprefix} <code>{file_}</code>"
            self.__lprefix = re_sub('<.*?>', '', self.__lprefix)
            file_ = f"{self.__lprefix} {file_}"
            new_path = ospath.join(dirpath, file_)
            await aiorename(up_path, new_path)
            up_path = new_path
        else:
            cap_mono = f"<code>{file_}</code>"
        return up_path, cap_mono

    def __get_input_media(self, subkey, key):
        rlist = []
        for msg in self.__media_dict[key][subkey]:
            if key == 'videos':
                input_media = InputMediaVideo(media=msg.video.file_id, caption=msg.caption)
            else:
                input_media = InputMediaDocument(media=msg.document.file_id, caption=msg.caption)
            rlist.append(input_media)
        return rlist

    async def __send_media_group(self, subkey, key, msgs):
        msgs_list = await msgs[0].reply_to_message.reply_media_group(media=self.__get_input_media(subkey, key),
                                                                     quote=True,
                                                                     disable_notification=True)
        for msg in msgs:
            if msg.link in self.__msgs_dict:
                del self.__msgs_dict[msg.link]
            await msg.delete()
        del self.__media_dict[key][subkey]
        if self.__listener.isSuperGroup or config_dict['DUMP_CHAT']:
            for m in msgs_list:
                self.__msgs_dict[m.link] = m.caption
        self.__sent_msg = msgs_list[-1]

    async def upload(self, o_files, m_size):
        await self.__msg_to_reply()
        await self.__user_settings()
        for dirpath, subdir, files in sorted(await sync_to_async(walk, self.__path)):
            for file_ in natsorted(files):
                try:
                    if file_.lower().endswith(tuple(GLOBAL_EXTENSION_FILTER)):
                        continue
                    up_path = ospath.join(dirpath, file_)
                    f_size = await aiopath.getsize(up_path)
                    if self.__listener.seed and file_ in o_files and f_size in m_size:
                        continue
                    self.__total_files += 1
                    if f_size == 0:
                        LOGGER.error(f"{up_path} size is zero, telegram don't upload zero size files")
                        self.__corrupted += 1
                        continue
                    if self.__is_cancelled:
                        return
                    if f_size > 2097152000 and IS_PREMIUM_USER and self.__sent_msg._client.me.is_bot:
                        self.__sent_msg = await user.get_messages(chat_id=self.__sent_msg.chat.id, message_ids=self.__sent_msg.id)
                        self.__upload_4gb += 1
                    elif not self.__sent_msg._client.me.is_bot:
                        self.__sent_msg = await bot.get_messages(chat_id=self.__sent_msg.chat.id, message_ids=self.__sent_msg.id)
                    if self.__last_msg_in_group:
                        group_lists = [x for v in self.__media_dict.values() for x in v.keys()]
                        if (match := re_match(r'.+(?=\.0*\d+$)|.+(?=\.part\d+\..+)', up_path)) and match.group(0) not in group_lists:
                            for key, value in list(self.__media_dict.items()):
                                for subkey, msgs in list(value.items()):
                                    if len(msgs) > 1:
                                        await self.__send_media_group(subkey, key, msgs)
                    self.__last_msg_in_group = False
                    up_path, cap_mono = await self.__prepare_file(up_path, file_, dirpath)
                    self._last_uploaded = 0
                    uploaded_doc = await self.__upload_file(up_path, cap_mono)
                    if self.__is_cancelled:
                        return
                    if not self.__listener.seed or self.__listener.newDir or dirpath.endswith("splited_files_mltb"):
                        await aioremove(uploaded_doc)
                    if not self.__is_corrupted and (self.__listener.isSuperGroup or config_dict['DUMP_CHAT']):
                        self.__msgs_dict[self.__sent_msg.link] = file_
                    await sleep(1)
                except Exception as err:
                    if isinstance(err, RetryError):
                        LOGGER.info(f"Total Attempts: {err.last_attempt.attempt_number}")
                    else:
                        LOGGER.error(f"{err}. Path: {up_path}")
                    if self.__is_cancelled:
                        return
                    continue
        for key, value in list(self.__media_dict.items()):
            for subkey, msgs in list(value.items()):
                if len(msgs) > 1:
                    await self.__send_media_group(subkey, key, msgs)
        if self.__is_cancelled:
            return
        if self.__listener.seed and not self.__listener.newDir:
            await clean_unwanted(self.__path)
        if self.__total_files == 0:
            await self.__listener.onUploadError("No files to upload. In case you have filled EXTENSION_FILTER, then check if all files have those extensions or not.")
            return
        if self.__total_files <= self.__corrupted:
            await self.__listener.onUploadError('Files Corrupted or unable to upload. Check logs!')
            return
        if config_dict['DUMP_CHAT']:
            msg = f'<b><a href="{self.__listener.message.link}">Source</a></b>' if self.__listener.isSuperGroup else self.__listener.message.text
            msg = f'{msg}\n\n<b>#LeechCompleted</b>: {self.__listener.tag} #id{self.__listener.message.from_user.id}'
            await self.__sent_msg.reply_text(text=msg, quote=True)
        LOGGER.info(f"Leech Completed: {self.name}")
        size = get_readable_file_size(self.__size)
        await self.__listener.onUploadComplete(None, size, self.__msgs_dict, self.__total_files, self.__corrupted, self.name)

    @retry(wait=wait_exponential(multiplier=2, min=4, max=8), stop=stop_after_attempt(3),
           retry=retry_if_exception_type(Exception))
    async def __upload_file(self, up_path, cap_mono, force_document=False):
        if self.__thumb is not None and not await aiopath.exists(self.__thumb):
            self.__thumb = None
        thumb = self.__thumb
        self.__is_corrupted = False
        try:
            is_video, is_audio, is_image = await get_document_type(up_path)
            if self.__as_doc or force_document or (not is_video and not is_audio and not is_image):
                key = 'documents'
                if is_video and thumb is None:
                    thumb = await take_ss(up_path, None)
                    if self.__is_cancelled:
                        return
                self.__sent_msg = await self.__sent_msg.reply_document(document=up_path,
                                                                       quote=True,
                                                                       thumb=thumb,
                                                                       caption=cap_mono,
                                                                       force_document=True,
                                                                       reply_markup=self.__button,
                                                                       disable_notification=True,
                                                                       progress=self.__upload_progress)
            elif is_video:
                key = 'videos'
                duration = (await get_media_info(up_path))[0]
                if thumb is None:
                    thumb = await take_ss(up_path, duration)
                    if self.__is_cancelled:
                        return
                if thumb is not None:
                    with Image.open(thumb) as img:
                        width, height = img.size
                else:
                    width = 480
                    height = 320
                if not up_path.upper().endswith(("MKV", "MP4")):
                    new_path = f"{up_path.rsplit('.', 1)[0]}.mp4"
                    await aiorename(up_path, new_path)
                    up_path = new_path
                self.__sent_msg = await self.__sent_msg.reply_video(video=up_path,
                                                                    quote=True,
                                                                    caption=cap_mono,
                                                                    duration=duration,
                                                                    width=width,
                                                                    height=height,
                                                                    thumb=thumb,
                                                                    supports_streaming=True,
                                                                    reply_markup=self.__button,
                                                                    disable_notification=True,
                                                                    progress=self.__upload_progress)
            elif is_audio:
                key = 'audios'
                duration , artist, title = await get_media_info(up_path)
                self.__sent_msg = await self.__sent_msg.reply_audio(audio=up_path,
                                                                    quote=True,
                                                                    caption=cap_mono,
                                                                    duration=duration,
                                                                    performer=artist,
                                                                    title=title,
                                                                    thumb=thumb,
                                                                    reply_markup=self.__button,
                                                                    disable_notification=True,
                                                                    progress=self.__upload_progress)
            else:
                key = 'photos'
                self.__sent_msg = await self.__sent_msg.reply_photo(photo=up_path,
                                                                    quote=True,
                                                                    caption=cap_mono,
                                                                    reply_markup=self.__button,
                                                                    disable_notification=True,
                                                                    progress=self.__upload_progress)
            if not self.__is_cancelled and self.__media_group and (self.__sent_msg.video or self.__sent_msg.document):
                key = 'documents' if self.__sent_msg.document else 'videos'
                if match := re_match(r'.+(?=\.0*\d+$)|.+(?=\.part\d+\..+)', up_path):
                    pname = match.group(0)
                    if pname in self.__media_dict[key].keys():
                        self.__media_dict[key][pname].append(self.__sent_msg)
                    else:
                        self.__media_dict[key][pname] = [self.__sent_msg]
                    msgs = self.__media_dict[key][pname]
                    if len(msgs) == 10:
                        await self.__send_media_group(pname, key, msgs)
                    else:
                        self.__last_msg_in_group = True

            if not self.__is_cancelled and self.__sent_DMmsg:
                await sleep(1)
                self.__sent_DMmsg = await self.__sent_msg.copy(
                chat_id=self.__sent_DMmsg.chat.id,
                reply_markup=None,
                reply_to_message_id=self.__sent_DMmsg.id)
            return up_path
        except FloodWait as f:
            LOGGER.warning(str(f))
            await sleep(f.value)
        except Exception as err:
            err_type = "RPCError: " if isinstance(err, RPCError) else ""
            LOGGER.error(f"{err_type}{err}. Path: {up_path}")
            if 'Telegram says: [400' in str(err) and key != 'documents':
                LOGGER.error(f"Retrying As Document. Path: {up_path}")
                return await self.__upload_file(up_path, cap_mono, True)
            raise err
        finally:
            if self.__thumb is None and thumb is not None and await aiopath.exists(thumb):
                await aioremove(thumb)

    @property
    def speed(self):
        try:
            return self.uploaded_bytes / (time() - self.__start_time)
        except:
            return 0

    async def cancel_download(self):
        self.__is_cancelled = True
        LOGGER.info(f"Cancelling Upload: {self.name}")
        await self.__listener.onUploadError('your upload has been stopped!')