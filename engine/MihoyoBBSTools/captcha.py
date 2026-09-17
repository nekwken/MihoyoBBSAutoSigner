from request import http


def game_captcha(gt: str, challenge: str) -> dict:
    # challenge不要直接用传入的，老格式也还支持，但是建议优先使用新格式
    return None  # 失败返回None 成功返回{"challenge":challenge,"validate":validate}


def bbs_captcha(gt: str, challenge: str) -> dict:
    return None
