import os
import sys
import time
import random
from typing import Tuple, Optional
from enum import Enum

import push
import login
import tools
import config
import mihoyobbs
import cloudgames
import gamecheckin
import hoyo_checkin
import web_activity
import os_cloudgames
from loghelper import log
from error import CookieError, StokenError


class StatusCode(Enum):
    SUCCESS = 0
    FAILURE = 1
    PARTIAL_FAILURE = 2
    CAPTCHA_TRIGGERED = 3


def check_github_actions() -> None:
    """检查是否在GitHub Actions环境运行"""
    if os.getenv('GITHUB_ACTIONS') == 'true':
        log.error("请不要在 GitHub Action 运行本项目")
        exit(0)


def initialize_config() -> Tuple[bool, Optional[str]]:
    """初始化配置"""
    config.load_config()
    if not config.config["enable"]:
        log.warning("Config 未启用！")
        return False, "Config 未启用！"
    return True, None


def handle_login() -> None:
    """处理登录逻辑"""
    account_cfg = config.config["account"]
    if any([
        account_cfg["stuid"] == "",
        account_cfg["stoken"] == "",
        account_cfg["mid"] == ""
    ]):
        if config.config["mihoyobbs"]["enable"]:
            login.login()
            time.sleep(random.randint(3, 8))
        account_cfg["cookie"] = tools.tidy_cookie(account_cfg["cookie"])


def run_mihoyobbs() -> Tuple[str, bool]:
    """执行米游社签到任务"""
    return_data = ""
    raise_stoken = False

    if config.config["mihoyobbs"]["enable"]:
        if config.config["account"]["stoken"] == "StokenError":
            return_data = "米游社：\n账号 Stoken 异常"
            raise_stoken = True
        else:
            try:
                bbs = mihoyobbs.Mihoyobbs()
                return_data = bbs.run_task()
            except StokenError:
                raise_stoken = True
    return return_data, raise_stoken


def run_cn_tasks() -> str:
    """执行国服任务"""
    result = []
    if config.config["games"]['cn']["enable"]:
        result.append(gamecheckin.run_task())
    if config.config["cloud_games"]['cn']["enable"]:
        log.info("正在进行云游戏签到")
        result.append(cloudgames.run_task())
    return "\n\n".join(filter(None, result))


def run_os_tasks() -> str:
    """执行国际服任务"""
    result = []
    if config.config["games"]['os']["enable"]:
        log.info("海外版：")
        os_result = hoyo_checkin.run_task()
        if os_result:
            result.append(f"海外版：{os_result}")
    if config.config["cloud_games"]['os']["enable"]:
        log.info("正在进行云游戏国际版签到")
        result.append(os_cloudgames.run_task())
    return "\n\n".join(filter(None, result))


def run_web_activity() -> None:
    """执行网页活动任务"""
    if config.config["web_activity"]['enable']:
        log.info("正在进行米游社网页活动任务")
        web_activity.run_task()


def classify_result(result_msg: str) -> int:
    text = result_msg or ""
    fail_tokens = (
        "出错", "失败", "异常", "无效", "已过期", "请重新", "无法",
        "CookieError", "StokenError", "Traceback", "retcode\":-100",
    )
    if "部分子任务失败" in text:
        return StatusCode.PARTIAL_FAILURE.value
    if "触发验证码" in text:
        return StatusCode.CAPTCHA_TRIGGERED.value
    if any(t in text for t in fail_tokens):
        return StatusCode.FAILURE.value
    ok_tokens = (
        "签到成功", "已经签到过了", "今天已经全部完成了",
        "已全部完成", "获得", "OK",
    )
    if any(t in text for t in ok_tokens):
        return StatusCode.SUCCESS.value
    if not text.strip():
        return StatusCode.FAILURE.value
    return StatusCode.SUCCESS.value


def main() -> Tuple[int, str]:
    check_github_actions()

    success, msg = initialize_config()
    if not success:
        return StatusCode.FAILURE.value, msg or "Config 未启用！"

    handle_login()

    if config.config["account"]["cookie"] == "CookieError":
        raise CookieError("Cookie expires")

    return_data = []
    raise_stoken = False

    try:
        mihoyo_result, raise_stoken = run_mihoyobbs()
        return_data.append(mihoyo_result)
    except StokenError:
        raise_stoken = True
    except Exception as e:
        log.error(f"米游社模块异常（已跳过）：{e}")
        return_data.append(f"米游社：模块异常 {e}")

    for name, fn in (("国服", run_cn_tasks), ("国际服", run_os_tasks)):
        try:
            return_data.append(fn())
        except Exception as e:
            log.error(f"{name}任务异常（已跳过）：{e}")
            return_data.append(f"{name}：模块异常 {e}")

    try:
        run_web_activity()
    except Exception as e:
        log.error(f"网页活动任务异常（已跳过）：{e}")

    if raise_stoken:
        raise StokenError("Stoken 异常")

    result_msg = "\n".join(filter(None, return_data))
    status_code = classify_result(result_msg)
    return status_code, result_msg


def task_run() -> int:
    status_code = StatusCode.FAILURE.value
    message = ""
    push_message = ""

    try:
        status_code, message = main()
        push_message = message or "（无输出）"
    except CookieError as e:
        status_code = StatusCode.FAILURE.value
        push_message = f"账号 Cookie 出错！{e}"
        log.error("账号 Cookie 有问题！")
    except StokenError as e:
        status_code = StatusCode.FAILURE.value
        push_message = f"账号 Stoken 出错！{e}"
        log.error("账号 Stoken 有问题！")
    except Exception as e:
        status_code = StatusCode.FAILURE.value
        push_message = f"签到执行异常：{e}"
        log.error(f"签到执行异常：{e}")

    try:
        push.push(status_code, push_message)
    except Exception as e:
        log.error(f"推送失败：{e}")

    if status_code == StatusCode.SUCCESS.value:
        summary = "签到成功" if "签到成功" in push_message else "签到任务完成"
        if "已经签到过了" in push_message or "今天已经全部完成了" in push_message:
            summary = "今日已签到/任务已完成"
        log.info(f"RESULT: SUCCESS | {summary}")
        log.info(push_message)
    elif status_code == StatusCode.PARTIAL_FAILURE.value:
        log.warning("RESULT: PARTIAL | 部分子任务失败，详情见日志")
        log.warning(push_message)
    elif status_code == StatusCode.CAPTCHA_TRIGGERED.value:
        log.warning("RESULT: FAILURE | 社区签到触发验证码")
        log.warning(push_message)
    else:
        log.error(f"RESULT: FAILURE | {push_message.splitlines()[0] if push_message else '签到失败'}")
        log.error(push_message)

    sys.exit(int(status_code))


if __name__ == "__main__":
    task_run()
