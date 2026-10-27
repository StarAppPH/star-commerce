from flask import Blueprint, jsonify, request
import logging
from mongoDb import db
from PubSubRequests import PubSubRequests
from routeErrors import routeFailure
from pubSub import decodeMessage
from objects import ApiResponse
from callerAuth import requirePushCaller

main = Blueprint('main', __name__)
logging.basicConfig(level=logging.INFO)


@main.route('/ping', methods=['POST'])
@requirePushCaller

def ping():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    messageId = None
    originalMessage = None
    try:
        if isinstance(data["message"]["data"],str):
            data["message"]["data"] = decodeMessage(data["message"]["data"])
        originalMessage = data["message"]["data"]
        originalTopicPath=data["message"].get("attributes").get("originalTopicPath")
        messageId = data["message"]["messageId"]
        if messageId != None and db.read({"_id": messageId},"PubSubMessages",findOne=True):
            response = ApiResponse(message="Message already processed", status=200, data=None)
            return jsonify(response.model_dump(exclude_unset=True)), response.status
        res = PubSubRequests().ping(originalMessage)
    except PermissionError as e:
        failure = routeFailure(e, status=403, push=True)
        logging.log(failure.level, {'function': 'ping', 'event': 'error', 'messageId': messageId, 'messageKeys': sorted(originalMessage) if isinstance(originalMessage, dict) else None, 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400, push=True, envelopeParsed=originalMessage is not None)
        logging.log(failure.level, {'function': 'ping', 'event': 'error', 'messageId': messageId, 'messageKeys': sorted(originalMessage) if isinstance(originalMessage, dict) else None, 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'ping', 'event': 'success', 'messageId': messageId, 'messageKeys': sorted(originalMessage) if isinstance(originalMessage, dict) else None})

    if messageId != None:
        try:
            db.create({"_id":messageId,"_version":0},"PubSubMessages")
        except:
            pass
    response = ApiResponse(message="Pong", status=200, data=res)
    return jsonify(response.model_dump(exclude_unset=True)), response.status

