# encoding:utf-8
import os
import re
import io
import json
import base64
import pickle
import requests
from PIL import Image
from plugins import *
from lib import itchat
from lib.itchat.content import *
from bridge.reply import Reply, ReplyType
from config import conf
from common.log import logger

COMMANDS = {
    "mj_help": {
        "alias": ["mj_help", "mj帮助", "mj文档","mjhelp"],
        "desc": "mj帮助",
    },
    "mj_admin_cmd": {
        "alias": ["mj_admin_cmd", "mj管理员指令"],
        "desc": "mj管理员指令",
    },
    "mj_admin_password": {
        "alias": ["mj_admin_password", "mj管理员认证"],
        "args": ["口令"],
        "desc": "mj管理员认证",
    },
}


ADMIN_COMMANDS = {
    "g_info": {
        "alias": ["g_info", "查询用户信息"],
        "desc": "查询数据库中用户昵称和对应的剩余次数",
    },
    "stop_mj": {
        "alias": ["stop_mj", "暂停mj服务"],
        "desc": "暂停mj服务",
    },
    "enable_mj": {
        "alias": ["enable_mj", "启用mj服务"],
        "desc": "启用mj服务",
    },
    "set_mj_admin_password": {
        "alias": ["set_mj_admin_password", "设置管理员口令"],
        "args": ["口令"],
        "desc": "修改管理员口令",
    },
    "g_admin_list": {
        "alias": ["g_admin_list", "查询管理员列表"],
        "desc": "查询管理员列表",
    },
    "s_admin_list": {
        "alias": ["s_admin_list", "添加管理员"],
        "args": ["用户ID或昵称"],
        "desc": "添加管理员",
    },
    "r_admin_list": {
        "alias": ["r_admin_list", "移除管理员"],
        "args": ["用户ID或昵称或序列号"],
        "desc": "移除管理员",
    },
    "c_admin_list": {
        "alias": ["c_admin_list", "清空管理员"],
        "desc": "清空管理员",
    },
    "s_limit": {
        "alias": ["s_limit", "设置每日作图数限制"],
        "args": ["限制值"],
        "desc": "设置每日作图数限制",
    },
    "r_limit": {
        "alias": ["r_limit", "清空重置用户作图数限制"],
        "desc": "清空重置用户作图数限制",
    },
    "g_wgroup": {
        "alias": ["g_wgroup", "查询白名单群组"],
        "desc": "查询白名单群组",
    },
    "s_wgroup": {
        "alias": ["s_wgroup", "添加白名单群组"],
        "args": ["群组名称"],
        "desc": "添加白名单群组",
    },
    "r_wgroup": {
        "alias": ["r_wgroup", "移除白名单群组"],
        "args": ["群组名称或序列号"],
        "desc": "移除白名单群组",
    },
    "c_wgroup": {
        "alias": ["c_wgroup", "清空白名单群组"],
        "desc": "清空白名单群组",
    },
    "g_wuser": {
        "alias": ["g_wuser", "查询白名单用户"],
        "desc": "查询白名单用户",
    },
    "s_wuser": {
        "alias": ["s_wuser", "添加白名单用户"],
        "args": ["用户ID或昵称"],
        "desc": "添加白名单用户",
    },
    "r_wuser": {
        "alias": ["r_wuser", "移除白名单用户"],
        "args": ["用户ID或昵称或序列号"],
        "desc": "移除白名单用户",
    },
    "c_wuser": {
        "alias": ["c_wuser", "清空白名单用户"],
        "desc": "清空白名单用户",
    },
    "g_bgroup": {
        "alias": ["g_bgroup", "查询黑名单群组"],
        "desc": "查询黑名单群组",
    },
    "s_bgroup": {
        "alias": ["s_bgroup", "添加黑名单群组"],
        "args": ["群组名称"],
        "desc": "添加黑名单群组",
    },
    "r_bgroup": {
        "alias": ["r_bgroup", "移除黑名单群组"],
        "args": ["群组名称或序列号"],
        "desc": "移除黑名单群组",
    },
    "c_bgroup": {
        "alias": ["c_bgroup", "清空黑名单群组"],
        "desc": "清空黑名单群组",
    },
    "g_buser": {
        "alias": ["g_buser", "查询黑名单用户"],
        "desc": "查询黑名单用户",
    },
    "s_buser": {
        "alias": ["s_buser", "添加黑名单用户"],
        "args": ["用户ID或昵称"],
        "desc": "添加黑名单用户",
    },
    "r_buser": {
        "alias": ["r_buser", "移除黑名单用户"],
        "args": ["用户ID或昵称或序列号"],
        "desc": "移除黑名单用户",
    },
    "c_buser": {
        "alias": ["c_buser", "清空黑名单用户"],
        "desc": "清空黑名单用户",
    },
}

def read_pickle(path):
    with open(path, "rb") as f:
        data = pickle.load(f)
    return data


def write_pickle(path, content):
    with open(path, "wb") as f:
        pickle.dump(content, f)
    return True


def read_file(path):
    with open(path, mode="r", encoding="utf-8") as f:
        return f.read()


def write_file(path, content):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(content, f, indent=4)
    return True
def Text(msg, e_context: EventContext):
    return send(msg, e_context, ReplyType.TEXT)


def Image_file(msg, e_context: EventContext):
    return send(msg, e_context, ReplyType.IMAGE)


def Image_url(msg, e_context: EventContext):
    return send(msg, e_context, ReplyType.IMAGE_URL)


def Info(msg, e_context: EventContext):
    return send(msg, e_context, ReplyType.INFO)


def Error(msg, e_context: EventContext):
    return send(msg, e_context, ReplyType.ERROR)


def send(reply, e_context: EventContext, reply_type=ReplyType.TEXT, action=EventAction.BREAK_PASS):
    if isinstance(reply, Reply):
        if not reply.type and reply_type:
            reply.type = reply_type
    else:
        reply = Reply(reply_type, reply)
    e_context["reply"] = reply
    e_context.action = action
    return


def Textr(msg, e_context: EventContext):
    return send_reply(msg, e_context, ReplyType.TEXT)


def Image_filer(msg, e_context: EventContext):
    return send_reply(msg, e_context, ReplyType.IMAGE)


def Image_url_reply(msg, e_context: EventContext):
    return send_reply(msg, e_context, ReplyType.IMAGE_URL)


def Info_reply(msg, e_context: EventContext):
    return send_reply(msg, e_context, ReplyType.INFO)


def Error_reply(msg, e_context: EventContext):
    return send_reply(msg, e_context, ReplyType.ERROR)


def send_reply(reply, e_context: EventContext, reply_type=ReplyType.TEXT):
    if isinstance(reply, Reply):
        if not reply.type and reply_type:
            reply.type = reply_type
    else:
        reply = Reply(reply_type, reply)
    channel = e_context['channel']
    context = e_context['context']
    # reply的包装步骤
    rd = channel._decorate_reply(context, reply)
    # reply的发送步骤
    return channel._send_reply(context, rd)

def search_friends(name):
    userInfo = {
        "user_id": "",
        "user_nickname": ""
    }
    # 判断是id还是昵称
    if name.startswith("@"):
        friends = itchat.search_friends(userName=name)
    else:
        friends = itchat.search_friends(name=name)
    if friends and len(friends) > 0:
        if isinstance(friends, list):
            userInfo["user_id"] = friends[0]["UserName"]
            userInfo["user_nickname"] = friends[0]["NickName"]
        else:
            userInfo["user_id"] = friends["UserName"]
            userInfo["user_nickname"] = friends["NickName"]
    return userInfo


def env_detection(self, e_context: EventContext):
    trigger_prefix = conf().get("plugin_trigger_prefix", "$")
    reply = None
    
    # 如果用户是管理员或者在白名单用户列表中，则不受限制
    if self.userInfo["isadmin"] or self.userInfo["iswuser"]:
        return True
    
    # 如果用户不在白名单用户列表中且使用次数已用完
    if not self.userInfo["limit"]:
        # 检查是否在白名单群组中
        if self.userInfo["iswgroup"]:
            return True
        else:
            reply = Reply(ReplyType.ERROR, "[MJ] 您今日的使用次数已用完，请明日再来")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS
            return False

    return True
