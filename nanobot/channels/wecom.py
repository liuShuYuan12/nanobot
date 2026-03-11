# -*- coding: utf-8 -*-
"""
@File : wecom.py
@Author : bookfar
@Time : 2026/03/11 20:00
@Desc : wecom AI bot channel（WebSocket ）
"""

import asyncio
import re
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from nanobot.bus.events import OutboundMessage
from nanobot.bus.queue import MessageBus
from nanobot.channels.base import BaseChannel
from nanobot.config.paths import get_media_dir
from nanobot.config.schema import WeComConfig

try:
    from wecom_aibot_sdk import WSClient, generate_req_id

    WECOM_SDK_AVAILABLE = True
except ImportError:
    WECOM_SDK_AVAILABLE = False
    WSClient = None  # type: ignore[assignment,misc]
    generate_req_id = None  # type: ignore[assignment,misc]


class _LoguruAdapter:
    """将 loguru 适配为 wecom-aibot-sdk 的 Logger 协议。"""

    def debug(self, message: str, *args: Any) -> None:
        logger.debug("[WeCom SDK] " + message, *args)

    def info(self, message: str, *args: Any) -> None:
        logger.info("[WeCom SDK] " + message, *args)

    def warn(self, message: str, *args: Any) -> None:
        logger.warning("[WeCom SDK] " + message, *args)

    def error(self, message: str, *args: Any) -> None:
        logger.error("[WeCom SDK] " + message, *args)


def _parse_message_content(body: dict[str, Any]) -> dict[str, Any]:
    """
    解析企业微信消息体，提取文本、图片、文件、引用等内容。

    Returns:
        包含 text_parts, image_urls, image_aes_keys, file_urls,
        file_aes_keys, quote_content 的字典。
    """
    text_parts: list[str] = []
    image_urls: list[str] = []
    image_aes_keys: dict[str, str] = {}
    file_urls: list[str] = []
    file_aes_keys: dict[str, str] = {}
    quote_content: str | None = None

    msgtype = body.get("msgtype", "")

    # 图文混排消息
    if msgtype == "mixed" and body.get("mixed", {}).get("msg_item"):
        for item in body["mixed"]["msg_item"]:
            item_type = item.get("msgtype", "")
            if item_type == "text" and item.get("text", {}).get("content"):
                text_parts.append(item["text"]["content"])
            elif item_type == "image" and item.get("image", {}).get("url"):
                url = item["image"]["url"]
                image_urls.append(url)
                if item["image"].get("aeskey"):
                    image_aes_keys[url] = item["image"]["aeskey"]
    else:
        if body.get("text", {}).get("content"):
            text_parts.append(body["text"]["content"])
        if msgtype == "voice" and body.get("voice", {}).get("content"):
            text_parts.append(body["voice"]["content"])
        if body.get("image", {}).get("url"):
            url = body["image"]["url"]
            image_urls.append(url)
            if body["image"].get("aeskey"):
                image_aes_keys[url] = body["image"]["aeskey"]
        if msgtype == "file" and body.get("file", {}).get("url"):
            url = body["file"]["url"]
            file_urls.append(url)
            if body["file"].get("aeskey"):
                file_aes_keys[url] = body["file"]["aeskey"]

    # 引用消息
    quote = body.get("quote")
    if quote:
        qt = quote.get("msgtype", "")
        if qt == "text" and quote.get("text", {}).get("content"):
            quote_content = quote["text"]["content"]
        elif qt == "voice" and quote.get("voice", {}).get("content"):
            quote_content = quote["voice"]["content"]
        elif qt == "image" and quote.get("image", {}).get("url"):
            url = quote["image"]["url"]
            image_urls.append(url)
            if quote["image"].get("aeskey"):
                image_aes_keys[url] = quote["image"]["aeskey"]
        elif qt == "file" and quote.get("file", {}).get("url"):
            url = quote["file"]["url"]
            file_urls.append(url)
            if quote["file"].get("aeskey"):
                file_aes_keys[url] = quote["file"]["aeskey"]

    return {
        "text_parts": text_parts,
        "image_urls": image_urls,
        "image_aes_keys": image_aes_keys,
        "file_urls": file_urls,
        "file_aes_keys": file_aes_keys,
        "quote_content": quote_content,
    }


class WeComChannel(BaseChannel):
    """
    企业微信 AI 机器人通道。

    通过 wecom-aibot-sdk 的 WebSocket 长连接收发消息，
    支持私聊和群聊、文本/图片/语音/文件/图文混排消息。
    """

    name = "wecom"

    def __init__(self, config: WeComConfig, bus: MessageBus):
        super().__init__(config, bus)
        self.config: WeComConfig = config
        self._ws_client: Any = None
        self._stop_event: asyncio.Event | None = None
        self._background_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        """启动企业微信 WebSocket 长连接。"""
        if not WECOM_SDK_AVAILABLE:
            logger.error(
                "wecom-aibot-sdk 未安装，请运行: pip install wecom-aibot-sdk"
            )
            return

        if not self.config.bot_id or not self.config.secret:
            logger.error("企业微信 bot_id 和 secret 未配置")
            return

        self._running = True
        self._stop_event = asyncio.Event()

        logger.info(
            "正在初始化企业微信 WebSocket 连接，Bot ID: {}...",
            self.config.bot_id,
        )

        self._ws_client = WSClient(
            bot_id=self.config.bot_id,
            secret=self.config.secret,
            logger=_LoguruAdapter(),
            max_reconnect_attempts=-1,
            heartbeat_interval=30000,
        )

        self._ws_client.on("authenticated", self._on_authenticated)
        self._ws_client.on("disconnected", self._on_disconnected)
        self._ws_client.on("error", self._on_error)
        self._ws_client.on("message", self._on_message)

        await self._ws_client.connect()
        logger.info("企业微信通道已启动")

        # 保持运行直到收到停止信号
        await self._stop_event.wait()

    async def stop(self) -> None:
        """停止企业微信通道并释放资源。"""
        self._running = False

        if self._ws_client:
            try:
                await self._ws_client.disconnect()
            except Exception as e:
                logger.warning("断开企业微信连接时出错: {}", e)
            self._ws_client = None

        for task in self._background_tasks:
            task.cancel()
        self._background_tasks.clear()

        if self._stop_event:
            self._stop_event.set()

        logger.info("企业微信通道已停止")

    async def send(self, msg: OutboundMessage) -> None:
        """通过企业微信发送消息。"""
        if not self._ws_client or not self._ws_client.is_connected:
            logger.warning("企业微信 WebSocket 未连接，无法发送消息")
            return

        content = (msg.content or "").strip()
        if not content:
            return

        chat_id = msg.chat_id
        # 群聊 chat_id 带有 "group:" 前缀，发送时需要去掉
        if chat_id.startswith("group:"):
            chat_id = chat_id[6:]

        try:
            await self._ws_client.send_message(chat_id, {
                "msgtype": "markdown",
                "markdown": {"content": content},
            })
            logger.debug("企业微信消息已发送至 {}", msg.chat_id)
        except Exception as e:
            logger.error("发送企业微信消息失败: {}", e)

    # ------------------------------------------------------------------
    # 事件处理
    # ------------------------------------------------------------------

    def _on_authenticated(self) -> None:
        logger.info("企业微信认证成功")

    def _on_disconnected(self, reason: str) -> None:
        logger.warning("企业微信 WebSocket 断开: {}", reason)

    def _on_error(self, error: Exception) -> None:
        logger.error("企业微信 WebSocket 错误: {}", error)

    async def _on_message(self, frame: dict[str, Any]) -> None:
        """处理收到的企业微信消息帧。"""
        task = asyncio.create_task(self._process_message(frame))
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    # ------------------------------------------------------------------
    # 消息处理核心逻辑
    # ------------------------------------------------------------------

    async def _process_message(self, frame: dict[str, Any]) -> None:
        """解析消息并转发到 MessageBus。"""
        try:
            body: dict[str, Any] = frame.get("body", {})
            sender_id = body.get("from", {}).get("userid", "")
            chat_id_raw = body.get("chatid") or sender_id
            chat_type = body.get("chattype", "single")
            is_group = chat_type == "group"

            # 解析消息内容
            parsed = _parse_message_content(body)
            text_parts: list[str] = parsed["text_parts"]
            image_urls: list[str] = parsed["image_urls"]
            image_aes_keys: dict[str, str] = parsed["image_aes_keys"]
            file_urls: list[str] = parsed["file_urls"]
            file_aes_keys: dict[str, str] = parsed["file_aes_keys"]
            quote_content: str | None = parsed["quote_content"]

            text = "\n".join(text_parts).strip()

            # 群聊时移除 @机器人 提及
            if is_group:
                text = re.sub(r"@\S+", "", text).strip()

            # 如果文本为空但存在引用内容，使用引用内容
            if not text and quote_content:
                text = quote_content
                logger.debug("使用引用内容作为消息正文")

            # 没有有效内容则跳过
            if not text and not image_urls and not file_urls:
                logger.debug("跳过空消息（无文本、图片、文件或引用）")
                return

            # 群组策略检查
            if is_group and self.config.group_policy == "mention":
                original_text = "\n".join(text_parts).strip()
                if not re.search(r"@\S+", original_text):
                    logger.debug("群消息未 @机器人，根据策略跳过")
                    return

            # 构造 chat_id（群聊加 group: 前缀）
            chat_id = f"group:{chat_id_raw}" if is_group else chat_id_raw

            logger.info(
                "收到企业微信消息: chat_type={} chat_id={} sender={} text_len={} images={} files={}",
                chat_type, chat_id, sender_id, len(text),
                len(image_urls), len(file_urls),
            )

            # 下载媒体文件
            media_paths = await self._download_media(
                image_urls, image_aes_keys, file_urls, file_aes_keys
            )

            # 转发到 MessageBus
            await self._handle_message(
                sender_id=sender_id,
                chat_id=chat_id,
                content=text,
                media=media_paths,
                metadata={
                    "platform": "wecom",
                    "chat_type": chat_type,
                    "msg_id": body.get("msgid", ""),
                },
            )

        except Exception as e:
            logger.error("处理企业微信消息失败: {}", e)

    # ------------------------------------------------------------------
    # 媒体下载
    # ------------------------------------------------------------------

    async def _download_media(
        self,
        image_urls: list[str],
        image_aes_keys: dict[str, str],
        file_urls: list[str],
        file_aes_keys: dict[str, str],
    ) -> list[str]:
        """下载图片和文件到本地，返回本地文件路径列表。"""
        if not self._ws_client:
            return []

        media_dir = get_media_dir("wecom")
        media_dir.mkdir(parents=True, exist_ok=True)
        paths: list[str] = []

        for url in image_urls:
            path = await self._download_single(
                url, image_aes_keys.get(url), media_dir, "img"
            )
            if path:
                paths.append(path)

        for url in file_urls:
            path = await self._download_single(
                url, file_aes_keys.get(url), media_dir, "file"
            )
            if path:
                paths.append(path)

        return paths

    async def _download_single(
        self,
        url: str,
        aes_key: str | None,
        media_dir: Path,
        prefix: str,
    ) -> str | None:
        """下载单个媒体文件，返回本地路径或 None。"""
        try:
            logger.debug("正在下载企业微信媒体: {}", url[:80])
            result = await asyncio.wait_for(
                self._ws_client.download_file(url, aes_key),
                timeout=60.0,
            )
            buffer: bytes = result["buffer"]
            filename: str | None = result.get("filename")

            if not filename:
                ext = _guess_extension(buffer)
                filename = f"{prefix}_{uuid.uuid4().hex[:12]}{ext}"

            file_path = media_dir / filename
            await asyncio.to_thread(file_path.write_bytes, buffer)
            logger.debug("企业微信媒体已保存: {} ({} bytes)", file_path, len(buffer))
            return str(file_path)
        except asyncio.TimeoutError:
            logger.error("下载企业微信媒体超时: {}", url[:80])
            return None
        except Exception as e:
            logger.error("下载企业微信媒体失败: {} - {}", url[:80], e)
            return None


def _guess_extension(data: bytes) -> str:
    """根据文件头魔数猜测扩展名。"""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"GIF8":
        return ".gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[:4] == b"%PDF":
        return ".pdf"
    if data[:2] == b"PK":
        return ".zip"
    return ".bin"
