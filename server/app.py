from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_compress import Compress
from AppConfig import AppConfig
from objects import *
from ApiRequests import ApiRequests
import os
import logging
from appPubSub import main
from flask_jwt_extended import (JWTManager, create_access_token, jwt_required, get_jwt_identity)
#from Settings import DatabaseSettingUpdater
from datetime import timedelta
from routeErrors import routeFailure
from callerAuth import requireServiceCaller

app = Flask(__name__)
app.register_blueprint(main)
if os.getenv("JWT_SECRET_KEY") == None:
    raise ValueError("JWT_SECRET_KEY environment variable is not set")
# Outside localdev the signing key must be a real secret: anyone who knows it can mint a token for any user.
if os.getenv("ENVIRONMENT") != "localdev" and (len(os.getenv("JWT_SECRET_KEY")) < 32 or os.getenv("JWT_SECRET_KEY") == "change-me-to-a-secret-key"):
    raise ValueError("JWT_SECRET_KEY must be a random secret of at least 32 characters outside localdev")
app.config["JWT_SECRET_KEY"] = os.getenv("JWT_SECRET_KEY")
app.config["JWT_ACCESS_TOKEN_EXPIRES"] = timedelta(minutes=5)
# The largest request body Flask accepts (a larger one is refused with 413). Raise it if a route takes uploads.
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024
jwt = JWTManager(app)
CORS(app, resources={r"/*": {"origins": "*"}})
app.config["COMPRESS_ALGORITHM"] = ["br", "gzip"]
app.config["COMPRESS_ALGORITHM_STREAMING"] = ["br"]
app.config["COMPRESS_BR_LEVEL"] = 4
app.config["COMPRESS_LEVEL"] = 6
app.config["COMPRESS_MIN_SIZE"] = 500
app.config["COMPRESS_MIMETYPES"] = ["application/json", "text/csv"]
app.config["COMPRESS_REGISTER"] = False
app.config["COMPRESS_DISABLED"] = (os.getenv("COMPRESS_DISABLED") or "").strip().lower() in ("1", "true", "yes", "on")
compress = Compress(app)
logging.basicConfig(level=logging.INFO)


@app.route('/loginWithGoogle', methods=['POST'])

def loginWithGoogle():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    firebaseUserObject = data.get('firebaseUserObject')
    try:
        if not isinstance(firebaseUserObject, dict):
            raise ValueError('Invalid firebaseUserObject')
        res = ApiRequests().loginWithGoogle(firebaseUserObject)
        access_token = create_access_token(identity=res["_id"])
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'loginWithGoogle', 'event': 'error', 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'loginWithGoogle', 'event': 'error', 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'loginWithGoogle', 'event': 'success'})

    response = ApiResponse(message="Login successful", status=200, data=res, access_token=access_token)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/devLogin', methods=['POST'])

def devLogin():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        res = ApiRequests().devLogin(userId)
        access_token = create_access_token(identity=res["_id"])
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'devLogin', 'event': 'error', 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'devLogin', 'event': 'error', 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'devLogin', 'event': 'success'})

    response = ApiResponse(message="Dev login successful", status=200, data=res, access_token=access_token)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/getRoles', methods=['POST'])
@jwt_required()
def getRoles():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().getRoles(userId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'getRoles', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId',)), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'getRoles', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId',)), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'getRoles', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId',))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Roles fetched successfully", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/getUserRoleTypes', methods=['POST'])
@jwt_required()
def getUserRoleTypes():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().getUserRoleTypes(userId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'getUserRoleTypes', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId',)), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'getUserRoleTypes', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId',)), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'getUserRoleTypes', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId',))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="User role types fetched successfully", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/getUserStore', methods=['POST'])
@jwt_required()
def getUserStore():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().getUserStore(userId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'getUserStore', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId',)), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'getUserStore', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId',)), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'getUserStore', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId',))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Store fetched", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/setMyDeliveryZones', methods=['POST'])
@jwt_required()
def setMyDeliveryZones():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    zones = data.get('zones')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if not isinstance(zones, list):
            raise ValueError('Invalid zones')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().setMyDeliveryZones(userId, zones)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'setMyDeliveryZones', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'zones')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'setMyDeliveryZones', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'zones')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'setMyDeliveryZones', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'zones'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Delivery zones saved", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/resetMyStore', methods=['POST'])
@jwt_required()
def resetMyStore():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    confirmName = data.get('confirmName')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(confirmName, (dict, list)):
            raise ValueError('Invalid confirmName')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().resetMyStore(userId, confirmName)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'resetMyStore', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'confirmName')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'resetMyStore', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'confirmName')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'resetMyStore', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'confirmName'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Store reset", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/startGuestSession', methods=['POST'])

def startGuestSession():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    idToken = data.get('idToken')
    storeSlug = data.get('storeSlug')
    try:
        if isinstance(idToken, (dict, list)):
            raise ValueError('Invalid idToken')
        if isinstance(storeSlug, (dict, list)):
            raise ValueError('Invalid storeSlug')
        res = ApiRequests().startGuestSession(idToken, storeSlug)
        access_token = create_access_token(identity=res["_id"])
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'startGuestSession', 'event': 'error', 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'startGuestSession', 'event': 'error', 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'startGuestSession', 'event': 'success'})

    response = ApiResponse(message="Welcome", status=200, data=res, access_token=access_token)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/verifyCustomerPhone', methods=['POST'])
@jwt_required()
def verifyCustomerPhone():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    idToken = data.get('idToken')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(idToken, (dict, list)):
            raise ValueError('Invalid idToken')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().verifyCustomerPhone(userId, idToken)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'verifyCustomerPhone', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'idToken')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'verifyCustomerPhone', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'idToken')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'verifyCustomerPhone', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'idToken'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Phone number verified", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/getWaitingOnYou', methods=['POST'])
@jwt_required()
def getWaitingOnYou():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().getWaitingOnYou(userId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'getWaitingOnYou', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId',)), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'getWaitingOnYou', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId',)), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'getWaitingOnYou', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId',))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Waiting work counted", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/addPaymentReceipt', methods=['POST'])
@jwt_required()
def addPaymentReceipt():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    storeId = data.get('storeId')
    orderId = data.get('orderId')
    receiptUrl = data.get('receiptUrl')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(storeId, (dict, list)):
            raise ValueError('Invalid storeId')
        if isinstance(orderId, (dict, list)):
            raise ValueError('Invalid orderId')
        if isinstance(receiptUrl, (dict, list)):
            raise ValueError('Invalid receiptUrl')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().addPaymentReceipt(userId, storeId, orderId, receiptUrl)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'addPaymentReceipt', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'orderId', 'receiptUrl')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'addPaymentReceipt', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'orderId', 'receiptUrl')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'addPaymentReceipt', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'orderId', 'receiptUrl'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Receipt sent to the store", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/getPaymentReceipt', methods=['POST'])
@jwt_required()
def getPaymentReceipt():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    orderId = data.get('orderId')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(orderId, (dict, list)):
            raise ValueError('Invalid orderId')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().getPaymentReceipt(userId, orderId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'getPaymentReceipt', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'orderId')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'getPaymentReceipt', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'orderId')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'getPaymentReceipt', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'orderId'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Receipt fetched", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/getMyStorePolicy', methods=['POST'])
@jwt_required()
def getMyStorePolicy():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().getMyStorePolicy(userId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'getMyStorePolicy', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId',)), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'getMyStorePolicy', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId',)), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'getMyStorePolicy', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId',))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Policy fetched", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/sendAdminChatMessage', methods=['POST'])
@jwt_required()
def sendAdminChatMessage():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    message = data.get('message')
    conversationId = data.get('conversationId')
    form = data.get('form')
    attachments = data.get('attachments')
    turnKey = data.get('turnKey')
    speak = data.get('speak')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(message, (dict, list)):
            raise ValueError('Invalid message')
        if isinstance(conversationId, (dict, list)):
            raise ValueError('Invalid conversationId')
        if isinstance(turnKey, (dict, list)):
            raise ValueError('Invalid turnKey')
        if isinstance(speak, (dict, list)):
            raise ValueError('Invalid speak')
        if form is not None and not isinstance(form, dict):
            raise ValueError('Invalid form')
        if attachments is not None and not isinstance(attachments, list):
            raise ValueError('Invalid attachments')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().sendAdminChatMessage(userId, message, conversationId, form, attachments, turnKey, speak)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'sendAdminChatMessage', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'message', 'conversationId', 'form', 'attachments', 'turnKey', 'speak')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'sendAdminChatMessage', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'message', 'conversationId', 'form', 'attachments', 'turnKey', 'speak')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'sendAdminChatMessage', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'message', 'conversationId', 'form', 'attachments', 'turnKey', 'speak'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Reply sent", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/sendStoreChatMessage', methods=['POST'])
@jwt_required()
def sendStoreChatMessage():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    storeId = data.get('storeId')
    message = data.get('message')
    conversationId = data.get('conversationId')
    form = data.get('form')
    attachments = data.get('attachments')
    turnKey = data.get('turnKey')
    speak = data.get('speak')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(storeId, (dict, list)):
            raise ValueError('Invalid storeId')
        if isinstance(message, (dict, list)):
            raise ValueError('Invalid message')
        if isinstance(conversationId, (dict, list)):
            raise ValueError('Invalid conversationId')
        if isinstance(turnKey, (dict, list)):
            raise ValueError('Invalid turnKey')
        if isinstance(speak, (dict, list)):
            raise ValueError('Invalid speak')
        if form is not None and not isinstance(form, dict):
            raise ValueError('Invalid form')
        if attachments is not None and not isinstance(attachments, list):
            raise ValueError('Invalid attachments')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().sendStoreChatMessage(userId, storeId, message, conversationId, form, attachments, turnKey, speak)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'sendStoreChatMessage', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'message', 'conversationId', 'form', 'attachments', 'turnKey', 'speak')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'sendStoreChatMessage', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'message', 'conversationId', 'form', 'attachments', 'turnKey', 'speak')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'sendStoreChatMessage', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'message', 'conversationId', 'form', 'attachments', 'turnKey', 'speak'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Reply sent", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/getChatThinking', methods=['POST'])
@jwt_required()
def getChatThinking():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    turnKey = data.get('turnKey')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(turnKey, (dict, list)):
            raise ValueError('Invalid turnKey')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().getChatThinking(userId, turnKey)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'getChatThinking', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'turnKey')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'getChatThinking', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'turnKey')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'getChatThinking', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'turnKey'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Thinking fetched", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/listMyConversations', methods=['POST'])
@jwt_required()
def listMyConversations():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    storeId = data.get('storeId')
    before = data.get('before')
    limit = data.get('limit')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(storeId, (dict, list)):
            raise ValueError('Invalid storeId')
        if isinstance(before, (dict, list)):
            raise ValueError('Invalid before')
        if isinstance(limit, (dict, list)):
            raise ValueError('Invalid limit')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().listMyConversations(userId, storeId, before, limit)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'listMyConversations', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'before', 'limit')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'listMyConversations', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'before', 'limit')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'listMyConversations', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'before', 'limit'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Conversations fetched", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/getConversationTurns', methods=['POST'])
@jwt_required()
def getConversationTurns():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    conversationId = data.get('conversationId')
    beforeSeq = data.get('beforeSeq')
    limit = data.get('limit')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(conversationId, (dict, list)):
            raise ValueError('Invalid conversationId')
        if isinstance(beforeSeq, (dict, list)):
            raise ValueError('Invalid beforeSeq')
        if isinstance(limit, (dict, list)):
            raise ValueError('Invalid limit')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().getConversationTurns(userId, conversationId, beforeSeq, limit)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'getConversationTurns', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'conversationId', 'beforeSeq', 'limit')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'getConversationTurns', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'conversationId', 'beforeSeq', 'limit')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'getConversationTurns', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'conversationId', 'beforeSeq', 'limit'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Turns fetched", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/speakChatThinking', methods=['POST'])
@jwt_required()
def speakChatThinking():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    turnKey = data.get('turnKey')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(turnKey, (dict, list)):
            raise ValueError('Invalid turnKey')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().speakChatThinking(userId, turnKey)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'speakChatThinking', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'turnKey')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'speakChatThinking', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'turnKey')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'speakChatThinking', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'turnKey'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Thinking spoken", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



# Never compressed: the routes that issue an access token (BREACH; see
# AppCreator._compression_block).
NO_COMPRESSION_ENDPOINTS = frozenset(['devLogin', 'loginWithGoogle', 'startGuestSession'])

if app.config["COMPRESS_DISABLED"]:
    logging.warning({'event': 'compressionDisabled'})


@app.after_request
def _compressResponse(response):
    if app.config["COMPRESS_DISABLED"] or request.endpoint in NO_COMPRESSION_ENDPOINTS:
        return response
    return compress.after_request(response)


# Cloud Run's HTTP startup probe (cloudbuild.yaml) calls this. It answers
# only once app.py has fully imported, so startup, which has full CPU,
# lasts until the app is loaded (see AppCreator._startup_check_block).
@app.route('/startupCheck', methods=['GET'])
def startupCheck():
    return jsonify({'status': 'ok'}), 200


from werkzeug.exceptions import HTTPException


@app.errorhandler(Exception)
def _unhandledError(e):
    if isinstance(e, HTTPException):
        return e
    failure = routeFailure(e, status=403 if isinstance(e, PermissionError) else 400)
    logging.log(failure.level, {'function': request.endpoint, 'event': 'error', 'traceback': failure.traceback})
    return failure.response()


def _localBindHost() -> str:
    # The host a local run binds: 127.0.0.1, unless LOCAL_BIND_HOST (in
    # server/.env) names another, e.g. 0.0.0.0 to reach it from the LAN.
    host = (os.getenv("LOCAL_BIND_HOST") or "").strip() or "127.0.0.1"
    if host != "127.0.0.1":
        line = "!" * 78
        print(f"\033[91m{line}\n"
              f"LOCAL_BIND_HOST={host}: this server is reachable from your network.\n"
              "localdev accepts a sign-in with a raw uid and email, and devLogin signs\n"
              "in as any user: anyone who can reach it can act as any user.\n"
              f"{line}\033[0m")
        logging.warning({'event': 'localBindHostExposed', 'host': host})
    return host

if __name__ == '__main__':
    if (AppConfig().getIsDevEnvironment()):
        print(f"[92m_______________________{AppConfig().getEnvironment().upper()}_______________________[0m")
    if AppConfig().getIsProductionEnvironment():
        print(f"[91m_______________________{AppConfig().getEnvironment().upper()}_______________________[0m")

    #from Settings import DatabaseSettingUpdater
    #DatabaseSettingUpdater().updateDatabaseSettingsToDefault()

    if AppConfig().getisLocalEnvironment():
        app.run(debug=False, host=_localBindHost(), port=5003)
    else:
        app.run(host='0.0.0.0', port=8080)
