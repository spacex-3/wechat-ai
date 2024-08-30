# encoding:utf-8
import threading

import json
import time
import requests
import base64
import os
import io

import traceback
import plugins

from bridge.context import ContextType, Context
from bridge.reply import Reply, ReplyType
from channel.chat_message import ChatMessage
from channel.wechat.wechat_channel import WechatChannel

from common.expired_dict import ExpiredDict
from common.log import logger
from config import conf

from typing import Tuple

from PIL import Image
from apscheduler.schedulers.blocking import BlockingScheduler
from lib import itchat
from lib.itchat.content import *

from plugins import *
from .ctext import *



@plugins.register(
    name="Midjourney",
    desire_priority=-1,
    hidden=False,
    desc="AI drawing plugin of midjourney",
    version="2.0",
    author="SpaceX",
)
class Midjourney(Plugin):
    def __init__(self):
 
        super().__init__()

        self.trigger_prefix = "$"
        self.help_text = self._generate_help_text()
        
        try:
            #默认配置
            gconf = {
                "proxy_server": "",
                "proxy_api_secret": "",
                "mj_admin_password": "12345678",
                "daily_limit": 10
            }

            # 配置文件路径
            curdir = os.path.dirname(__file__)
            self.json_path = os.path.join(curdir, "config.json")
            self.roll_path = os.path.join(curdir, "user_info.pkl")
            self.user_datas_path = os.path.join(curdir, "user_datas.pkl")
            tm_path = os.path.join(curdir, "config.json.template")

            # 加载配置文件或模板
            jld = {}
            if os.path.exists(self.json_path):
                jld = json.loads(read_file(self.json_path))
            elif os.path.exists(tm_path):
                jld = json.loads(read_file(tm_path))

            # 合并配置（默认配置 -> 配置文件）
            if not isinstance(gconf, dict):
                raise TypeError(f"Expected gconf to be a dictionary but got {type(gconf)}")

            gconf = {**gconf, **jld}


            # 存储配置到类属性
            self.config = gconf
            if not isinstance(self.config, dict):
                raise TypeError(f"Expected self.config to be a dictionary but got {type(self.config)}")

            self.mj_admin_password = gconf.get("mj_admin_password")           
            self.proxy_server = gconf.get("proxy_server")
            self.proxy_api_secret = gconf.get("proxy_api_secret")
            
            self.handlers[Event.ON_HANDLE_CONTEXT] = self.on_handle_context 
            self.channel = WechatChannel()
            self.task_id_dict = ExpiredDict(60 * 60)
            self.cmd_dict = ExpiredDict(60 * 60)
            
            # 创建调度器
            scheduler = BlockingScheduler()
            scheduler.add_job(self.query_task_result, 'interval', seconds=10)
            # 创建并启动一个新的线程来运行调度器
            thread = threading.Thread(target=scheduler.start)
            thread.start()

            # self.config = gconf
            # logger.info("[MJ] config={}".format(self.config))
            
            # 重新写入合并后的配置文件
            write_file(self.json_path, self.config)

            # 初始化用户数据
            self.roll = {
                "mj_admin_users": [],
                "mj_groups": [],
                "mj_users": [],
                "mj_bgroups": [],
                "mj_busers": []
            }
            if os.path.exists(self.roll_path):
                sroll = read_pickle(self.roll_path)
                self.roll = {**self.roll, **sroll}

            # 写入用户列表
            write_pickle(self.roll_path, self.roll)

            # 初始化用户数据
            self.user_datas = {}
            if os.path.exists(self.user_datas_path):
                self.user_datas = read_pickle(self.user_datas_path)
                logger.debug(f"[MJ] Loaded user_datas: {self.user_datas}")

            self.ismj = True  # 机器人是否运行中

            logger.info("[MJ] inited")

        except Exception as e:
            logger.error(f"[MJ] init failed, ignored.")
            logger.warning(f"Traceback: {traceback.format_exc()}")
            raise e


    def get_help_text(self, **kwargs):
        # 获取用户的剩余使用次数
        remaining_uses = self.userInfo.get('limit', '未知')

        # 生成普通用户的帮助文本
        help_text = f"这是一个能调用midjourney实现ai绘图的扩展能力。\n今日剩余使用次数：{remaining_uses}\n使用说明:\n/imagine 根据给出的提示词绘画;\n/img2img 根据提示词+垫图生成图;\n/up 任务ID 序号执行动作;\n/describe 图片转文字;\n/shorten 提示词分析;\n/seed 获取任务图片的seed值;\n\n注意，使用本插件请避免政治、色情、名人等相关提示词，监测到则可能存在停止使用风险。"

        # 如果是管理员，附加管理员指令的帮助信息
        if kwargs.get("admin", False) is True:
            help_text += "\n\n管理员指令：\n"
            for cmd, info in ADMIN_COMMANDS.items():
                alias = [self.trigger_prefix + a for a in info["alias"][:1]]
                help_text += f"{','.join(alias)} "
                if "args" in info:
                    args = [a for a in info["args"]]
                    help_text += f"{' '.join(args)}"
                help_text += f": {info['desc']}\n"

        return help_text



    def _generate_help_text(self):
        help_text = "这是一个能调用midjourney实现ai绘图的扩展能力。\n"
        help_text += "今日剩余使用次数：{remaining_uses}\n"
        help_text += "使用说明: \n"
        help_text += "/imagine 根据给出的提示词绘画;\n"
        help_text += "/img2img 根据提示词+垫图生成图;\n"
        help_text += "/up 任务ID 序号执行动作;\n"
        help_text += "/describe 图片转文字;\n"
        help_text += "/shorten 提示词分析;\n"
        help_text += "/seed 获取任务图片的seed值;\n"
        return help_text


    def on_handle_context(self, e_context: EventContext):
        try:
            if not isinstance(self.user_datas, dict):
                logger.error(f"Expected self.user_datas to be a dictionary, but got {type(self.user_datas)}")

            if e_context["context"].type not in [ContextType.TEXT, ContextType.IMAGE]:
                return
            context = e_context["context"]
            content = context.content

            logger.debug(f"[MJ] on_handle_context. content={content}")
            msg: ChatMessage = context["msg"]
            
            self.sessionid = context["session_id"]
            logger.debug(f"[MJ] sessionid: {self.sessionid}")
            self.userInfo = self.get_user_info(e_context)
            if not isinstance(self.userInfo, dict):
                logger.error(f"Expected self.userInfo to be a dictionary, but got {type(self.userInfo)}")
            logger.debug(f"[MJ] userInfo: {self.userInfo}")
            self.isgroup = self.userInfo["isgroup"]
            logger.debug(f"[MJ] isgroup: {self.isgroup}")

            if ContextType.TEXT == context.type and content.startswith(self.trigger_prefix):
                return self.handle_command(e_context)

            # 拦截非白名单黑名单群组
            if not self.userInfo["isadmin"] and self.isgroup and not self.userInfo["iswgroup"] and self.userInfo["isbgroup"]:
                logger.debug("[MJ] Blocked by group whitelist/blacklist.")
                return

            # 拦截黑名单用户
            if not self.userInfo["isadmin"] and self.userInfo["isbuser"]:
                logger.debug("[MJ] Blocked by user blacklist.")
                return

            if not e_context["context"]["isgroup"]:
                state = "u:" + msg.other_user_id + ":" + msg.other_user_nickname
            else:
                state = "r:" + msg.other_user_id + ":" + msg.actual_user_nickname
            result = None
            try:
                env = env_detection(self, e_context)
                if not env:
                    return
                if content.startswith("/imagine "):
                    result = self.handle_imagine(content[9:], state)
                elif content.startswith("/up "):
                    arr = content[4:].split()
                    try:
                        task_id = arr[0]
                        index = int(arr[1])
                    except Exception as e:
                        e_context["reply"] = Reply(ReplyType.TEXT, '❌ 您的任务提交失败\nℹ️ 参数错误')
                        e_context.action = EventAction.BREAK_PASS
                        return
                    # 获取任务
                    task = self.get_task(task_id)
                    if task is None:
                        e_context["reply"] = Reply(ReplyType.TEXT, '❌ 您的任务提交失败\nℹ️ 任务ID不存在')
                        e_context.action = EventAction.BREAK_PASS
                        return
                    if index > len(task['buttons']):
                        e_context["reply"] = Reply(ReplyType.TEXT, '❌ 您的任务提交失败\nℹ️ 按钮序号不正确')
                        e_context.action = EventAction.BREAK_PASS
                        return
                    # 获取按钮
                    button = task['buttons'][index - 1]
                    if button['label'] == 'Custom Zoom':
                        e_context["reply"] = Reply(ReplyType.TEXT, '❌ 您的任务提交失败\nℹ️ 暂不支持自定义变焦')
                        e_context.action = EventAction.BREAK_PASS
                        return
                    result = self.post_json('/submit/action',
                                            {'customId': button['customId'], 'taskId': task_id, 'state': state})
                    if result.get("code") == 21:
                        result = self.post_json('/submit/modal',
                                            {'taskId': result.get("result"), 'state': state})
                elif content.startswith("/img2img "):
                    self.cmd_dict[msg.actual_user_id] = content
                    e_context["reply"] = Reply(ReplyType.TEXT, '请给我发一张图片作为垫图')
                    e_context.action = EventAction.BREAK_PASS
                    return
                elif content == "/describe":
                    self.cmd_dict[msg.actual_user_id] = content
                    e_context["reply"] = Reply(ReplyType.TEXT, '请给我发一张图片用于图生文')
                    e_context.action = EventAction.BREAK_PASS
                    return
                elif content.startswith("/shorten "):
                    result = self.handle_shorten(content[9:], state)
                elif content.startswith("/seed "):
                    task_id = content[6:]
                    result = self.get_task_image_seed(task_id)
                    if result.get("code") == 1:
                        e_context["reply"] = Reply(ReplyType.TEXT, '✅ 获取任务图片seed成功\n📨 任务ID: %s\n🔖 seed值: %s' % (
                                        task_id, result.get("result")))
                    else:
                        e_context["reply"] = Reply(ReplyType.TEXT, '❌ 获取任务图片seed失败\n📨 任务ID: %s\nℹ️ %s' % (
                                        task_id, result.get("description")))
                    e_context.action = EventAction.BREAK_PASS
                    return
                elif e_context["context"].type == ContextType.IMAGE:
                    cmd = self.cmd_dict.get(msg.actual_user_id)
                    if not cmd:
                        return
                    msg.prepare()
                    self.cmd_dict.pop(msg.actual_user_id)
                    if "/describe" == cmd:
                        result = self.handle_describe(content, state)
                    elif cmd.startswith("/img2img "):
                        result = self.handle_img2img(content, cmd[9:], state)
                    else:
                        return
                else:
                    return
            except Exception as e:
                logger.exception("[MJ] handle failed: %s" % e)
                result = {'code': -9, 'description': '服务异常, 请稍后再试'}
            code = result.get("code")
            # 获取用户当前剩余次数
            remaining_uses = self.user_datas[self.userInfo['user_id']]["mj_data"]["limit"]
            if code == 1:
                task_id = result.get("result")
                self.add_task(task_id)

                e_context["reply"] = Reply(ReplyType.TEXT,
                                        '✅ 您的任务已提交\n🚀 正在快速处理中，请稍后\n📨 任务ID: ' + task_id + '本次生成图像后，今日还剩余{remaining_uses}次。')
            elif code == 22:
                self.add_task(result.get("result"))
                e_context["reply"] = Reply(ReplyType.TEXT, '✅ 您的任务已提交\n⏰ ' + result.get("description")+ '本次生成图像后，今日还剩余{remaining_uses}次。')
            else:
                e_context["reply"] = Reply(ReplyType.TEXT, '❌ 您的任务提交失败\nℹ️ ' + result.get("description")+ '本次生成图像后，今日还剩余{remaining_uses}次。')
            e_context.action = EventAction.BREAK_PASS
        except Exception as e:
            logger.warning(f"[MJ] failed to generate pic, error={e}")
            logger.warning(f"Traceback: {traceback.format_exc()}")
            reply = Reply(ReplyType.TEXT, "抱歉！创作失败了，请稍后再试🥺")
            e_context["reply"] = reply
            e_context.action = EventAction.BREAK_PASS


    def handle_imagine(self, prompt, state):
        return self.post_json('/submit/imagine', {'prompt': prompt, 'state': state})

    def handle_describe(self, img_data, state):
        base64_str = self.image_file_to_base64(img_data)
        return self.post_json('/submit/describe', {'base64': base64_str, 'state': state})

    def handle_shorten(self, prompt, state):
        return self.post_json('/submit/shorten', {'prompt': prompt, 'state': state})

    def handle_img2img(self, img_data, prompt, state):
        base64_str = self.image_file_to_base64(img_data)
        return self.post_json('/submit/imagine', {'prompt': prompt, 'base64': base64_str, 'state': state})

    def post_json(self, api_path, data):
        return requests.post(url=self.proxy_server + api_path, json=data,
                             headers={'mj-api-secret': self.proxy_api_secret}).json()

    def get_task(self, task_id):
        return requests.get(url=self.proxy_server + '/task/%s/fetch' % task_id,
                            headers={'mj-api-secret': self.proxy_api_secret}).json()
    
    def get_task_image_seed(self, task_id):
        return requests.get(url=self.proxy_server + '/task/%s/image-seed' % task_id,
                        headers={'mj-api-secret': self.proxy_api_secret}).json()

    def add_task(self, task_id):
        self.task_id_dict[task_id] = 'NOT_START'

    def query_task_result(self):
        task_ids = list(self.task_id_dict.keys())
        if len(task_ids) == 0:
            return
        logger.info("[MJ] handle task , size [%s]", len(task_ids))
        tasks = self.post_json('/task/list-by-condition', {'ids': task_ids})
        for task in tasks:
            task_id = task['id']
            description = task['description']
            status = task['status']
            action = task['action']
            state_array = task['state'].split(':', 2)
            # Check length of state_array
            if len(state_array) >= 3:
                context = Context()
                context.__setitem__("receiver", state_array[1])
                reply_prefix = '@%s ' % state_array[2] if state_array[0] == 'r' else ''
            else:
                logger.error(f"Invalid state format: {task['state']}")
                continue  # Skip this task or handle the error appropriately

            if status == 'SUCCESS':
                logger.debug("[MJ] 任务已完成: " + task_id)
                self.task_id_dict.pop(task_id)
                if action == 'DESCRIBE' or action == 'SHORTEN':
                    prompt = task['properties']['finalPrompt']
                    reply = Reply(ReplyType.TEXT, (
                                reply_prefix + '✅ 任务已完成\n📨 任务ID: %s\n%s\n\n' + self.get_buttons(
                            task) + '\n' + '💡 使用 /up 任务ID 序号执行动作\n🔖 /up %s 1') % (
                                      task_id, prompt, task_id))
                    self.channel.send(reply, context)
                elif action == 'UPSCALE':
                    reply = Reply(ReplyType.TEXT,
                                  ('✅ 任务已完成\n📨 任务ID: %s\n✨ %s\n\n' + self.get_buttons(
                                      task) + '\n' + '💡 使用 /up 任务ID 序号执行动作\n🔖 /up %s 1') % (
                                      task_id, description, task_id))
                    url_reply = Reply(ReplyType.IMAGE_URL, task['imageUrl'])
                    self.channel.send(url_reply, context)
                    self.channel.send(reply, context)
                else:
                    reply = Reply(ReplyType.TEXT,
                                  ('✅ 任务已完成\n📨 任务ID: %s\n✨ %s\n\n' + self.get_buttons(
                                      task) + '\n' + '💡 使用 /up 任务ID 序号执行动作\n🔖 /up %s 1') % (
                                      task_id, description, task_id))
                    image_storage = self.download_and_compress_image(task['imageUrl'])
                    url_reply = Reply(ReplyType.IMAGE, image_storage)
                    self.channel.send(url_reply, context)
                    self.channel.send(reply, context)
            elif status == 'FAILURE':
                self.task_id_dict.pop(task_id)
                reply = Reply(ReplyType.TEXT,
                              reply_prefix + '❌ 任务执行失败\n✨ %s\n📨 任务ID: %s\n📒 失败原因: %s' % (
                              description, task_id, task['failReason']))
                self.channel.send(reply, context)

    def image_file_to_base64(self, file_path):
        with open(file_path, "rb") as image_file:
            img_data = image_file.read()
        img_base64 = base64.b64encode(img_data).decode("utf-8")
        os.remove(file_path)
        return "data:image/png;base64," + img_base64

    def get_buttons(self, task):
        res = ''
        index = 1
        for button in task['buttons']:
            name = button['emoji'] + button['label']
            if name in ['🎉Imagine all', '❤️']:
                continue
            res += ' %d- %s\n' % (index, name)
            index += 1
        return res

    def download_and_compress_image(self, img_url, max_size=(800, 800)):  # 下载并压缩图片
        # 下载图片
        pic_res = requests.get(img_url, stream=True)
        image_storage = io.BytesIO()
        for block in pic_res.iter_content(1024):
            image_storage.write(block)
        image_storage.seek(0)

        # 压缩图片
        initial_image = Image.open(image_storage)
        initial_image.thumbnail(max_size)
        output = io.BytesIO()
        initial_image.save(output, format=initial_image.format)
        output.seek(0)
        
        userInfo = self.userInfo
        if self.user_datas[userInfo['user_id']]["mj_data"]["limit"] > 0:
            self.user_datas[userInfo['user_id']]["mj_data"]["limit"] -= 1
            write_pickle(self.user_datas_path, self.user_datas)
        
        return output

    # 指令处理
    def handle_command(self, e_context: EventContext):
        content = e_context['context'].content
        com = content[1:].strip().split()
        cmd = com[0]
        args = com[1:]
        if any(cmd in info["alias"] for info in COMMANDS.values()):
            cmd = next(c for c, info in COMMANDS.items() if cmd in info["alias"])
            if cmd == "mj_help":
                return Info(self.get_help_text(admin=self.userInfo.get("isadmin", False)), e_context)
            elif cmd == "mj_admin_cmd":
                if not self.userInfo["isadmin"]:
                    return Error("[MJ] 您没有权限执行该操作，请先进行管理员认证", e_context)
                return Info(self.get_help_text(admin=True), e_context)
            elif cmd == "mj_admin_password":
                ok, result = self.authenticate(self.userInfo, args)
                if not ok:
                    return Error(result, e_context)
                else:
                    return Info(result, e_context)
        elif any(cmd in info["alias"] for info in ADMIN_COMMANDS.values()):
            cmd = next(c for c, info in ADMIN_COMMANDS.items() if cmd in info["alias"])
            if not self.userInfo["isadmin"]:
                return Error("[MJ] 您没有权限执行该操作，请先进行管理员认证", e_context)
            # 在 handle_command 函数中添加 g_info 处理逻辑
            if cmd == "g_info":
                user_infos = []
                for uid, data in self.user_datas.items():
                    user_nickname = data.get("user_nickname", None)
                    limit = data.get("mj_data", {}).get("limit", "未知次数")
                    
                    if not user_nickname:  # 如果在 `user_datas` 中没有昵称
                        user_info = search_friends(uid)
                        user_nickname = user_info.get("user_nickname", None)

                    if user_nickname:  # 如果找到昵称，才添加到结果中
                        user_infos.append(f"{user_nickname}: {limit}次")

                # 将所有用户信息拼接成一个字符串
                if user_infos:
                    info_text = "当前用户昵称及剩余次数:\n" + "\n".join(user_infos)
                else:
                    info_text = "没有找到用户数据。"
                
                return Info(info_text, e_context)

            if cmd == "mj_tip":
                self.config["tip"] = not self.config["tip"]
                write_file(self.json_path, self.config)
                return Info(f"[MJ] 提示功能已{'开启' if self.config['tip'] else '关闭'}", e_context)

            elif cmd == "s_limit":
                if len(args) < 1:
                    return Error("[MJ] 请输入需要设置的数量", e_context)
                limit = int(args[0])
                if limit < 0:
                    return Error("[MJ] 数量不能小于0", e_context)
                self.config["daily_limit"] = limit
                for index, item in self.user_datas.items():
                    if "mj_data" in item:  # 确保 mj_data 字段存在
                        self.user_datas[index]["mj_data"]["limit"] = limit
                write_pickle(self.user_datas_path, self.user_datas)
                write_file(self.json_path, self.config)
                return Info(f"[MJ] 每日使用次数已设置为{limit}次", e_context)

            elif cmd == "r_limit":
                for index, item in self.user_datas.items():
                    if "mj_data" in item:  # 确保 mj_data 字段存在
                        self.user_datas[index]["mj_data"]["limit"] = self.config["daily_limit"]
                write_pickle(self.user_datas_path, self.user_datas)
                return Info(f"[MJ] 所有用户每日使用次数已重置为{self.config['daily_limit']}次", e_context)

            elif cmd == "set_mj_admin_password":
                if len(args) < 1:
                    return Error("[MJ] 请输入需要设置的密码", e_context)
                password = args[0]
                if self.isgroup:
                    return Error("[MJ] 为避免密码泄露，请勿在群聊中进行修改", e_context)
                if len(password) < 6:
                    return Error("[MJ] 密码长度不能小于6位", e_context)
                if password == self.config['mj_admin_password']:
                    return Error("[MJ] 新密码不能与旧密码相同", e_context)
                self.config["mj_admin_password"] = password
                write_file(self.json_path, self.config)
                return Info("[MJ] 管理员口令设置成功", e_context)
            elif cmd == "stop_mj":
                self.ismj = False
                return Info("[MJ] 服务已暂停", e_context)
            elif cmd == "enable_mj":
                self.ismj = True
                return Info("[MJ] 服务已启用", e_context)
            elif cmd == "g_admin_list" and not self.isgroup:
                adminUser = self.roll["mj_admin_users"]
                t = "\n"
                nameList = t.join(f'{index+1}. {data["user_nickname"]}' for index, data in enumerate(adminUser))
                return Info(f"[MJ] 管理员用户\n{nameList}", e_context)
            elif cmd == "c_admin_list" and not self.isgroup:
                self.roll["mj_admin_users"] = []
                write_pickle(self.roll_path, self.roll)
                return Info("[MJ] 管理员用户已清空", e_context)
            elif cmd == "s_admin_list" and not self.isgroup:
                user_name = args[0] if args and args[0] else ""
                adminUsers = self.roll["mj_admin_users"]
                buser = self.roll["mj_busers"]
                if not args or len(args) < 1:
                    return Error("[MJ] 请输入需要设置的管理员名称或ID", e_context)
                index = -1
                for i, user in enumerate(adminUsers):
                    if user["user_id"] == user_name or user["user_nickname"] == user_name:
                        index = i
                        break
                if index >= 0:
                    return Error(f"[MJ] 管理员[{adminUsers[index]['user_nickname']}]已在列表中", e_context)
                for i, user in enumerate(buser):
                    if user == user_name:
                        index = i
                        break
                if index >= 0:
                    return Error(f"[MJ] 用户[{user_name}]已在黑名单中，如需添加请先进行移除", e_context)
                userInfo = {
                    "user_id": user_name,
                    "user_nickname": user_name
                }
                # 判断是否是itchat平台
                if conf().get("channel_type", "wx") == "wx":
                    userInfo = search_friends(user_name)
                    # 判断user_name是否在列表中
                    if not userInfo or not userInfo["user_id"]:
                        return Error(f"[MJ] 用户[{user_name}]不存在通讯录中", e_context)
                adminUsers.append(userInfo)
                self.roll["mj_admin_users"] = adminUsers
                # 写入用户列表
                write_pickle(self.roll_path, self.roll)
                return Info(f"[MJ] 管理员[{userInfo['user_nickname']}]已添加到列表中", e_context)
            elif cmd == "r_admin_list" and not self.isgroup:
                text = ""
                adminUsers = self.roll["mj_admin_users"]
                if len(args) < 1:
                    return Error("[MJ] 请输入需要移除的管理员名称或ID或序列号", e_context)
                if args and args[0]:
                    if args[0].isdigit():
                        index = int(args[0]) - 1
                        if index < 0 or index >= len(adminUsers):
                            return Error(f"[MJ] 序列号[{args[0]}]不存在", e_context)
                        user_name = adminUsers[index]['user_nickname']
                        del adminUsers[index]
                        self.roll["mj_admin_users"] = adminUsers
                        write_pickle(self.roll_path, self.roll)
                        text = f"[MJ] 管理员[{user_name}]已从列表中移除"
                    else:
                        user_name = args[0]
                        index = -1
                        for i, user in enumerate(adminUsers):
                            if user["user_nickname"] == user_name or user["user_id"] == user_name:
                                index = i
                                break
                        if index >= 0:
                            del adminUsers[index]
                            text = f"[MJ] 管理员[{user_name}]已从列表中移除"
                            self.roll["mj_admin_users"] = adminUsers
                            write_pickle(self.roll_path, self.roll)
                        else:
                            return Error(f"[MJ] 管理员[{user_name}]不在列表中", e_context)
                return Info(text, e_context)
            elif cmd == "g_wgroup" and not self.isgroup:
                text = ""
                groups = self.roll["mj_groups"]
                if len(groups) == 0:
                    text = "[MJ] 白名单群组：无"
                else:
                    t = "\n"
                    nameList = t.join(f'{index+1}. {group}' for index, group in enumerate(groups))
                    text = f"[MJ] 白名单群组\n{nameList}"
                return Info(text, e_context)
            elif cmd == "c_wgroup":
                self.roll["mj_groups"] = []
                write_pickle(self.roll_path, self.roll)
                return Info("[MJ] 群组白名单已清空", e_context)
            elif cmd == "s_wgroup":
                groups = self.roll["mj_groups"]
                bgroups = self.roll["mj_bgroups"]
                if not self.isgroup and len(args) < 1:
                    return Error("[MJ] 请输入需要设置的群组名称", e_context)
                if self.isgroup:
                    group_name = self.userInfo["group_name"]
                if args and args[0]:
                    group_name = args[0]
                if group_name in groups:
                    return Error(f"[MJ] 群组[{group_name}]已在白名单中", e_context)
                if group_name in bgroups:
                    return Error(f"[MJ] 群组[{group_name}]已在黑名单中，如需添加请先进行移除", e_context)
                # 判断是否是itchat平台，并判断group_name是否在列表中
                if conf().get("channel_type", "wx") == "wx":
                    chatrooms = itchat.search_chatrooms(name=group_name)
                    if len(chatrooms) == 0:
                        return Error(f"[MJ] 群组[{group_name}]不存在", e_context)
                groups.append(group_name)
                self.roll["mj_groups"] = groups
                write_pickle(self.roll_path, self.roll)
                return Info(f"[MJ] 群组[{group_name}]已添加到白名单", e_context)
            elif cmd == "r_wgroup":
                groups = self.roll["mj_groups"]
                if not self.isgroup and len(args) < 1:
                    return Error("[MJ] 请输入需要移除的群组名称或序列号", e_context)
                if self.isgroup:
                    group_name = self.userInfo["group_name"]
                if args and args[0]:
                    if args[0].isdigit():
                        index = int(args[0]) - 1
                        if index < 0 or index >= len(groups):
                            return Error(f"[MJ] 序列号[{args[0]}]不在白名单中", e_context)
                        group_name = groups[index]
                    else:
                        group_name = args[0]
                if group_name in groups:
                    groups.remove(group_name)
                    self.roll["mj_groups"] = groups
                    write_pickle(self.roll_path, self.roll)
                    return Info(f"[MJ] 群组[{group_name}]已从白名单中移除", e_context)
                else:
                    return Error(f"[MJ] 群组[{group_name}]不在白名单中", e_context)
            elif cmd == "g_bgroup" and not self.isgroup:
                text = ""
                bgroups = self.roll["mj_bgroups"]
                if len(bgroups) == 0:
                    text = "[MJ] 黑名单群组：无"
                else:
                    t = "\n"
                    nameList = t.join(f'{index+1}. {group}' for index, group in enumerate(bgroups))
                    text = f"[MJ] 黑名单群组\n{nameList}"
                return Info(text, e_context)
            elif cmd == "c_bgroup":
                self.roll["mj_bgroups"] = []
                write_pickle(self.roll_path, self.roll)
                return Info("[MJ] 已清空黑名单群组", e_context)
            elif cmd == "s_bgroup":
                groups = self.roll["mj_groups"]
                bgroups = self.roll["mj_bgroups"]
                if not self.isgroup and len(args) < 1:
                    return Error("[MJ] 请输入需要设置的群组名称", e_context)
                if self.isgroup:
                    group_name = self.userInfo["group_name"]
                if args and args[0]:
                    group_name = args[0]
                if group_name in groups:
                    return Error(f"[MJ] 群组[{group_name}]已在白名单中，如需添加请先进行移除", e_context)
                if group_name in bgroups:
                    return Error(f"[MJ] 群组[{group_name}]已在黑名单中", e_context)
                # 判断是否是itchat平台，并判断group_name是否在列表中
                if conf().get("channel_type", "wx") == "wx":
                    chatrooms = itchat.search_chatrooms(name=group_name)
                    if len(chatrooms) == 0:
                        return Error(f"[MJ] 群组[{group_name}]不存在", e_context)
                bgroups.append(group_name)
                self.roll["mj_bgroups"] = bgroups
                write_pickle(self.roll_path, self.roll)
                return Info(f"[MJ] 群组[{group_name}]已添加到黑名单", e_context)
            elif cmd == "r_bgroup":
                bgroups = self.roll["mj_bgroups"]
                if not self.isgroup and len(args) < 1:
                    return Error("[MJ] 请输入需要移除的群组名称或序列号", e_context)
                if self.isgroup:
                    group_name = self.userInfo["group_name"]
                if args and args[0]:
                    if args[0].isdigit():
                        index = int(args[0]) - 1
                        if index < 0 or index >= len(bgroups):
                            return Error(f"[MJ] 序列号[{args[0]}]不在黑名单中", e_context)
                        group_name = bgroups[index]
                    else:
                        group_name = args[0]
                if group_name in bgroups:
                    bgroups.remove(group_name)
                    self.roll["mj_bgroups"] = bgroups
                    write_pickle(self.roll_path, self.roll)
                    return Info(f"[MJ] 群组[{group_name}]已从黑名单中移除", e_context)
                else:
                    return Error(f"[MJ] 群组[{group_name}]不在黑名单中", e_context)
            elif cmd == "g_buser" and not self.isgroup:
                busers = self.roll["mj_busers"]
                if len(busers) == 0:
                    return Info("[MJ] 黑名单用户：无", e_context)
                else:
                    t = "\n"
                    nameList = t.join(f'{index+1}. {data}' for index, data in enumerate(busers))
                    return Info(f"[MJ] 黑名单用户\n{nameList}", e_context)
            elif cmd == "g_wuser" and not self.isgroup:
                users = self.roll["mj_users"]
                if len(users) == 0:
                    return Info("[MJ] 白名单用户：无", e_context)
                else:
                    t = "\n"
                    nameList = t.join(f'{index+1}. {data}' for index, data in enumerate(users))
                    return Info(f"[MJ] 白名单用户\n{nameList}", e_context)
            elif cmd == "c_wuser":
                self.roll["mj_users"] = []
                write_pickle(self.roll_path, self.roll)
                return Info("[MJ] 用户白名单已清空", e_context)
            elif cmd == "c_buser":
                self.roll["mj_busers"] = []
                write_pickle(self.roll_path, self.roll)
                return Info("[MJ] 用户黑名单已清空", e_context)
            elif cmd == "s_wuser":
                user_name = args[0] if args and args[0] else ""
                users = self.roll["mj_users"]
                busers = self.roll["mj_busers"]
                if not args or len(args) < 1:
                    return Error("[MJ] 请输入需要设置的用户名称或ID", e_context)
                index = -1
                for i, user in enumerate(users):
                    if user == user_name:
                        index = i
                        break
                if index >= 0:
                    return Error(f"[MJ] 用户[{user_name}]已在白名单中", e_context)
                for i, user in enumerate(busers):
                    if user == user_name:
                        index = i
                        break
                if index >= 0:
                    return Error(f"[MJ] 用户[{user_name}]已在黑名单中，如需添加请先移除黑名单", e_context)
                # 判断是否是itchat平台
                if conf().get("channel_type", "wx") == "wx":
                    userInfo = search_friends(user_name)
                    # 判断user_name是否在列表中
                    if not userInfo or not userInfo["user_id"]:
                        return Error(f"[MJ] 用户[{user_name}]不存在通讯录中", e_context)
                users.append(user_name)
                self.roll["mj_users"] = users
                write_pickle(self.roll_path, self.roll)
                return Info(f"[MJ] 用户[{user_name}]已添加到白名单", e_context)
            elif cmd == "s_buser":
                user_name = args[0] if args and args[0] else ""
                users = self.roll["mj_users"]
                busers = self.roll["mj_busers"]
                if not args or len(args) < 1:
                    return Error("[MJ] 请输入需要设置的用户名称或ID", e_context)
                index = -1
                for i, user in enumerate(users):
                    if user == user_name:
                        index = i
                        break
                if index >= 0:
                    return Error(f"[MJ] 用户[{user_name}]已在白名单中，如需添加请先移除白名单", e_context)
                for i, user in enumerate(busers):
                    if user == user_name:
                        index = i
                        break
                if index >= 0:
                    return Error(f"[MJ] 用户[{user_name}]已在黑名单中", e_context)
                # 判断是否是itchat平台
                if conf().get("channel_type", "wx") == "wx":
                    userInfo = search_friends(user_name)
                    # 判断user_name是否在列表中
                    if not userInfo or not userInfo["user_id"]:
                        return Error(f"[MJ] 用户[{user_name}]不存在通讯录中", e_context)
                busers.append(user_name)
                self.roll["mj_busers"] = busers
                write_pickle(self.roll_path, self.roll)
                return Info(f"[MJ] 用户[{user_name}]已添加到黑名单", e_context)
            elif cmd == "r_wuser":
                text = ""
                users = self.roll["mj_users"]
                if len(args) < 1:
                    return Error("[MJ] 请输入需要移除的用户名称或ID或序列号", e_context)
                if args and args[0]:
                    if args[0].isdigit():
                        index = int(args[0]) - 1
                        if index < 0 or index >= len(users):
                            return Error(f"[MJ] 序列号[{args[0]}]不存在", e_context)
                        user_name = users[index]
                        del users[index]
                        self.roll["mj_users"] = users
                        write_pickle(self.roll_path, self.roll)
                        text = f"[MJ] 用户[{user_name}]已从白名单中移除"
                    else:
                        user_name = args[0]
                        index = -1
                        for i, user in enumerate(users):
                            if user == user_name:
                                index = i
                                break
                        if index >= 0:
                            del users[index]
                            text = f"[MJ] 用户[{user_name}]已从白名单中移除"
                            self.roll["mj_users"] = users
                            write_pickle(self.roll_path, self.roll)
                        else:
                            return Error(f"[MJ] 用户[{user_name}]不在白名单中", e_context)
                return Info(text, e_context)
            elif cmd == "r_buser":
                text = ""
                busers = self.roll["mj_busers"]
                if len(args) < 1:
                    return Error("[MJ] 请输入需要移除的用户名称或ID或序列号", e_context)
                if args and args[0]:
                    if args[0].isdigit():
                        index = int(args[0]) - 1
                        if index < 0 or index >= len(busers):
                            return Error(f"[MJ] 序列号[{args[0]}]不存在", e_context)
                        user_name = busers[index]
                        del busers[index]
                        self.roll["mj_busers"] = busers
                        write_pickle(self.roll_path, self.roll)
                        text = f"[MJ] 用户[{user_name}]已从黑名单中移除"
                    else:
                        user_name = args[0]
                        index = -1
                        for i, user in enumerate(busers):
                            if user == user_name:
                                index = i
                                break
                        if index >= 0:
                            del busers[index]
                            text = f"[MJ] 用户[{user_name}]已从黑名单中移除"
                            self.roll["mj_busers"] = busers
                            write_pickle(self.roll_path, self.roll)
                        else:
                            return Error(f"[MJ] 用户[{user_name}]不在黑名单中", e_context)
                return Info(text, e_context)
            else:
                return "Bye"
                
    def authenticate(self, userInfo, args) -> Tuple[bool, str]:
        isgroup = userInfo["isgroup"]
        isadmin = userInfo["isadmin"]
        if isgroup:
            return False, "[MJ] 为避免密码泄露，请勿在群聊中认证"

        if isadmin:
            return False, "[MJ] 管理员账号无需认证"

        if len(args) != 1:
            return False, "[MJ] 请输入密码"

        password = args[0]
        if password == self.config['mj_admin_password']:
            self.roll["mj_admin_users"].append({
                "user_id": userInfo["user_id"],
                "user_nickname": userInfo["user_nickname"]
            })
            write_pickle(self.roll_path, self.roll)
            return True, f"[MJ] 认证成功"
        else:
            return False, "[MJ] 认证失败"

    
    def get_user_info(self, e_context: EventContext):
            # 获取当前时间戳
            current_timestamp = time.time()
            # 将当前时间戳和给定时间戳转换为日期字符串
            current_date = time.strftime("%Y-%m-%d", time.localtime(current_timestamp))
            groups = self.roll["mj_groups"]
            bgroups = self.roll["mj_bgroups"]
            users = self.roll["mj_users"]
            logger.debug(f"[MJ] Type of users: {type(users)}, Content: {users}")
            busers = self.roll["mj_busers"]
            mj_admin_users = self.roll["mj_admin_users"]
            context = e_context['context']
            msg: ChatMessage = context["msg"]
            isgroup = context.get("isgroup", False)
            # 写入用户信息，企业微信没有from_user_nickname，所以使用from_user_id代替
            uid = msg.from_user_id if not isgroup else msg.actual_user_id
            uname = (msg.from_user_nickname if msg.from_user_nickname else uid) if not isgroup else msg.actual_user_nickname
            logger.debug(f"[MJ] UID: {uid}, User data keys: {list(self.user_datas.keys())}")
            if uid not in self.user_datas:
                logger.warning(f"[MJ] UID: {uid} not found in user_datas")
            else:
                logger.debug(f"[MJ] Found UID: {uid}, Data: {self.user_datas[uid]}")

            userInfo = {
                "user_id": uid,
                "user_nickname": uname,
                "isgroup": isgroup,
                "group_id": msg.from_user_id if isgroup else "",
                "group_name": msg.from_user_nickname if isgroup else "",
            }
            # 判断是否是新的一天
            logger.debug(f"[MJ] UID: {uid}, Type of self.user_datas[uid]: {type(self.user_datas.get(uid))}, Content: {self.user_datas.get(uid)}")
            if uid not in self.user_datas or "mj_data" not in self.user_datas[uid] or "mj_data" not in self.user_datas[uid] or self.user_datas[uid]["mj_data"]["time"] != current_date:
                mj_data = {
                    "limit": self.config["daily_limit"],
                    "time": current_date
                }
                if uid in self.user_datas and self.user_datas[uid]["mj_data"]:
                    self.user_datas[uid]["mj_data"] = mj_data
                else:
                    self.user_datas[uid] = {
                        "mj_data": mj_data
                    }
                write_pickle(self.user_datas_path, self.user_datas)
            limit = self.user_datas[uid]["mj_data"]["limit"] if "mj_data" in self.user_datas[uid] and "limit" in self.user_datas[uid]["mj_data"] and self.user_datas[uid]["mj_data"]["limit"] and self.user_datas[uid]["mj_data"]["limit"] > 0 else False
            userInfo['limit'] = limit
            userInfo['isadmin'] = uid in [user["user_id"] for user in mj_admin_users]

            # 判断白名单用户
            if isinstance(users, list):
                if all(isinstance(user, dict) for user in users):
                    userInfo['iswuser'] = uname in [user["user_nickname"] for user in users]
                else:
                    userInfo['iswuser'] = uname in users  # users 中为字符串时
            else:
                userInfo['iswuser'] = False
            
            # 判断黑名单用户
            if isinstance(busers, list):
                if all(isinstance(user, dict) for user in busers):
                    userInfo['isbuser'] = uname in [user["user_nickname"] for user in busers]
                else:
                    userInfo['isbuser'] = uname in busers  # busers 中为字符串时
            else:
                userInfo['isbuser'] = False
            
            userInfo['iswgroup'] = userInfo["group_name"] in groups
            userInfo['isbgroup'] = userInfo["group_name"] in bgroups
            return userInfo
