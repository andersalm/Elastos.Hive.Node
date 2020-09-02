import json
import logging

from flask import request
from datetime import datetime
import time
import os

from hive.util.did.eladid import ffi, lib

from hive.util.did_info import add_did_info_to_db, create_nonce, get_did_info_by_nonce, update_nonce_of_did_info, \
    get_did_info_by_did_appid, update_token_of_did_info
from hive.util.server_response import response_err, response_ok
from hive.settings import DID_CHALLENGE_EXPIRE, DID_TOKEN_EXPIRE
from hive.util.constants import DID_INFO_DB_NAME, DID_INFO_REGISTER_COL, DID, APP_ID, DID_INFO_NONCE, DID_INFO_TOKEN, \
    DID_INFO_NONCE_EXPIRE, DID_INFO_TOKEN_EXPIRE, APP_INSTANCE_DID

from hive.util.did.entity import Entity, cache_dir

ACCESS_AUTH_COL = "did_auth"
APP_DID = "appdid"
ACCESS_TOKEN = "access_token"
# localdids = $HIVE_DATA/localdids


class HiveAuth(Entity):
    access_token = None

    def __init__(self):
        Entity.__init__(self, "hive.auth")

    def init_app(self, app):
        self.app = app

    def __is_did(self, did_str):
        did = lib.DID_FromString(did_str.encode())
        if did is None:
            return False
        doc = lib.DID_Resolve(did, True)
        if doc is None:
            return False
        else:
            return True

    def __get_token_from_db(self, iss, appdid):
        return vp_token

    def sign_in(self):
        body = request.get_json(force=True, silent=True)
        if body is None:
            return response_err(401, "parameter is not application/json")
        document = body.get('document', None)
        if document is None:
            return response_err(400, "document is null")

        doc_str = json.dumps(body.get('document', None))
        doc = lib.DIDDocument_FromJson(doc_str.encode())
        if doc is None:
            return response_err(400, "doc is vaild")

        did = lib.DIDDocument_GetSubject(doc)
        if did is None:
            return response_err(400, "did can't get from doc")

        spec_did_str = ffi.string(lib.DID_GetMethodSpecificId(did)).decode()
        f = open(cache_dir + os.sep + spec_did_str, "w")
        f.write(doc_str)
        f.close()
        did_str = "did:" + ffi.string(lib.DID_GetMethod(did)).decode() +":" + spec_did_str

        # save to db
        nonce = create_nonce()
        exp = int(datetime.now().timestamp()) + DID_CHALLENGE_EXPIRE
        if not self.__save_nonce_to_db(nonce, did_str, exp):
            return response_err(500, "save to db fail!")

        # response token
        builder = lib.DIDDocument_GetJwtBuilder(self.doc)
        lib.JWTBuilder_SetHeader(builder, "type".encode(), "JWT".encode())
        lib.JWTBuilder_SetHeader(builder, "version".encode(), "1.0".encode())

        lib.JWTBuilder_SetSubject(builder, "DIDAuthCredential".encode())
        lib.JWTBuilder_SetAudience(builder, did_str.encode())
        lib.JWTBuilder_SetClaim(builder, "nonce".encode(), nonce.encode())
        lib.JWTBuilder_SetExpiration(builder, exp)

        token = ffi.string(lib.JWTBuilder_Compact(builder)).decode()
        # print(token)
        lib.JWTBuilder_Destroy(builder)
        data = {
            "jwt": token,
        }
        return response_ok(data)

    def request_did_auth(self):
        # get jwt
        body = request.get_json(force=True, silent=True)
        if body is None:
            return response_err(400, "parameter is not application/json")
        jwt = body.get('jwt', None)

        # check auth token
        credentialSubject, expTime = self.__check_auth_token(jwt)
        if credentialSubject is None:
            return response_err(400, expTime)

        # create access token
        exp = int(datetime.now().timestamp()) + DID_TOKEN_EXPIRE
        if exp > expTime:
            exp = expTime

        access_token = self.__create_access_token(credentialSubject, exp)
        if not access_token:
            return response_err(400, "create access token fail!")

        # save to db
        if not self.__save_token_to_db(credentialSubject, access_token, exp):
            return response_err(400, "save to db fail!")

        # response token
        app_instance_did = credentialSubject["id"]

        builder = lib.DIDDocument_GetJwtBuilder(self.doc)
        lib.JWTBuilder_SetHeader(builder, "type".encode(), "JWT".encode())
        lib.JWTBuilder_SetHeader(builder, "version".encode(), "1.0".encode())

        lib.JWTBuilder_SetSubject(builder, "DIDAuthCredential".encode())
        lib.JWTBuilder_SetAudience(builder, app_instance_did.encode())
        lib.JWTBuilder_SetClaim(builder, "token".encode(), access_token.encode())
        lib.JWTBuilder_SetExpiration(builder, exp)

        token = ffi.string(lib.JWTBuilder_Compact(builder)).decode()
        # print(token)
        lib.JWTBuilder_Destroy(builder)
        data = {
            "jwt": token,
        }
        return response_ok(data)

    def __check_auth_token(self, jwt):
        if jwt is None:
            return None, "jwt is null"

        #check jwt token
        jws = lib.JWTParser_Parse(jwt.encode())
        vp_str = lib.JWS_GetClaimAsJson(jws, "presentation".encode())

        vp = lib.Presentation_FromJson(vp_str)
        vp_json = json.loads(ffi.string(vp_str).decode())
        lib.JWS_Destroy(jws)

        #check vp
        ret = lib.Presentation_IsValid(vp)
        if not ret:
            return None, "vp isn't valid"
        # print(ffi.string(vp_str).decode())

        #check nonce
        nonce = vp_json["proof"]["nonce"]
        if nonce is None:
            return None, "nonce is none"

        #check did:nonce from db
        info = get_did_info_by_nonce(nonce)
        if info is None:
            return None, "nonce is error."

        if info[DID_INFO_NONCE] != nonce:
            return None, "nonce is error."

        #check reaml
        realm = vp_json["proof"]["realm"]
        if realm is None:
            return None, "realm is none."

        if realm != self.get_did_string():
            return None, "realm is error."

        #check
        vc_json = vp_json["verifiableCredential"][0]
        credentialSubject = vc_json["credentialSubject"]

        instance_did = credentialSubject["id"]
        if info[APP_INSTANCE_DID] != instance_did:
            return None, "app instance did is error."

        if info[DID_INFO_NONCE_EXPIRE] < int(datetime.now().timestamp()):
            return None, "nonce is expire"


        credentialSubject["userDid"] = vc_json["issuer"]
        credentialSubject["nonce"] = nonce
        expirationDate = vc_json["expirationDate"]
        timeArray = time.strptime(expirationDate, "%Y-%m-%dT%H:%M:%SZ")
        expTime = int(time.mktime(timeArray))

        return credentialSubject, expTime

    def __create_access_token(self, credentialSubject, exp):
        did_str = self.get_did_string()
        doc = lib.DIDStore_LoadDID(self.store, self.did)
        builder = lib.DIDDocument_GetJwtBuilder(doc)
        lib.JWTBuilder_SetHeader(builder, "typ".encode(), "JWT".encode())
        lib.JWTBuilder_SetHeader(builder, "version".encode(), "1.0".encode())

        lib.JWTBuilder_SetSubject(builder, "AccessAuthority".encode())
        lib.JWTBuilder_SetIssuer(builder, did_str.encode())
        lib.JWTBuilder_SetExpiration(builder, exp)
        lib.JWTBuilder_SetClaimWithJson(builder, "accessSubject".encode(), json.dumps(credentialSubject).encode())
        token = ffi.string(lib.JWTBuilder_Compact(builder)).decode()
        lib.JWTBuilder_Destroy(builder)
        return token

    def __save_nonce_to_db(self, nonce, app_instance_did, exp):
        info = get_did_info_by_nonce(nonce)

        try:
            if info is None:
                add_did_info_to_db(app_instance_did, nonce, exp)
            else:
                update_nonce_of_did_info(app_instance_did, nonce, exp)
        except Exception as e:
            logging.debug(f"Exception in __save_nonce_to_db:: {e}")
            return False

        return True

    def __save_token_to_db(self, credentialSubject, token, exp):
        user_did = credentialSubject["userDid"]
        app_id = credentialSubject["appId"]
        nonce = credentialSubject["nonce"]
        app_instance_did = credentialSubject["id"]

        try:
            update_token_of_did_info(user_did, app_id, app_instance_did, nonce, token, exp)
        except Exception as e:
            logging.debug(f"Exception in __save_token_to_db:: {e}")
            return False

        return True
