from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.filters.command import Command, CommandStart
from aiogram.types import Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from func.interactions import *
from difflib import SequenceMatcher
import asyncio
import traceback
import io
import base64

bot = Bot(token=token)
dp = Dispatcher()
start_kb = InlineKeyboardBuilder()
settings_kb = InlineKeyboardBuilder()
start_kb.row(
    types.InlineKeyboardButton(text="ℹ️ About", callback_data="about"),
    types.InlineKeyboardButton(text="⚙️ Settings", callback_data="settings"),
)
settings_kb.row(
    types.InlineKeyboardButton(text="🔄 Switch LLM", callback_data="switchllm"),
    types.InlineKeyboardButton(text="✏️ Edit system prompt", callback_data="editsystemprompt"),
)

commands = [
    types.BotCommand(command="start", description="Start"),
    types.BotCommand(command="reset", description="Reset Chat"),
    types.BotCommand(command="history", description="Look through messages"),
]
ACTIVE_CHATS = {}
ACTIVE_CHATS_LOCK = contextLock()
modelname = os.getenv("INITMODEL")
mention = None
CHAT_TYPE_GROUP = "group"
CHAT_TYPE_SUPERGROUP = "supergroup"
USER_ANSWERS_DICT = {}
USER_ANSWERS_LOCK = asyncio.Lock()


def is_mentioned_in_group_or_supergroup(message):
    return message.chat.type in [CHAT_TYPE_GROUP, CHAT_TYPE_SUPERGROUP] and (
        (message.text is not None and message.text.startswith(mention))
        or (message.caption is not None and message.caption.startswith(mention))
    )


def is_similar(question1, question2, threshold=0.8):
    return SequenceMatcher(None, question1, question2).ratio() > threshold


async def get_bot_info():
    global mention
    if mention is None:
        get = await bot.get_me()
        mention = f"@{get.username}"
    return mention


@dp.message(CommandStart())
async def command_start_handler(message: Message) -> None:
    start_message = f"Welcome, <b>{message.from_user.full_name}</b>!"
    await message.answer(
        start_message,
        parse_mode=ParseMode.HTML,
        reply_markup=start_kb.as_markup(),
        disable_web_page_preview=True,
    )


@dp.message(Command("reset"))
async def command_reset_handler(message: Message) -> None:
    if message.from_user.id in allowed_ids:
        if message.from_user.id in ACTIVE_CHATS:
            async with ACTIVE_CHATS_LOCK:
                ACTIVE_CHATS.pop(message.from_user.id)
            logging.info(f"\033[92mChat has been reset for {message.from_user.first_name}\033[0m")
            await bot.send_message(
                chat_id=message.chat.id,
                text="Chat has been reset",
            )


@dp.message(Command("history"))
async def command_get_context_handler(message: Message) -> None:
    if message.from_user.id in allowed_ids:
        if message.from_user.id in ACTIVE_CHATS:
            messages = ACTIVE_CHATS.get(message.chat.id)["messages"]
            context = ""
            for msg in messages:
                context += f"*{msg['role'].capitalize()}*: {msg['content']}\n"
            await bot.send_message(
                chat_id=message.chat.id,
                text=context,
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await bot.send_message(
                chat_id=message.chat.id,
                text="No chat history available for this user",
            )


@dp.callback_query(lambda query: query.data == "settings")
async def settings_callback_handler(query: types.CallbackQuery):
    await bot.send_message(
        chat_id=query.message.chat.id,
        text=f"Choose the right option.",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=settings_kb.as_markup(),
    )


@dp.callback_query(lambda query: query.data == "switchllm")
async def switchllm_callback_handler(query: types.CallbackQuery):
    models = await model_list()
    switchllm_builder = InlineKeyboardBuilder()
    for model in models:
        modelname = model["name"]
        modelfamilies = ""
        if model["details"]["families"]:
            modelicon = {"llama": "🦙", "clip": "📷"}
            try:
                modelfamilies = "".join([modelicon[family] for family in model["details"]["families"]])
            except KeyError as e:
                modelfamilies = f"✨"
        switchllm_builder.row(
            types.InlineKeyboardButton(text=f"{modelname} {modelfamilies}", callback_data=f"model_{modelname}")
        )
    await query.message.edit_text(
        f"{len(models)} models available.\n🦙 = Regular\n🦙📷 = Multimodal",
        reply_markup=switchllm_builder.as_markup(),
    )


@dp.callback_query(lambda query: query.data == "editsystemprompt")
async def editsystemprompt_callback_handler(query: types.CallbackQuery):
    await bot.send_message(
        chat_id=query.message.chat.id,
        text="Please enter a new system prompt for ollama model",
    )
    msg = await dp.bot.wait_for_message(chat_id=query.message.chat.id)
    prompt = msg.text
    await add_prompt_to_active_chats(msg, prompt, None, modelname)


@dp.callback_query(lambda query: query.data.startswith("model_"))
async def model_callback_handler(query: types.CallbackQuery):
    global modelname
    global modelfamily
    modelname = query.data.split("model_")[1]
    await query.answer(f"Chosen model: {modelname}")


@dp.callback_query(lambda query: query.data == "about")
@perms_admins
async def about_callback_handler(query: types.CallbackQuery):
    dotenv_model = os.getenv("INITMODEL")
    global modelname
    await bot.send_message(
        chat_id=query.message.chat.id,
        text=f"<b>Your LLMs</b>\nCurrently using: <code>{modelname}</code>\nDefault in .env: <code>{dotenv_model}</code>\nThis project is under <a href='https://github.com/ruecat/ollama-telegram/blob/main/LICENSE'>MIT License.</a>\n<a href='https://github.com/ruecat/ollama-telegram'>Source Code</a>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@dp.message()
@perms_allowed
async def handle_message(message: types.Message):
    await get_bot_info()
    chat_type = message.chat.type
    logging.info(f"\033[92m [INFO] {chat_type} \033[92m")

    if message.chat.type == "private":
        await ollama_request(message)
    elif message.chat.type == CHAT_TYPE_GROUP:
        if message.reply_to_message and message.reply_to_message.text:
            original_question = message.reply_to_message.text.strip()
            answer = message.text.strip()
            async with USER_ANSWERS_LOCK:
                USER_ANSWERS_DICT[original_question] = answer

            await bot.send_message(
                chat_id=message.chat.id,
                text=f"Ответ был успешно сохранен.\nВопрос: {original_question}",
            )
        elif message.text and message.text.endswith("?"):
            prompt = message.text.strip()
            prev_answer = ""

            async with USER_ANSWERS_LOCK:
                for question, answer in USER_ANSWERS_DICT.items():
                    if is_similar(question, prompt):
                        prev_answer = answer
                        break

            if prev_answer:
                res = await ollama_request_without_sending(message, prompt)

                text = f"{res.strip()}\n\n⚙️ {modelname}"

                await bot.send_message(
                    chat_id=message.chat.id,
                    text=f"Похожий вопрос: {prompt}\nОтвет: {prev_answer}\n\nСгенерированный ответ: {text}",
                )


async def process_image(message):
    image_base64 = ""
    if message.content_type == "photo":
        image_buffer = io.BytesIO()
        await bot.download(message.photo[-1], destination=image_buffer)
        image_base64 = base64.b64encode(image_buffer.getvalue()).decode("utf-8")
    return image_base64


async def add_prompt_to_active_chats(message, prompt, image_base64, modelname):
    async with ACTIVE_CHATS_LOCK:
        if ACTIVE_CHATS.get(message.from_user.id) is None:
            ACTIVE_CHATS[message.from_user.id] = {
                "model": modelname,
                "messages": [
                    {
                        "role": "user",
                        "content": prompt,
                        "images": ([image_base64] if image_base64 else []),
                    }
                ],
                "stream": True,
            }
        else:
            ACTIVE_CHATS[message.from_user.id]["messages"].append(
                {
                    "role": "user",
                    "content": prompt,
                    "images": ([image_base64] if image_base64 else []),
                }
            )


async def handle_response(message, response_data, full_response):
    full_response_stripped = full_response.strip()
    if full_response_stripped == "":
        return
    if response_data.get("done"):
        text = (
            f"{full_response_stripped}\n\n⚙️ {modelname}\nGenerated in {response_data.get('total_duration') / 1e9:.2f}s."
        )
        await send_response(message, text)
        async with ACTIVE_CHATS_LOCK:
            if ACTIVE_CHATS.get(message.from_user.id) is not None:
                ACTIVE_CHATS[message.from_user.id]["messages"].append(
                    {"role": "assistant", "content": full_response_stripped}
                )
        logging.info(
            f"\033[92m[Response]: '{full_response_stripped}' for {message.from_user.first_name} {message.from_user.last_name}\033[0m"
        )
        return True
    return False


async def send_response(message, text):
    if message.chat.id == message.from_user.id:
        await bot.send_message(chat_id=message.chat.id, text=text)
    else:
        await bot.send_message(chat_id=message.chat.id, text=text)
        # await bot.edit_message_text(chat_id=message.chat.id, message_id=message.message_id, text=text)


async def ollama_request(message: types.Message, prompt: str = None):
    try:
        full_response = ""
        await bot.send_chat_action(message.chat.id, "typing")
        image_base64 = await process_image(message)
        if prompt is None:
            prompt = message.text or message.caption

        await add_prompt_to_active_chats(message, prompt, image_base64, modelname)
        logging.info(
            f"\033[94m[OllamaAPI]: Processing '{prompt}' for {message.from_user.first_name} {message.from_user.last_name}\033[0m"
        )
        payload = ACTIVE_CHATS.get(message.from_user.id)
        async for response_data in generate(payload, modelname, prompt):
            msg = response_data.get("message")
            if msg is None:
                continue
            chunk = msg.get("content", "")
            full_response += chunk

            if any([c in chunk for c in ".\n!?"]) or response_data.get("done"):
                if await handle_response(message, response_data, full_response):
                    break

    except Exception as e:
        print(f"-----\n[OllamaAPI-ERR] CAUGHT FAULT!\n{traceback.format_exc()}\n-----")
        await bot.send_message(
            chat_id=message.chat.id,
            text=f"Something went wrong.",
            parse_mode=ParseMode.HTML,
        )


async def ollama_request_without_sending(message: types.Message, prompt: str = None):
    try:
        full_response = ""
        image_base64 = await process_image(message)
        if prompt is None:
            prompt = message.text or message.caption

        await add_prompt_to_active_chats(message, prompt, image_base64, modelname)
        logging.info(
            f"\033[94m[OllamaAPI]: Processing '{prompt}' for {message.from_user.first_name} {message.from_user.last_name}\033[0m"
        )
        payload = ACTIVE_CHATS.get(message.from_user.id)
        async for response_data in generate(payload, modelname, prompt):
            msg = response_data.get("message")
            if msg is None:
                continue
            chunk = msg.get("content", "")
            full_response += chunk

            if any([c in chunk for c in ".\n!?"]) or response_data.get("done"):
                break

        logging.info(
            f"\033[92m[Response]: '{full_response}' for {message.from_user.first_name} {message.from_user.last_name}\033[0m"
        )

        return full_response

    except Exception as e:
        print(f"-----\n[OllamaAPI-ERR] CAUGHT FAULT!\n{traceback.format_exc()}\n-----")
        await bot.send_message(
            chat_id=message.chat.id,
            text=f"Something went wrong.",
            parse_mode=ParseMode.HTML,
        )


async def main():
    await bot.set_my_commands(commands)
    await dp.start_polling(bot, skip_update=True)


if __name__ == "__main__":
    asyncio.run(main())
