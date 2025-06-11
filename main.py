import logging
import ssl
import aiohttp
import asyncio
import random
import os
from typing import List, Dict, Set
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.types import Message, ChatMemberOwner, ChatMemberAdministrator, ChatMember, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.filters import Command
from aiogram.enums import ParseMode, ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.storage.memory import MemoryStorage

# Загрузка переменных окружения
load_dotenv("misc.env")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
COHERE_API_KEY = os.getenv("COHERE_API_KEY")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Инициализация
bot = Bot(token=TELEGRAM_TOKEN)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)

# Хранилище истории чата и участников
user_histories = {}
chat_members: Dict[int, Set[int]] = {}
chat_admins: Dict[int, Set[int]] = {}

# Файл для хранения участников
participants_file = "participants.txt"


class Form(StatesGroup):
    gpt_input = State()


def save_participant(chat_id: int,
                     user_id: int,
                     username: str = None,
                     first_name: str = None,
                     action: str = "register"):
    """Сохранить участника в файл"""
    # Проверяем, есть ли уже такой пользователь в файле (любое действие)
    if os.path.exists(participants_file):
        with open(participants_file, 'r', encoding='utf-8') as f:
            existing_lines = f.readlines()
            for line in existing_lines:
                if f"Chat: {chat_id}, User: {user_id}" in line:
                    return  # Пользователь уже существует, не добавляем повторно

    with open(participants_file, 'a', encoding='utf-8') as f:
        user_info = f"@{username}" if username else first_name or f"User_{user_id}"
        f.write(
            f"Chat: {chat_id}, User: {user_id}, Name: {user_info}, Action: {action}\n"
        )


def load_participants_from_file(chat_id: int) -> List[int]:
    """Загрузить участников из файла для конкретного чата"""
    participants = []
    if os.path.exists(participants_file):
        with open(participants_file, 'r', encoding='utf-8') as f:
            for line in f:
                if f"Chat: {chat_id}" in line:
                    # Извлекаем user_id из строки формата "Chat: -1002629246104, User: 1693165490, Name: @RookingIt, Action: message"
                    parts = line.split(", ")
                    for part in parts:
                        if part.startswith("User: "):
                            user_id = int(part.replace("User: ", ""))
                            if user_id not in participants:
                                participants.append(user_id)
                            break
    return participants


async def get_chat_members(chat_id: int) -> List[int]:
    """Получить список участников чата"""
    try:
        members = []
        # Получаем количество участников через get_chat_members_count
        count = await bot.get_chat_member_count(chat_id)
        logging.info(f"Участников в чате {chat_id}: {count}")
        return members
    except Exception as e:
        logging.error(f"Ошибка получения участников: {e}")
        return []


async def update_chat_members(chat_id: int):
    """Обновить кэш участников чата"""
    members = await get_chat_members(chat_id)
    chat_members[chat_id] = set(members)
    # Получить админов
    try:
        admins = await bot.get_chat_administrators(chat_id)
        admin_ids = []
        for admin in admins:
            if not admin.user.is_bot:
                admin_ids.append(admin.user.id)
        chat_admins[chat_id] = set(admin_ids)
        logging.info(f"Найдено {len(admin_ids)} админов в чате {chat_id}")
    except Exception as e:
        logging.error(f"Ошибка получения админов: {e}")
        chat_admins[chat_id] = set()


async def get_user_mention(user_id: int, chat_id: int) -> str:
    """Получить упоминание пользователя"""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        user = member.user
        if user.username:
            # Экранируем специальные символы для Markdown
            username = user.username.replace('_', '\\_').replace(
                '*', '\\*').replace('[', '\\[').replace(']', '\\]').replace(
                    '(',
                    '\\(').replace(')', '\\)').replace('~', '\\~').replace(
                        '`',
                        '\\`').replace('>', '\\>').replace('#', '\\#').replace(
                            '+', '\\+').replace('-', '\\-').replace(
                                '=', '\\=').replace('|', '\\|').replace(
                                    '{', '\\{').replace('}', '\\}').replace(
                                        '.', '\\.').replace('!', '\\!')
            return f"@{username}"
        else:
            # Экранируем имя для Markdown
            first_name = user.first_name.replace('_', '\\_').replace(
                '*', '\\*').replace('[', '\\[').replace(']', '\\]').replace(
                    '(',
                    '\\(').replace(')', '\\)').replace('~', '\\~').replace(
                        '`',
                        '\\`').replace('>', '\\>').replace('#', '\\#').replace(
                            '+', '\\+').replace('-', '\\-').replace(
                                '=', '\\=').replace('|', '\\|').replace(
                                    '{', '\\{').replace('}', '\\}').replace(
                                        '.', '\\.').replace('!', '\\!')
            return f"[{first_name}](tg://user?id={user_id})"
    except Exception:
        return f"[User {user_id}](tg://user?id={user_id})"


def is_group_chat(message: Message) -> bool:
    """Проверить, является ли чат групповым"""
    return message.chat.type in [ChatType.GROUP, ChatType.SUPERGROUP]


async def log_user_activity(message: Message, action: str = "message"):
    """Логировать активность пользователя"""
    if message.from_user and is_group_chat(message):
        chat_id = message.chat.id
        user_id = message.from_user.id
        user = message.from_user

        # Добавляем в кэш
        if chat_id not in chat_members:
            chat_members[chat_id] = set()
        chat_members[chat_id].add(user_id)

        # Сохраняем в файл
        save_participant(chat_id, user_id, user.username, user.first_name,
                         action)


async def ask_cohere(user_id: int, prompt: str):
    """Запрос к Cohere API"""
    url = "https://api.cohere.ai/v1/chat"
    headers = {
        "Authorization": f"Bearer {COHERE_API_KEY}",
        "Content-Type": "application/json"
    }

    history = user_histories.get(user_id, [])
    history.append({"role": "USER", "message": prompt})

    payload = {
        "model":
        "command-r-plus",
        "message":
        prompt,
        "chat_history":
        history,
        "preamble":
        ("Ты — Коралл, умный и дружелюбный групповой бот. Ты помогаешь участникам группы, "
         "отвечаешь на вопросы, развлекаешь и создаёшь позитивную атмосферу. "
         "Ты говоришь живо, с юмором, но всегда вежливо и конструктивно. "
         "Отвечай коротко и по делу 🐙")
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, headers=headers,
                                    json=payload) as resp:
                if resp.status != 200:
                    return f"❌ Ошибка AI: {resp.status}"
                result = await resp.json()
                reply = result.get("text", "(пустой ответ)")
                history.append({"role": "CHATBOT", "message": reply})
                user_histories[user_id] = history[-10:]
                return reply
    except Exception as e:
        return f"💥 Ошибка при запросе: {e}"


@dp.message()
async def handle_message(message: Message, state: FSMContext):
    if not message.text:
        # Логируем любую активность (стикеры, фото и т.д.)
        await log_user_activity(message, "media")
        return

    text = message.text.lower().strip()

    # Логируем сообщение
    await log_user_activity(message, "message")

    if is_group_chat(message):
        chat_id = message.chat.id
        if chat_id not in chat_members:
            await update_chat_members(chat_id)

    # Команды без слэша
    if text.startswith("коралл") or text.startswith("coral"):
        prompt = message.text[6:].strip() if text.startswith(
            "коралл") else message.text[5:].strip()
        if prompt:
            response = await ask_cohere(message.from_user.id, prompt)
            await message.answer(response, parse_mode=ParseMode.MARKDOWN)
        else:
            await message.answer("🐙 Коралл слушает! О чём хочешь поговорить?")

    elif text in ["пинг", "ping"]:
        ping_responses = [
            "🏓 Понг! Коралл на связи!", "🎯 Попал! Я здесь!",
            "⚡ Молниеносно отвечаю!", "🚀 Коралл в деле!", "💫 Как дела? Я тут!",
            "🌊 Плещусь в чате!", "🐙 Щупальца готовы к работе!"
        ]
        await message.answer(random.choice(ping_responses))

    elif text in ["помощь", "команды", "help"]:
        help_text = """🐙 **Команды Коралла:**

**Общение:**
• коралл [вопрос] — поговорить с ИИ
• пинг — проверить бота

**Групповые команды:**
• шип — выбрать милую парочку
• предсказание — получить предсказание
• миссия — получить тайное задание
• участие — добавиться в список участников
• цитата — мудрая цитата
• факт — интересный факт
• комплимент — случайный комплимент
• мотивация — мотивирующая фраза
• викторина — случайный вопрос
• челлендж — испытание дня

**Развлечения:**
• гороскоп — предсказания по знакам зодиака
• рецепт — кулинарные рецепты
• игра — интерактивные игры
• загадка — загадки для размышлений
• история — интересные исторические факты
• покер — случайная карта
• монетка — подбросить монетку
• кубик — бросить кости

**Инфо:**
• статистика — статистика группы
• админы — список админов"""
        await message.answer(help_text, parse_mode=ParseMode.MARKDOWN)

    elif text == "участие":
        if not is_group_chat(message):
            await message.answer("🐙 Эта команда работает только в группах!")
            return

        keyboard = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="Нажмите на кнопку, чтобы добавиться в список участников",
                callback_data=
                f"register_{message.chat.id}_{message.from_user.id}")
        ]])

        await message.answer("🐙 Хотите участвовать в активностях группы?",
                             reply_markup=keyboard)

    elif text == "шип":
        if not is_group_chat(message):
            await message.answer("🐙 Эта команда работает только в группах!")
            return

        # Загружаем участников из файла
        file_participants = load_participants_from_file(message.chat.id)

        # Объединяем с участниками из кэша
        cache_participants = list(chat_members.get(message.chat.id, set()))
        all_participants = list(set(file_participants + cache_participants))

        # Если нет участников, добавляем текущего пользователя
        if not all_participants:
            if message.chat.id not in chat_members:
                chat_members[message.chat.id] = set()
            chat_members[message.chat.id].add(message.from_user.id)
            all_participants = [message.from_user.id]

        if len(all_participants) < 2:
            await message.answer(
                "🐙 В группе слишком мало участников для выбора! Нужно минимум 2 участника."
            )
            return

        pair = random.sample(all_participants, 2)
        mention1 = await get_user_mention(pair[0], message.chat.id)
        mention2 = await get_user_mention(pair[1], message.chat.id)
        wishes = [
            "Желаем вам счастья и любви!",
            "Пусть ваша дружба крепнет с каждым днём!",
            "Любите и поддерживайте друг друга!",
            "Пусть ваши дни будут полны радости и понимания!",
            "Всегда оставайтесь рядом и цените моменты вместе!",
            "Пусть ваша связь будет крепкой как кораллы!",
            "Вместе вы непобедимы!",
            "Пусть каждый день приносит новые приключения!",
            "Ваша дружба — настоящее сокровище!",
            "Пусть смех и радость не покидают вас!"
        ]
        wish = random.choice(wishes)
        await message.answer(f"💕 Пмисиарочка: {mention1} и {mention2}. {wish}",
                             parse_mode=ParseMode.MARKDOWN)

    elif text == "предсказание":
        predictions = [
            "🔮 Не бойся менять жизнь!", "✨ Что-то хорошее произойдёт скоро.",
            "🌟 Ты на правильном пути.", "🍀 Удача улыбнётся тебе сегодня.",
            "🎯 Твои мечты ближе, чем кажется.",
            "🌈 После дождичка в четверг будет радуга.",
            "💎 Ты найдёшь то, что давно искал.",
            "🚀 Впереди тебя ждут новые возможности.",
            "🎪 Жизнь готовит тебе приятный сюрприз.",
            "🌸 Твоя доброта вернётся к тебе сторицей."
        ]
        await message.answer(random.choice(predictions))

    elif text == "миссия":
        tasks = [
            "🎯 Скажи 'банан' в разговоре незаметно.",
            "🤝 Отправь сообщение дружелюбно кому-то.",
            "💝 Сделай комплимент участнику.", "📚 Поделись интересным фактом.",
            "🎵 Напой песню (текстом).", "🤔 Задай философский вопрос.",
            "🎭 Расскажи смешную историю.", "🌟 Поблагодари кого-то за что-то.",
            "🎨 Опиши свой идеальный день.", "🚀 Поделись своей мечтой."
        ]
        mission = random.choice(tasks)
        await message.answer(f"🎯 Твоя миссия: {mission}")

    elif text == "цитата":
        quotes = [
            "💫 'Будь собой — все остальные роли уже заняты.' — Оскар Уайльд",
            "🌟 'Жизнь — это то, что происходит, пока ты строишь планы.' — Джон Леннон",
            "🎯 'Единственный способ делать отличную работу — любить то, что делаешь.' — Стив Джобс",
            "🌈 'Счастье — это не цель, а побочный продукт жизни.' — Элеонор Рузвельт",
            "🚀 'Будущее принадлежит тем, кто верит в красоту своих мечтаний.' — Элеонор Рузвельт",
            "💎 'Не ждите особого случая — каждый день особенный.' — Неизвестный автор",
            "🌸 'Улыбка — это кривая, которая всё выпрямляет.' — Филлис Диллер",
            "⭐ 'Начинайте там, где вы есть. Используйте то, что у вас есть. Делайте то, что можете.' — Артур Эш"
        ]
        await message.answer(random.choice(quotes))

    elif text == "факт":
        facts = [
            "🐙 Осьминоги имеют три сердца и голубую кровь!",
            "🍯 Мёд никогда не портится — археологи находили съедобный мёд возрастом 3000 лет!",
            "🌙 На Луне твой вес был бы в 6 раз меньше!",
            "🐧 Пингвины могут прыгать на высоту до 2 метров!",
            "🌊 В океане больше артефактов истории, чем во всех музеях мира!",
            "🧠 Человеческий мозг использует 20% всей энергии тела!",
            "🦋 Бабочки пробуют еду лапками!",
            "🌍 Банан — это ягода, а клубника — нет!",
            "⚡ Молния в 5 раз горячее поверхности Солнца!",
            "🐨 Коалы спят 22 часа в сутки!"
        ]
        await message.answer(random.choice(facts))

    elif text == "комплимент":
        compliments = [
            "✨ Ты освещаешь этот чат своим присутствием!",
            "🌟 У тебя потрясающее чувство юмора!",
            "💫 Ты делаешь мир лучше просто тем, что есть!",
            "🎨 Твоя креативность вдохновляет!",
            "🌈 Ты как радуга после дождя — приносишь радость!",
            "💎 Ты ценнее любых драгоценностей!",
            "🚀 Твоя энергия заразительна в лучшем смысле!",
            "🌸 Ты как весенний цветок — приносишь красоту в жизнь!",
            "⭐ Ты звезда этого чата!", "🎵 Твой голос важен и нужен!"
        ]
        await message.answer(random.choice(compliments))

    elif text == "мотивация":
        motivations = [
            "💪 Ты сильнее, чем думаешь!",
            "🎯 Каждый маленький шаг ведёт к большой цели!",
            "🌟 Твои возможности безграничны!",
            "🚀 Сегодня отличный день для новых достижений!",
            "💫 Ты уже на пути к успеху!",
            "🏆 Победа начинается с первого шага!",
            "🌈 После каждой бури выходит солнце!",
            "💎 Ты создан для великих дел!", "⚡ В тебе есть сила изменить мир!",
            "🌸 Верь в себя — это первый шаг к успеху!"
        ]
        await message.answer(random.choice(motivations))

    elif text == "викторина":
        questions = [
            "🤔 Какой цвет получится, если смешать красный и синий?",
            "🌍 Какая самая высокая гора в мире?",
            "🐧 Где живут пингвины — на Северном или Южном полюсе?",
            "🌙 Сколько спутников у Земли?",
            "🍯 Что производят пчёлы кроме мёда?",
            "🌊 Какой океан самый большой?", "🦕 В какую эпоху жили динозавры?",
            "🌟 Какая звезда ближайшая к Земле?",
            "🏛️ В какой стране находится Тадж-Махал?",
            "🎵 Сколько струн у классической гитары?"
        ]
        await message.answer(random.choice(questions))

    elif text == "челлендж":
        challenges = [
            "📱 Час без телефона — сможешь?",
            "💧 Выпей 8 стаканов воды сегодня!",
            "📚 Прочитай 10 страниц любой книги.",
            "🚶 Пройди 10000 шагов сегодня!", "🧘 Помедитируй 5 минут.",
            "📞 Позвони старому другу.", "🎨 Нарисуй что-нибудь за 5 минут.",
            "🌱 Посади семечко или полей растение.",
            "📝 Напиши список из 10 вещей, за которые благодарен.",
            "🎵 Выучи слова новой песни."
        ]
        challenge = random.choice(challenges)
        await message.answer(f"🏆 Челлендж дня: {challenge}")

    elif text == "гороскоп":
        # Сначала выбираем случайный знак зодиака
        signs = [{
            "sign":
            "♈ Овен",
            "predictions": [
                "Сегодня ваша энергия на пике! Отличное время для новых начинаний.",
                "Марс дарит вам силу и уверенность. Действуйте решительно!",
                "Ваша импульсивность сегодня сыграет вам на руку.",
                "Лидерские качества помогут вам достичь цели."
            ]
        }, {
            "sign":
            "♉ Телец",
            "predictions": [
                "Стабильность и терпение — ваши союзники сегодня.",
                "Венера благословляет ваши отношения и финансы.",
                "Не торопитесь — медленно, но верно к успеху.",
                "Ваша практичность принесёт материальную выгоду."
            ]
        }, {
            "sign":
            "♊ Близнецы",
            "predictions": [
                "День полон интересных встреч и неожиданных открытий.",
                "Меркурий усиливает вашу коммуникабельность.",
                "Новая информация откроет перспективы.",
                "Ваше остроумие очарует окружающих."
            ]
        }, {
            "sign":
            "♋ Рак",
            "predictions": [
                "Доверьтесь своей интуиции — она не подведёт.",
                "Луна усиливает ваши эмоции и чувствительность.",
                "Семейные дела требуют внимания.",
                "Забота о близких принесёт радость."
            ]
        }, {
            "sign":
            "♌ Лев",
            "predictions": [
                "Ваш шарм и харизма сегодня особенно заметны!",
                "Солнце освещает путь к славе и признанию.",
                "Творческие проекты получат одобрение.",
                "Ваша щедрость будет вознаграждена."
            ]
        }, {
            "sign":
            "♍ Дева",
            "predictions": [
                "Внимание к деталям принесёт успех в делах.",
                "Меркурий помогает в анализе и планировании.",
                "Организованность — ваше преимущество.",
                "Здоровье требует заботы и внимания."
            ]
        }, {
            "sign":
            "♎ Весы",
            "predictions": [
                "Гармония и баланс — ключ к решению проблем.",
                "Венера дарит красоту и эстетическое наслаждение.",
                "Партнёрские отношения на подъёме.",
                "Справедливость восторжествует в ваших делах."
            ]
        }, {
            "sign":
            "♏ Скорпион",
            "predictions": [
                "Глубокие размышления приведут к важным выводам.",
                "Плутон раскрывает скрытые тайны.",
                "Ваша проницательность поразит других.",
                "Трансформации принесут обновление."
            ]
        }, {
            "sign":
            "♐ Стрелец",
            "predictions": [
                "Приключения и новые горизонты ждут вас!",
                "Юпитер расширяет ваши возможности.",
                "Путешествия или обучение принесут пользу.",
                "Ваш оптимизм заразителен."
            ]
        }, {
            "sign":
            "♑ Козерог",
            "predictions": [
                "Упорство и дисциплина приведут к цели.",
                "Сатурн учит терпению и мудрости.",
                "Карьерные перспективы улучшаются.", "Ваш авторитет растёт."
            ]
        }, {
            "sign":
            "♒ Водолей",
            "predictions": [
                "Ваши оригинальные идеи найдут понимание.",
                "Уран приносит неожиданные возможности.",
                "Дружба и сотрудничество важны сегодня.",
                "Будущее начинается прямо сейчас."
            ]
        }, {
            "sign":
            "♓ Рыбы",
            "predictions": [
                "Творчество и мечты вдохновят на новые свершения.",
                "Нептун усиливает интуицию и воображение.",
                "Сострадание откроет новые возможности.",
                "Искусство и музыка принесут гармонию."
            ]
        }]

        # Выбираем случайный знак зодиака
        chosen_sign = random.choice(signs)
        # Выбираем случайное предсказание для этого знака
        prediction = random.choice(chosen_sign["predictions"])

        await message.answer(f"{chosen_sign['sign']}: {prediction}")

    elif text == "рецепт":
        recipes = [
            "🍝 Паста Карбонара: спагетти + яйца + бекон + сыр пармезан + чёрный перец",
            "🥗 Греческий салат: помидоры + огурцы + фета + оливки + оливковое масло",
            "🍲 Борщ: свёкла + капуста + морковь + лук + мясо + сметана",
            "🥪 Авокадо тост: хлеб + авокадо + лимон + соль + перец",
            "🍛 Плов: рис + мясо + морковь + лук + специи",
            "🥞 Блинчики: мука + молоко + яйца + сахар + соль",
            "🍕 Пицца Маргарита: тесто + томатный соус + моцарелла + базилик",
            "🍜 Рамен: лапша + бульон + яйцо + зелёный лук + нори",
            "🧀 Сырники: творог + яйцо + мука + сахар + сметана",
            "🥙 Шаурма: лаваш + мясо + овощи + соус"
        ]
        recipe = random.choice(recipes)
        await message.answer(f"👨‍🍳 Рецепт дня:\n{recipe}")

    elif text == "игра":
        games = [
            "🎲 Игра 'Угадай число': Я загадал число от 1 до 100. Попробуй угадать!",
            "🎯 Игра 'Правда или ложь': Коралл имеет 8 щупалец — правда или ложь?",
            "🧩 Игра '20 вопросов': Загадай предмет, а я попробую угадать за 20 вопросов!",
            "🎪 Игра 'Ассоциации': Слово 'море' — какая первая ассоциация?",
            "🎭 Игра 'Рифма': Придумай рифму к слову 'коралл'!",
            "🎨 Игра 'Описание': Опиши смайлик только словами: 🐙",
            "🔤 Игра 'Последняя буква': Город на букву 'М'!",
            "🎵 Игра 'Песня': Допой строчку: 'В лесу родилась...'",
            "🌍 Игра 'География': Назови страну на букву 'И'!",
            "🎬 Игра 'Фильм': Угадай фильм по описанию: 'Рыба-клоун ищет сына'"
        ]
        game = random.choice(games)
        await message.answer(f"🎮 {game}")

    elif text == "загадка":
        riddles = [
            "🤔 Что можно увидеть с закрытыми глазами? (Ответ: сон)",
            "🏠 В доме его нет, а на улице есть. Что это? (Ответ: буква 'У')",
            "⏰ Что становится больше, если поставить вверх ногами? (Ответ: число 6)",
            "🌊 Без рук, без ног, а гору разрушает. Что это? (Ответ: вода)",
            "🔥 Красный петушок по жердочке бежит. Что это? (Ответ: огонь)",
            "❄️ Зимой и летом одним цветом. Что это? (Ответ: ёлка)",
            "🌙 Что идёт, не двигаясь с места? (Ответ: время)",
            "🎯 У него есть шляпа, но нет головы. Что это? (Ответ: гриб)",
            "🍯 Не мёд, а липнет. Что это? (Ответ: клей)",
            "📚 Кто говорит на всех языках? (Ответ: эхо)"
        ]
        riddle = random.choice(riddles)
        await message.answer(f"🧩 {riddle}")

    elif text == "история":
        stories = [
            "📚 В 1912 году титаник затонул, но история о героизме оркестра, игравшего до конца, стала легендой.",
            "🏺 Клеопатра жила ближе по времени к высадке на Луну, чем к строительству пирамид!",
            "🎨 Ван Гог продал за всю жизнь только одну картину — 'Красные виноградники'.",
            "🐘 Наполеон боялся... котов! У великого полководца была айлурофобия.",
            "📡 Факс был изобретён в 1843 году — до изобретения телефона!",
            "🗽 Статуя Свободы изначально была коричневой, но окислилась до зелёного цвета.",
            "🦖 Динозавры жили на Земле 165 миллионов лет, а люди — всего 300 тысяч.",
            "🍫 Шоколад когда-то использовался как валюта ацтеками и майя.",
            "📖 Шекспир изобрёл более 1700 слов, которые мы используем до сих пор.",
            "🚀 Нил Армстронг оставил на Луне сумку с мусором — она там до сих пор!"
        ]
        story = random.choice(stories)
        await message.answer(f"📜 {story}")

    elif text == "покер":
        cards = [
            "🂡", "🂢", "🂣", "🂤", "🂥", "🂦", "🂧", "🂨", "🂩", "🂪", "🂫", "🂭", "🂮",
            "🂱", "🂲", "🂳", "🂴", "🂵", "🂶", "🂷", "🂸", "🂹", "🂺", "🂻", "🂽", "🂾",
            "🃁", "🃂", "🃃", "🃄", "🃅", "🃆", "🃇", "🃈", "🃉", "🃊", "🃋", "🃍", "🃎",
            "🃑", "🃒", "🃓", "🃔", "🃕", "🃖", "🃗", "🃘", "🃙", "🃚", "🃛", "🃝", "🃞"
        ]
        card_names = [
            "Туз пик", "Двойка пик", "Тройка пик", "Четвёрка пик",
            "Пятёрка пик", "Шестёрка пик", "Семёрка пик", "Восьмёрка пик",
            "Девятка пик", "Десятка пик", "Валет пик", "Дама пик",
            "Король пик", "Туз червей", "Двойка червей", "Тройка червей",
            "Четвёрка червей", "Пятёрка червей", "Шестёрка червей",
            "Семёрка червей", "Восьмёрка червей", "Девятка червей",
            "Десятка червей", "Валет червей", "Дама червей", "Король червей",
            "Туз бубей", "Двойка бубей", "Тройка бубей", "Четвёрка бубей",
            "Пятёрка бубей", "Шестёрка бубей", "Семёрка бубей",
            "Восьмёрка бубей", "Девятка бубей", "Десятка бубей", "Валет бубей",
            "Дама бубей", "Король бубей", "Туз треф", "Двойка треф",
            "Тройка треф", "Четвёрка треф", "Пятёрка треф", "Шестёрка треф",
            "Семёрка треф", "Восьмёрка треф", "Девятка треф", "Десятка треф",
            "Валет треф", "Дама треф", "Король треф"
        ]
        card_index = random.randint(0, len(cards) - 1)
        card = cards[card_index]
        card_name = card_names[card_index]
        await message.answer(f"🎰 Ваша карта: {card} {card_name}")

    elif text == "монетка":
        coin_results = ["🪙 Орёл!", "🪙 Решка!"]
        result = random.choice(coin_results)
        await message.answer(f"🎯 {result}")

    elif text == "кубик":
        dice_faces = ["⚀", "⚁", "⚂", "⚃", "⚄", "⚅"]
        numbers = ["1", "2", "3", "4", "5", "6"]
        dice_index = random.randint(0, 5)
        dice_face = dice_faces[dice_index]
        number = numbers[dice_index]
        await message.answer(f"🎲 Выпало: {dice_face} ({number})")

    elif text == "статистика":
        if not is_group_chat(message):
            await message.answer("🐙 Эта команда работает только в группах!")
            return

        await update_chat_members(message.chat.id)

        # Получаем участников из файла и кэша
        file_participants = load_participants_from_file(message.chat.id)
        cache_participants = list(chat_members.get(message.chat.id, set()))
        all_participants = list(set(file_participants + cache_participants))

        admins_count = len(chat_admins.get(message.chat.id, set()))

        stats = f"""📊 **Статистика группы:**

👥 Всего участников: {len(all_participants)}
📝 Участников в файле: {len(file_participants)}
💬 Активных в кэше: {len(cache_participants)}
👑 Админов: {admins_count}
🐙 Коралл активен и готов помочь!"""

        await message.answer(stats, parse_mode=ParseMode.MARKDOWN)

    elif text == "админы":
        if not is_group_chat(message):
            await message.answer("🐙 Эта команда работает только в группах!")
            return

        await update_chat_members(message.chat.id)
        admin_ids = chat_admins.get(message.chat.id, set())

        if not admin_ids:
            await message.answer("🤷 Не удалось получить список админов")
            return

        admin_mentions = []
        for admin_id in admin_ids:
            mention = await get_user_mention(admin_id, message.chat.id)
            admin_mentions.append(mention)

        admins_text = "\n".join([f"👑 {mention}" for mention in admin_mentions])
        await message.answer(f"**Администраторы группы:**\n\n{admins_text}",
                             parse_mode=ParseMode.MARKDOWN)


@dp.callback_query()
async def handle_registration(callback: CallbackQuery):
    """Обработчик регистрации участников"""
    if callback.data.startswith("register_"):
        data_parts = callback.data.split("_")
        chat_id = int(data_parts[1])
        user_id = int(data_parts[2])

        # Проверяем, зарегистрирован ли уже пользователь
        already_registered = False
        if os.path.exists(participants_file):
            with open(participants_file, 'r', encoding='utf-8') as f:
                for line in f:
                    if f"Chat: {chat_id}, User: {user_id}" in line and "Action: button_register" in line:
                        already_registered = True
                        break

        if already_registered:
            await callback.answer("ℹ️ Вы уже зарегистрированы в этой группе!")
            return

        # Добавляем пользователя в кэш участников
        if chat_id not in chat_members:
            chat_members[chat_id] = set()

        chat_members[chat_id].add(user_id)

        # Сохраняем в файл
        user = callback.from_user
        save_participant(chat_id, user_id, user.username, user.first_name,
                         "button_register")

        await callback.answer("✅ Вы успешно добавлены в список участников!")

        # Обновляем сообщение
        user_mention = f"@{user.username}" if user.username else user.first_name
        await callback.message.edit_text(
            f"🎉 {user_mention} добавлен(а) в список участников группы!")


# Обработчик реакций на сообщения
@dp.message_reaction()
async def handle_reaction(reaction_update):
    """Обработчик реакций"""
    if hasattr(reaction_update, 'user') and reaction_update.user:
        # Сохраняем реакцию как активность
        save_participant(reaction_update.chat.id, reaction_update.user.id,
                         getattr(reaction_update.user, 'username', None),
                         getattr(reaction_update.user, 'first_name', None),
                         "reaction")


# Обработчики событий группы - логирование активности для обновления списка участников
@dp.message()
async def log_activity(message: Message):
    # Эта функция уже включена в handle_message
    pass


# Старые slash команды для совместимости
@dp.message(Command("start"))
async def start_cmd(message: Message):
    start_messages = [
        "🐙 Привет! Я Коралл — твой групповой помощник!\n\nНапиши 'помощь' чтобы узнать мои команды",
        "🌊 Приветствую! Коралл к вашим услугам!\n\nИспользуй 'команды' для списка возможностей",
        "🚀 Добро пожаловать! Я готов помочь!\n\nНабери 'help' для инструкций"
    ]
    await message.answer(random.choice(start_messages))


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await handle_message(message, None)


# Запуск
async def main():
    logging.basicConfig(level=logging.INFO)

    # Получить информацию о боте
    me = await bot.get_me()
    print(f"✅ Бот @{me.username} успешно авторизован!")

    print("🔄 Начинаю polling...")

    import signal

    def signal_handler(signum, frame):
        print("Received SIGTERM signal")
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, signal_handler)

    try:
        await dp.start_polling(bot)
    except KeyboardInterrupt:
        print("🛑 Бот остановлен")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
