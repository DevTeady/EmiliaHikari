import re
from typing import Optional, List

import telegram
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ParseMode
from telegram import Message, Chat, Update, Bot
from telegram.ext import CommandHandler, run_async, DispatcherHandlerStop, MessageHandler, Filters, CallbackQueryHandler
from telegram.utils.helpers import escape_markdown

from tg_bot import dispatcher, BAN_STICKER
from tg_bot.modules.disable import DisableAbleCommandHandler
from tg_bot.modules.helper_funcs.chat_status import is_user_admin, bot_admin, user_admin_no_reply, user_admin, \
    can_restrict
from tg_bot.modules.helper_funcs.extraction import extract_text, extract_user_and_text, extract_user
from tg_bot.modules.helper_funcs.misc import split_message
from tg_bot.modules.helper_funcs.string_handling import split_quotes
from tg_bot.modules.sql import warns_sql as sql

WARN_HANDLER_GROUP = 9
CURRENT_WARNING_FILTER_STRING = "*Current warning filters in this chat:*\n"


# Not async
def warn(user_id, chat, reason, bot, message):
    if is_user_admin(chat, user_id):
        message.reply_text("Damn admins, can't even be warned!")
        return

    limit, soft_warn = sql.get_warn_setting(chat.id)
    num_warns, reasons = sql.warn_user(user_id, chat.id, reason)
    if num_warns >= limit:
        if soft_warn:  # kick
            res = chat.unban_member(user_id)
        else:  # ban
            res = chat.kick_member(user_id)

        if res:
            bot.send_sticker(chat.id, BAN_STICKER)  # banhammer marie sticker
            message.reply_text("{} warnings, this user has been banned!".format(limit))
            sql.reset_warns(user_id, chat.id)
        else:
            message.reply_text("An error occurred, I couldn't ban this person!")
    else:
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Remove warn", callback_data="rm_warn({})".format(user_id))]])
        if reason:
            message.reply_text(
                "{}/{} warnings... watch out! Latest one was because:\n{}".format(num_warns, limit, reason),
                reply_markup=keyboard)
        else:
            message.reply_text("{}/{} warnings... watch out!".format(num_warns, limit),
                               reply_markup=keyboard)


@run_async
@user_admin_no_reply
@bot_admin
def button(bot: Bot, update: Update):
    query = update.callback_query
    match = re.match(r"rm_warn\((.+?)\)", query.data)
    if match:
        user_id = match.group(1)
        chat_id = update.effective_chat.id
        res = sql.remove_warn(user_id, chat_id)
        if res:
            update.effective_message.edit_text("Warn removed.")


@run_async
@user_admin
@can_restrict
def warn_user(bot: Bot, update: Update, args: List[str]):
    message = update.effective_message  # type: Optional[Message]
    chat = update.effective_chat  # type: Optional[Chat]

    user_id, reason = extract_user_and_text(message, args)

    if user_id:
        if message.reply_to_message.from_user.id == user_id:
            warn(user_id, chat, reason, bot, message.reply_to_message)
        else:
            warn(user_id, chat, reason, bot, message)
    else:
        message.reply_text("No user was designated!")


@run_async
@user_admin
@bot_admin
def reset_warns(bot: Bot, update: Update, args: List[str]):
    message = update.effective_message  # type: Optional[Message]
    chat = update.effective_chat  # type: Optional[Chat]

    user_id = extract_user(message, args)
    if user_id:
        sql.reset_warns(user_id, chat.id)
        message.reply_text("Warnings have been reset!")
    else:
        message.reply_text("No user has been designated!")


@run_async
def warns(bot: Bot, update: Update, args: List[str]):
    message = update.effective_message  # type: Optional[Message]
    chat = update.effective_chat  # type: Optional[Chat]
    user_id = extract_user(message, args) or update.effective_user.id
    result = sql.get_warns(user_id, chat.id)

    if result and result[0] != 0:
        num_warns, reasons = result
        limit, soft_warn = sql.get_warn_setting(chat.id)

        if reasons:
            text = "This user has {}/{} warnings, for the following reasons:".format(num_warns, limit)
            for reason in reasons:
                text += "\n - {}".format(reason)

            msgs = split_message(text)
            for msg in msgs:
                update.effective_message.reply_text(msg)
        else:
            update.effective_message.reply_text(
                "User has {}/{} warnings, but no reasons for any of them.".format(num_warns, limit))
    else:
        update.effective_message.reply_text("This user hasn't got any warnings!")


# Dispatcher handler stop - do not async
@user_admin
def add_warn_filter(bot: Bot, update: Update):
    chat = update.effective_chat  # type: Optional[Chat]
    msg = update.effective_message  # type: Optional[Message]

    args = msg.text.split(None, 1)  # use python's maxsplit to separate Cmd, keyword, and reply_text

    if len(args) < 2:
        return

    extracted = split_quotes(args[1])

    if len(extracted) >= 2:
        # set trigger -> lower, so as to avoid adding duplicate filters with different cases
        keyword = extracted[0].lower()
        content = extracted[1]

    else:
        return

    # Note: perhaps handlers can be removed somehow using sql.get_chat_filters
    for handler in dispatcher.handlers.get(WARN_HANDLER_GROUP, []):
        if handler.filters == (keyword, chat.id):
            dispatcher.remove_handler(handler, WARN_HANDLER_GROUP)

    sql.add_warn_filter(chat.id, keyword, content)

    update.effective_message.reply_text("Warn handler added for '{}'!".format(keyword))
    raise DispatcherHandlerStop


@user_admin
def remove_warn_filter(bot: Bot, update: Update, args: List[str]):
    chat = update.effective_chat  # type: Optional[Chat]

    if len(args) < 1:
        return

    chat_filters = sql.get_chat_warn_filters(chat.id)

    if not chat_filters:
        update.effective_message.reply_text("No warning filters are active here!")
        return

    for filt in chat_filters:
        if filt.chat_id == str(chat.id) and filt.keyword == args[0]:
            sql.remove_warn_filter(chat.id, args[0])
            update.effective_message.reply_text("Yep, I'll stop warning people for that.")
            raise DispatcherHandlerStop

    update.effective_message.reply_text("That's not a current warning filter - run /warnlist for all \
    active warning filters.")


@run_async
def list_warn_filters(bot: Bot, update: Update):
    chat = update.effective_chat  # type: Optional[Chat]
    all_handlers = sql.get_chat_warn_filters(chat.id)

    if not all_handlers:
        update.effective_message.reply_text("No warning filters are active here!")
        return

    filter_list = CURRENT_WARNING_FILTER_STRING
    for handler in all_handlers:
        entry = " - {}\n".format(escape_markdown(handler.keyword))
        if len(entry) + len(filter_list) > telegram.MAX_MESSAGE_LENGTH:
            update.effective_message.reply_text(filter_list, parse_mode=ParseMode.MARKDOWN)
            filter_list = entry
        else:
            filter_list += entry

    if not filter_list == CURRENT_WARNING_FILTER_STRING:
        update.effective_message.reply_text(filter_list, parse_mode=ParseMode.MARKDOWN)


@run_async
def reply_filter(bot: Bot, update: Update):
    chat_warn_filters = sql.get_chat_warn_filters(update.effective_chat.id)
    message = update.effective_message  # type: Optional[Message]
    to_match = extract_text(message)
    if not to_match:
        return

    for warn_filter in chat_warn_filters:
        pattern = r"( |^|[^\w])" + re.escape(warn_filter.keyword) + r"( |$|[^\w])"
        if re.search(pattern, to_match, flags=re.IGNORECASE):
            user_id = update.effective_user.id
            chat = update.effective_chat  # type: Optional[Chat]
            warn(user_id, chat, warn_filter.reply, bot, message)


@run_async
@user_admin
def set_warn_limit(bot: Bot, update: Update, args: List[str]):
    chat = update.effective_chat  # type: Optional[Chat]
    msg = update.effective_message  # type: Optional[Message]

    if args:
        if args[0].isdigit():
            if int(args[0]) < 3:
                msg.reply_text("The minimum warn limit is 3!")
            else:
                sql.set_warn_limit(chat.id, int(args[0]))
                msg.reply_text("Updated the warn limit to {}".format(args[0]))
        else:
            msg.reply_text("Give me a number as an arg!")
    else:
        limit, soft_warn = sql.get_warn_setting(chat.id)

        msg.reply_text("The current warn limit is {}".format(limit))


@run_async
@user_admin
def set_warn_strength(bot: Bot, update: Update, args: List[str]):
    chat = update.effective_chat  # type: Optional[Chat]
    msg = update.effective_message  # type: Optional[Message]

    if args:
        if args[0].lower() in ("on", "yes"):
            sql.set_warn_strength(chat.id, False)
            msg.reply_text("Too many warns will now result in a ban!")

        elif args[0].lower() in ("off", "no"):
            sql.set_warn_strength(chat.id, True)
            msg.reply_text("Too many warns will now result in a kick! Users will be able to join again after.")

        else:
            msg.reply_text("I only understand on/yes/no/off!")
    else:
        limit, soft_warn = sql.get_warn_setting(chat.id)
        if soft_warn:
            msg.reply_text("Warns are currently set to *kick* users when they exceed the limits.",
                           parse_mode=ParseMode.MARKDOWN)
        else:
            msg.reply_text("Warns are currently set to *ban* users when they exceed the limits.",
                           parse_mode=ParseMode.MARKDOWN)


def __stats__():
    return "{} overall warns, across {} chats.\n" \
           "{} warn filters, across {} chats.".format(sql.num_warns(), sql.num_warn_chats(),
                                                      sql.num_warn_filters(), sql.num_warn_filter_chats())


def __import_data__(chat_id, data):
    for user_id, count in data.get('warns', {}).items():
        for x in range(int(count)):
            sql.warn_user(user_id, chat_id)


def __migrate__(old_chat_id, new_chat_id):
    sql.migrate_chat(old_chat_id, new_chat_id)


def __chat_settings__(chat_id, user_id):
    num_warn_filters = sql.num_warn_chat_filters(chat_id)
    limit, soft_warn = sql.get_warn_setting(chat_id)
    return "This chat has `{}` warn filters. It takes `{}` warns " \
           "before the user gets *{}*.".format(num_warn_filters, limit, "kicked" if soft_warn else "banned")


__help__ = """
 - /warns <userhandle>: get a user's number, and reason, of warnings.
 - /warnlist: list of all current warning filters

*Admin only:*
 - /warn <userhandle>: warn a user. After 3 warns, the user will be banned from the group. Can also be used as a reply.
 - /resetwarn <userhandle>: reset the warnings for a user. Can also be used as a reply.
 - /addwarn <keyword> <reply message>: set a warning filter on a certain keyword. If you want your keyword to \
be a sentence, encompass it with quotes, as such: `/addwarn "very angry" This is an angry user`. 
 - /nowarn <keyword>: stop a warning filter
 - /warnlimit <num>: set the warning limit
 - /strongwarn <on/yes/off/no>: If set to on, exceeding the warn limit will result in a ban. Else, will just kick.
"""

__name__ = "Warnings"


WARN_HANDLER = CommandHandler("warn", warn_user, pass_args=True, filters=Filters.group)
RESET_WARN_HANDLER = CommandHandler("resetwarn", reset_warns, pass_args=True, filters=Filters.group)
CALLBACK_QUERY_HANDLER = CallbackQueryHandler(button, pattern=r"rm_warn")
MYWARNS_HANDLER = DisableAbleCommandHandler("warns", warns, pass_args=True, filters=Filters.group)
ADD_WARN_HANDLER = CommandHandler("addwarn", add_warn_filter, filters=Filters.group)
RM_WARN_HANDLER = CommandHandler("nowarn", remove_warn_filter, pass_args=True, filters=Filters.group)
LIST_WARN_HANDLER = DisableAbleCommandHandler("warnlist", list_warn_filters, filters=Filters.group)
WARN_FILTER_HANDLER = MessageHandler(Filters.text | Filters.command | Filters.sticker | Filters.photo | Filters.group,
                                     reply_filter)
WARN_LIMIT_HANDLER = CommandHandler("warnlimit", set_warn_limit, pass_args=True, filters=Filters.group)
WARN_STRENGTH_HANDLER = CommandHandler("strongwarn", set_warn_strength, pass_args=True, filters=Filters.group)


dispatcher.add_handler(WARN_HANDLER)
dispatcher.add_handler(CALLBACK_QUERY_HANDLER)
dispatcher.add_handler(RESET_WARN_HANDLER)
dispatcher.add_handler(MYWARNS_HANDLER)
dispatcher.add_handler(ADD_WARN_HANDLER)
dispatcher.add_handler(RM_WARN_HANDLER)
dispatcher.add_handler(LIST_WARN_HANDLER)
dispatcher.add_handler(WARN_LIMIT_HANDLER)
dispatcher.add_handler(WARN_STRENGTH_HANDLER)
dispatcher.add_handler(WARN_FILTER_HANDLER, WARN_HANDLER_GROUP)
