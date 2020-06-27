import json
import os
import pathlib

import base58
from flask import request

from hive.util.auth import did_auth
from hive.util.did.ela_did_util import setup_did_backend, is_did_resolve, did_verify
from hive.util.did_info import add_did_info_to_db, create_token, save_token_to_db, create_nonce, \
    get_did_info_by_nonce, update_nonce_of_did_info, get_did_info_by_id
from hive.util.server_response import response_err, response_ok
from hive.util.did_resource import get_all_resource_of_did

from hive.util.constants import DID_PREFIX, DID_DB_PREFIX, DID_INFO_NONCE, DID_RESOURCE_NAME, \
    DID_RESOURCE_SCHEMA, DID_CHALLENGE_EXPIRE, DID_AUTH_REALM, DID_INFO_NONCE_EXPIRE, DID_TOKEN_EXPIRE, \
    RCLONE_CONFIG_FILE, did_tail_part
from hive.settings import MONGO_HOST, MONGO_PORT
from datetime import datetime


class HiveAuth:
    def __init__(self, app=None):
        self.app = app

    def init_app(self, app):
        self.app = app
        setup_did_backend()

    def did_auth_challenge(self):
        content = request.get_json(force=True, silent=True)
        if content is None:
            return response_err(400, "parameter is not application/json")
        did = content.get('iss', None)
        if did is None:
            return response_err(400, "parameter is null")

        ret = is_did_resolve(did)
        if not ret:
            return response_err(400, "parameter did error")

        nonce = create_nonce()
        time = datetime.now().timestamp()

        info = get_did_info_by_id(did)

        try:
            if info is None:
                add_did_info_to_db(did, nonce, time + DID_CHALLENGE_EXPIRE)
            else:
                update_nonce_of_did_info(did, nonce, time + DID_CHALLENGE_EXPIRE)
        except Exception as e:
            print("Exception in did_auth_challenge::", e)
            return response_err(500, "Exception in did_auth_challenge:" + e)

        s = base58.b58encode(did)

        data = {
            "subject": "didauth",
            "iss": "elastos_hive_node",
            "nonce": nonce,
            "callback": "/api/v1/did/%s/callback" % str(s, encoding="utf-8")
        }
        return response_ok(data)

    def did_auth_callback(self, did_base58):
        content = request.get_json(force=True, silent=True)
        if content is None:
            return response_err(400, "parameter is not application/json")
        subject = content.get('subject', None)
        iss = content.get('iss', None)
        realm = content.get('realm', None)
        nonce = content.get('nonce', None)
        key_name = content.get('key_name', None)
        sig = content.get('sig', None)

        if (subject is None) \
                or (iss is None) \
                or (realm is None) \
                or (nonce is None) \
                or (key_name is None) \
                or (sig is None):
            return response_err(400, "parameter is null")

        # 0. 验证realm
        if realm != DID_AUTH_REALM:
            return response_err(406, "auth realm error")

        # 1. nonce找出数据库did， 对比iss， url_did
        info = get_did_info_by_nonce(nonce)
        if info is None:
            return response_err(406, "auth nonce error")
        did = info["_id"]
        url_did = base58.b58decode(did_base58)
        # "utf-8"
        if (did != iss) or (did != str(url_did, encoding="utf-8")):
            return response_err(406, "auth did error")

        # 2. 验证过期时间
        expire = info[DID_INFO_NONCE_EXPIRE]
        now = datetime.now().timestamp()
        if now > expire:
            return response_err(406, "auth expire error")

        # 3. 获取public key， 校验sig
        ret = did_verify(did, sig, key_name, nonce)
        if not ret:
            return response_err(406, "auth sig error")

        # 校验成功, 生成token
        self.create_db(did)

        token = create_token()
        save_token_to_db(did, token, now + DID_TOKEN_EXPIRE)

        data = {"token": token}
        return response_ok(data)

    def create_db(self, did):
        with self.app.app_context():
            self.app.config[did + DID_PREFIX + "_URI"] = "mongodb://%s:%s/%s" % (
                MONGO_HOST,
                MONGO_PORT,
                DID_DB_PREFIX + did,
            )
            resource_list = get_all_resource_of_did(did)
            for resource in resource_list:
                collection = resource[DID_RESOURCE_NAME]
                schema = resource[DID_RESOURCE_SCHEMA]
                settings = {"schema": json.loads(schema), "mongo_prefix": did + DID_PREFIX}
                self.app.register_resource(collection, settings)

        return did
