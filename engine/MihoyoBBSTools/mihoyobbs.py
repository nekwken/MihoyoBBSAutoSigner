import json
import random
import time

import captcha
import config
import login
import setting
import tools
from error import StokenError
from loghelper import log
from request import http


def wait():
    time.sleep(random.randint(3, 8))


class Mihoyobbs:
    def __init__(self):
        self.today_get_coins = 0
        self.today_have_get_coins = 0
        self.have_coins = 0
        self.bbs_config = config.config["mihoyobbs"]
        self.bbs_list = [setting.mihoyobbs_List.get(i) for i in self.bbs_config["checkin_list"]
                         if setting.mihoyobbs_List.get(i) is not None]
        # 与 App 2.114.0 请求格式保持一致
        self.headers = {
            "DS": tools.get_ds2("", ""),
            "cookie": login.get_stoken_cookie(),
            "x-rpc-client_type": setting.mihoyobbs_Client_type,
            "x-rpc-app_version": setting.mihoyobbs_version,
            "x-rpc-sys_version": "16",
            "x-rpc-channel": "miyousheluodi",
            "x-rpc-device_id": config.config["device"]["id"],
            "x-rpc-device_name": config.config["device"]["name"],
            "x-rpc-device_model": config.config["device"]["model"],
            "Referer": "https://app.mihoyo.com",
            "x-rpc-verify_key": setting.mihoyobbs_verify_key,
            "Content-Type": "application/json; charset=UTF-8",
            "Host": "bbs-api.miyoushe.com",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "User-Agent": "okhttp/4.9.3"
        }
        if config.config["device"]["fp"] != "":
            self.headers["x-rpc-device_fp"] = config.config["device"]["fp"]
        # 2.114.0 起米游币仅打卡发放；看帖/点赞/分享奖励已下线（见 10-miyoubbs-tasks）
        self.task_do = {"sign": False}
        self.get_tasks_list()

    def get_pass_challenge(self):
        req = http.get(url=setting.bbs_get_captcha, headers=self.headers)
        data = req.json()
        if data["retcode"] != 0:
            return None
        captcha_result = captcha.bbs_captcha(data["data"]["gt"], data["data"]["challenge"])
        if captcha_result is not None:
            challenge = data["data"]["challenge"]
            if type(captcha_result) == dict:
                validate = captcha_result["validate"]
                challenge = captcha_result["challenge"]
            else:
                validate = captcha_result

            check_req = http.post(url=setting.bbs_captcha_verify, headers=self.headers,
                                  json={"geetest_challenge": challenge,
                                        "geetest_seccode": validate + "|jordan",
                                        "geetest_validate": validate})
            check = check_req.json()
            if check["retcode"] == 0:
                return check["data"]["challenge"]
        return None

    # 获取任务列表，用来判断打卡是否已完成
    def get_tasks_list(self, update=False):
        log.info("正在获取任务列表")
        header = self.headers.copy()
        header["DS"] = tools.get_ds(web=False)
        req = http.get(url=setting.bbs_tasks_list, params={"point_sn": "myb"}, headers=header)
        data = req.json()
        if "err" in data["message"] or data["retcode"] == -100:
            if not update and login.update_cookie_token():
                self.headers["cookie"] = login.get_stoken_cookie()
                return self.get_tasks_list(True)
            else:
                log.error("获取任务列表失败，stoken/cookie 可能已过期。")
                raise StokenError('Stoken expires')
        self.today_get_coins = data["data"]["can_get_points"]
        self.today_have_get_coins = data["data"]["already_received_points"]
        self.have_coins = data["data"]["total_points"]
        if self.today_get_coins == 0:
            self.task_do["sign"] = True
        else:
            mission_state = next((x for x in data["data"]["states"] if x["mission_id"] == 58), None)
            if mission_state is not None and mission_state["is_get_award"]:
                self.task_do["sign"] = True
        if self.today_get_coins != 0:
            log.info(f"今天可以获得 {self.today_get_coins} 个米游币")

    # 进行签到操作
    def signing(self):
        if self.task_do["sign"]:
            log.info("讨论区任务已经完成过了~")
            return
        log.info("正在签到......")
        header = self.headers.copy()
        captcha_blocked = False
        for forum in self.bbs_list:
            if captcha_blocked:
                break
            challenge = None
            for retry_count in range(2):
                # 真机 body 为紧凑 JSON；DS2 的 b= 必须与发送字节一致
                post_data = json.dumps({"gids": str(forum["id"])}, separators=(",", ":"))
                header["DS"] = tools.get_ds2("", post_data)
                req = http.post(url=setting.bbs_sign_url, data=post_data, headers=header)
                log.debug(req.text)
                data = req.json()
                if data["retcode"] == 1034:
                    challenge = self.get_pass_challenge()
                    if challenge is not None:
                        header["x-rpc-challenge"] = challenge
                    else:
                        # 本工程按约定不自动过验证码（captcha.bbs_captcha 恒返回 None），
                        # 所以这里没必要继续：原来每个板块都重试 2 次、每次还调一遍
                        # get_pass_challenge()（内部 2 个请求）—— 4 个板块就是 ~16 个
                        # 无用请求，只会加重风控。改为只提示一次并停止后续板块。
                        log.warning(
                            "社区签到触发验证码，本次社区签到未完成（游戏签到不受影响）。"
                            "请在米游社 App 里手动完成一次社区签到，或过一会儿再运行一次。")
                        captcha_blocked = True
                        break
                elif "err" not in data["message"] and data["retcode"] == 0:
                    log.info(str(forum["name"] + data["message"]))
                    wait()
                    break
                elif data["retcode"] == -100:
                    log.error("签到失败，你的 cookie 可能已过期，请重新设置 cookie。")
                    config.clear_stoken()
                    raise StokenError('Stoken expires')
                elif data["retcode"] == 1008:
                    log.info(str(forum["name"] + " 今日已打卡"))
                    break
                else:
                    log.error(f'未知错误：{req.text}')
            if challenge is not None:
                header.pop("x-rpc-challenge")

    def run_task(self):
        return_data = "米游社: "
        if self.task_do["sign"]:
            return_data += "\n" + f"今天已经全部完成了！\n" \
                                  f"一共获得 {self.today_have_get_coins} 个米游币\n目前有 {self.have_coins} 个米游币"
            log.info(f"今天已经全部完成了！一共获得 {self.today_have_get_coins} 个米游币，目前有 {self.have_coins} 个米游币")
            return return_data
        if self.bbs_config["checkin"]:
            self.signing()
            self.get_tasks_list()
        return_data += "\n" + f"今天已经获得 {self.today_have_get_coins} 个米游币\n" \
                              f"还能获得 {self.today_get_coins} 个米游币\n目前有 {self.have_coins} 个米游币"
        log.info(f"今天已经获得 {self.today_have_get_coins} 个米游币，"
                 f"还能获得 {self.today_get_coins} 个米游币，目前有 {self.have_coins} 个米游币")
        wait()
        return return_data
