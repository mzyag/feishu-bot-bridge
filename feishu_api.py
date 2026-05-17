import json
from typing import Optional, Tuple

import lark_oapi as lark

from config import SETTINGS

LARK_CLIENT = (
    lark.Client.builder()
    .app_id(SETTINGS.app_id)
    .app_secret(SETTINGS.app_secret)
    .timeout(float(SETTINGS.feishu_http_timeout_sec))
    .log_level(lark.LogLevel.INFO)
    .build()
)


def reply_text(open_id: str, text: str) -> Optional[str]:
    req = (
        lark.im.v1.CreateMessageRequest.builder()
        .receive_id_type("open_id")
        .request_body(
            lark.im.v1.CreateMessageRequestBody.builder()
            .receive_id(open_id)
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )

    resp = LARK_CLIENT.im.v1.message.create(req)
    if not resp.success():
        lark.logger.error(
            "send message failed, code=%s, msg=%s, req_id=%s",
            resp.code,
            resp.msg,
            resp.get_log_id(),
        )
        return None
    lark.logger.info("sent reply to %s", open_id)
    try:
        return resp.data.message_id if resp.data else None
    except Exception:
        return None


def update_text_message(message_id: str, text: str) -> Tuple[bool, Optional[int]]:
    if not message_id:
        return False, None
    req = (
        lark.im.v1.UpdateMessageRequest.builder()
        .message_id(message_id)
        .request_body(
            lark.im.v1.UpdateMessageRequestBody.builder()
            .msg_type("text")
            .content(json.dumps({"text": text}, ensure_ascii=False))
            .build()
        )
        .build()
    )
    resp = LARK_CLIENT.im.v1.message.update(req)
    if not resp.success():
        err_code: Optional[int] = None
        try:
            err_code = int(resp.code)
        except Exception:
            err_code = None
        lark.logger.error(
            "update message failed, message_id=%s, code=%s, msg=%s, req_id=%s",
            message_id,
            resp.code,
            resp.msg,
            resp.get_log_id(),
        )
        return False, err_code
    lark.logger.info("updated message %s", message_id)
    return True, None
