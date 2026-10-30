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



@app.route('/devGetUserList', methods=['POST'])

def devGetUserList():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    try:
        res = ApiRequests().devGetUserList()
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'devGetUserList', 'event': 'error', 'requestKeys': [], 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'devGetUserList', 'event': 'error', 'requestKeys': [], 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'devGetUserList', 'event': 'success', 'requestKeys': []})

    response = ApiResponse(message="Dev user list fetched successfully", status=200, data=res)
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



@app.route('/updateMyStoreTheme', methods=['POST'])
@jwt_required()
def updateMyStoreTheme():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    photoStyle = data.get('photoStyle')
    primaryColor = data.get('primaryColor')
    secondaryColor = data.get('secondaryColor')
    voiceId = data.get('voiceId')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(photoStyle, (dict, list)):
            raise ValueError('Invalid photoStyle')
        if isinstance(primaryColor, (dict, list)):
            raise ValueError('Invalid primaryColor')
        if isinstance(secondaryColor, (dict, list)):
            raise ValueError('Invalid secondaryColor')
        if isinstance(voiceId, (dict, list)):
            raise ValueError('Invalid voiceId')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().updateMyStoreTheme(userId, photoStyle, primaryColor, secondaryColor, voiceId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'updateMyStoreTheme', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'photoStyle', 'primaryColor', 'secondaryColor', 'voiceId')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'updateMyStoreTheme', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'photoStyle', 'primaryColor', 'secondaryColor', 'voiceId')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'updateMyStoreTheme', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'photoStyle', 'primaryColor', 'secondaryColor', 'voiceId'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Store theme saved", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/createUserStore', methods=['POST'])
@jwt_required()
def createUserStore():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    name = data.get('name')
    description = data.get('description')
    contactNumber = data.get('contactNumber')
    address = data.get('address')
    currency = data.get('currency')
    openingHours = data.get('openingHours')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(name, (dict, list)):
            raise ValueError('Invalid name')
        if isinstance(description, (dict, list)):
            raise ValueError('Invalid description')
        if isinstance(contactNumber, (dict, list)):
            raise ValueError('Invalid contactNumber')
        if isinstance(address, (dict, list)):
            raise ValueError('Invalid address')
        if isinstance(currency, (dict, list)):
            raise ValueError('Invalid currency')
        if isinstance(openingHours, (dict, list)):
            raise ValueError('Invalid openingHours')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().createUserStore(userId, name, description, contactNumber, address, currency, openingHours)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'createUserStore', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'name', 'description', 'contactNumber', 'address', 'currency', 'openingHours')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'createUserStore', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'name', 'description', 'contactNumber', 'address', 'currency', 'openingHours')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'createUserStore', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'name', 'description', 'contactNumber', 'address', 'currency', 'openingHours'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Store created", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/updateMyStoreDetails', methods=['POST'])
@jwt_required()
def updateMyStoreDetails():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    name = data.get('name')
    description = data.get('description')
    contactNumber = data.get('contactNumber')
    address = data.get('address')
    openingHours = data.get('openingHours')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(name, (dict, list)):
            raise ValueError('Invalid name')
        if isinstance(description, (dict, list)):
            raise ValueError('Invalid description')
        if isinstance(contactNumber, (dict, list)):
            raise ValueError('Invalid contactNumber')
        if isinstance(address, (dict, list)):
            raise ValueError('Invalid address')
        if isinstance(openingHours, (dict, list)):
            raise ValueError('Invalid openingHours')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().updateMyStoreDetails(userId, name, description, contactNumber, address, openingHours)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'updateMyStoreDetails', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'name', 'description', 'contactNumber', 'address', 'openingHours')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'updateMyStoreDetails', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'name', 'description', 'contactNumber', 'address', 'openingHours')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'updateMyStoreDetails', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'name', 'description', 'contactNumber', 'address', 'openingHours'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Store details saved", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/suggestStoreAddress', methods=['POST'])
@jwt_required()
def suggestStoreAddress():
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
        res = ApiRequests().suggestStoreAddress(userId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'suggestStoreAddress', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId',)), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'suggestStoreAddress', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId',)), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'suggestStoreAddress', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId',))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Address suggested", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/setUserStoreLocation', methods=['POST'])
@jwt_required()
def setUserStoreLocation():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    latitude = data.get('latitude')
    longitude = data.get('longitude')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(latitude, (dict, list)):
            raise ValueError('Invalid latitude')
        if isinstance(longitude, (dict, list)):
            raise ValueError('Invalid longitude')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().setUserStoreLocation(userId, latitude, longitude)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'setUserStoreLocation', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'latitude', 'longitude')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'setUserStoreLocation', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'latitude', 'longitude')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'setUserStoreLocation', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'latitude', 'longitude'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Store location saved", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
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



@app.route('/listStoreProducts', methods=['POST'])
@jwt_required()
def listStoreProducts():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    storeId = data.get('storeId')
    search = data.get('search')
    limit = data.get('limit')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(storeId, (dict, list)):
            raise ValueError('Invalid storeId')
        if isinstance(search, (dict, list)):
            raise ValueError('Invalid search')
        if isinstance(limit, (dict, list)):
            raise ValueError('Invalid limit')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().listStoreProducts(userId, storeId, search, limit)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'listStoreProducts', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'search', 'limit')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'listStoreProducts', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'search', 'limit')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'listStoreProducts', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'search', 'limit'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Products fetched", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/addStoreProduct', methods=['POST'])
@jwt_required()
def addStoreProduct():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    name = data.get('name')
    price = data.get('price')
    inventory = data.get('inventory')
    description = data.get('description')
    isAvailable = data.get('isAvailable')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(name, (dict, list)):
            raise ValueError('Invalid name')
        if isinstance(price, (dict, list)):
            raise ValueError('Invalid price')
        if isinstance(inventory, (dict, list)):
            raise ValueError('Invalid inventory')
        if isinstance(description, (dict, list)):
            raise ValueError('Invalid description')
        if isinstance(isAvailable, (dict, list)):
            raise ValueError('Invalid isAvailable')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().addStoreProduct(userId, name, price, inventory, description, isAvailable)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'addStoreProduct', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'name', 'price', 'inventory', 'description', 'isAvailable')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'addStoreProduct', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'name', 'price', 'inventory', 'description', 'isAvailable')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'addStoreProduct', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'name', 'price', 'inventory', 'description', 'isAvailable'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Product added", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/addStoreProducts', methods=['POST'])
@jwt_required()
def addStoreProducts():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    products = data.get('products')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if not isinstance(products, list):
            raise ValueError('Invalid products')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().addStoreProducts(userId, products)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'addStoreProducts', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'products')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'addStoreProducts', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'products')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'addStoreProducts', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'products'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Products added", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/updateStoreProduct', methods=['POST'])
@jwt_required()
def updateStoreProduct():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    productId = data.get('productId')
    name = data.get('name')
    description = data.get('description')
    price = data.get('price')
    inventory = data.get('inventory')
    isAvailable = data.get('isAvailable')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(productId, (dict, list)):
            raise ValueError('Invalid productId')
        if isinstance(name, (dict, list)):
            raise ValueError('Invalid name')
        if isinstance(description, (dict, list)):
            raise ValueError('Invalid description')
        if isinstance(price, (dict, list)):
            raise ValueError('Invalid price')
        if isinstance(inventory, (dict, list)):
            raise ValueError('Invalid inventory')
        if isinstance(isAvailable, (dict, list)):
            raise ValueError('Invalid isAvailable')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().updateStoreProduct(userId, productId, name, description, price, inventory, isAvailable)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'updateStoreProduct', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'productId', 'name', 'description', 'price', 'inventory', 'isAvailable')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'updateStoreProduct', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'productId', 'name', 'description', 'price', 'inventory', 'isAvailable')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'updateStoreProduct', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'productId', 'name', 'description', 'price', 'inventory', 'isAvailable'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Product updated", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/listMyStoreProducts', methods=['POST'])
@jwt_required()
def listMyStoreProducts():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    search = data.get('search')
    limit = data.get('limit')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(search, (dict, list)):
            raise ValueError('Invalid search')
        if isinstance(limit, (dict, list)):
            raise ValueError('Invalid limit')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().listMyStoreProducts(userId, search, limit)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'listMyStoreProducts', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'search', 'limit')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'listMyStoreProducts', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'search', 'limit')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'listMyStoreProducts', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'search', 'limit'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Products fetched", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/getStoreOrders', methods=['POST'])
@jwt_required()
def getStoreOrders():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    status = data.get('status')
    period = data.get('period')
    startDate = data.get('startDate')
    endDate = data.get('endDate')
    limit = data.get('limit')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(status, (dict, list)):
            raise ValueError('Invalid status')
        if isinstance(period, (dict, list)):
            raise ValueError('Invalid period')
        if isinstance(startDate, (dict, list)):
            raise ValueError('Invalid startDate')
        if isinstance(endDate, (dict, list)):
            raise ValueError('Invalid endDate')
        if isinstance(limit, (dict, list)):
            raise ValueError('Invalid limit')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().getStoreOrders(userId, status, period, startDate, endDate, limit)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'getStoreOrders', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'status', 'period', 'startDate', 'endDate', 'limit')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'getStoreOrders', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'status', 'period', 'startDate', 'endDate', 'limit')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'getStoreOrders', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'status', 'period', 'startDate', 'endDate', 'limit'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Orders fetched", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/getMyOrders', methods=['POST'])
@jwt_required()
def getMyOrders():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    storeId = data.get('storeId')
    status = data.get('status')
    limit = data.get('limit')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(storeId, (dict, list)):
            raise ValueError('Invalid storeId')
        if isinstance(status, (dict, list)):
            raise ValueError('Invalid status')
        if isinstance(limit, (dict, list)):
            raise ValueError('Invalid limit')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().getMyOrders(userId, storeId, status, limit)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'getMyOrders', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'status', 'limit')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'getMyOrders', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'status', 'limit')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'getMyOrders', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'status', 'limit'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Orders fetched", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/createOrder', methods=['POST'])
@jwt_required()
def createOrder():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    storeId = data.get('storeId')
    items = data.get('items')
    fulfillment = data.get('fulfillment')
    delivery = data.get('delivery')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(storeId, (dict, list)):
            raise ValueError('Invalid storeId')
        if isinstance(fulfillment, (dict, list)):
            raise ValueError('Invalid fulfillment')
        if not isinstance(items, list):
            raise ValueError('Invalid items')
        if delivery is not None and not isinstance(delivery, dict):
            raise ValueError('Invalid delivery')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().createOrder(userId, storeId, items, fulfillment, delivery)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'createOrder', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'items', 'fulfillment', 'delivery')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'createOrder', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'items', 'fulfillment', 'delivery')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'createOrder', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'items', 'fulfillment', 'delivery'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Order placed", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/payOrder', methods=['POST'])
@jwt_required()
def payOrder():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    storeId = data.get('storeId')
    orderId = data.get('orderId')
    paymentMethod = data.get('paymentMethod')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(storeId, (dict, list)):
            raise ValueError('Invalid storeId')
        if isinstance(orderId, (dict, list)):
            raise ValueError('Invalid orderId')
        if isinstance(paymentMethod, (dict, list)):
            raise ValueError('Invalid paymentMethod')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().payOrder(userId, storeId, orderId, paymentMethod)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'payOrder', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'orderId', 'paymentMethod')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'payOrder', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'orderId', 'paymentMethod')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'payOrder', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'orderId', 'paymentMethod'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Order paid", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/completeOrder', methods=['POST'])
@jwt_required()
def completeOrder():
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
        res = ApiRequests().completeOrder(userId, orderId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'completeOrder', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'orderId')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'completeOrder', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'orderId')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'completeOrder', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'orderId'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Order completed", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/cancelStoreOrder', methods=['POST'])
@jwt_required()
def cancelStoreOrder():
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
        res = ApiRequests().cancelStoreOrder(userId, orderId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'cancelStoreOrder', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'orderId')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'cancelStoreOrder', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'orderId')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'cancelStoreOrder', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'orderId'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Order cancelled", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/cancelMyOrder', methods=['POST'])
@jwt_required()
def cancelMyOrder():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    storeId = data.get('storeId')
    orderId = data.get('orderId')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(storeId, (dict, list)):
            raise ValueError('Invalid storeId')
        if isinstance(orderId, (dict, list)):
            raise ValueError('Invalid orderId')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().cancelMyOrder(userId, storeId, orderId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'cancelMyOrder', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'orderId')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'cancelMyOrder', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'orderId')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'cancelMyOrder', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'orderId'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Order cancelled", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/requestReturn', methods=['POST'])
@jwt_required()
def requestReturn():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    storeId = data.get('storeId')
    orderId = data.get('orderId')
    items = data.get('items')
    reason = data.get('reason')
    note = data.get('note')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(storeId, (dict, list)):
            raise ValueError('Invalid storeId')
        if isinstance(orderId, (dict, list)):
            raise ValueError('Invalid orderId')
        if isinstance(reason, (dict, list)):
            raise ValueError('Invalid reason')
        if isinstance(note, (dict, list)):
            raise ValueError('Invalid note')
        if not isinstance(items, list):
            raise ValueError('Invalid items')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().requestReturn(userId, storeId, orderId, items, reason, note)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'requestReturn', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'orderId', 'items', 'reason', 'note')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'requestReturn', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'orderId', 'items', 'reason', 'note')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'requestReturn', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'orderId', 'items', 'reason', 'note'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Return requested", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/getMyReturns', methods=['POST'])
@jwt_required()
def getMyReturns():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    storeId = data.get('storeId')
    limit = data.get('limit')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(storeId, (dict, list)):
            raise ValueError('Invalid storeId')
        if isinstance(limit, (dict, list)):
            raise ValueError('Invalid limit')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().getMyReturns(userId, storeId, limit)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'getMyReturns', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'limit')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'getMyReturns', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'limit')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'getMyReturns', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'limit'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Returns fetched", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/cancelMyReturn', methods=['POST'])
@jwt_required()
def cancelMyReturn():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    storeId = data.get('storeId')
    returnId = data.get('returnId')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(storeId, (dict, list)):
            raise ValueError('Invalid storeId')
        if isinstance(returnId, (dict, list)):
            raise ValueError('Invalid returnId')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().cancelMyReturn(userId, storeId, returnId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'cancelMyReturn', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'returnId')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'cancelMyReturn', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'returnId')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'cancelMyReturn', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'returnId'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Return cancelled", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/getStoreReturns', methods=['POST'])
@jwt_required()
def getStoreReturns():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    status = data.get('status')
    limit = data.get('limit')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(status, (dict, list)):
            raise ValueError('Invalid status')
        if isinstance(limit, (dict, list)):
            raise ValueError('Invalid limit')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().getStoreReturns(userId, status, limit)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'getStoreReturns', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'status', 'limit')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'getStoreReturns', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'status', 'limit')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'getStoreReturns', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'status', 'limit'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Returns fetched", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/approveReturn', methods=['POST'])
@jwt_required()
def approveReturn():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    returnId = data.get('returnId')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(returnId, (dict, list)):
            raise ValueError('Invalid returnId')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().approveReturn(userId, returnId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'approveReturn', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'returnId')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'approveReturn', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'returnId')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'approveReturn', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'returnId'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Return approved", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/rejectReturn', methods=['POST'])
@jwt_required()
def rejectReturn():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    returnId = data.get('returnId')
    reason = data.get('reason')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(returnId, (dict, list)):
            raise ValueError('Invalid returnId')
        if isinstance(reason, (dict, list)):
            raise ValueError('Invalid reason')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().rejectReturn(userId, returnId, reason)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'rejectReturn', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'returnId', 'reason')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'rejectReturn', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'returnId', 'reason')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'rejectReturn', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'returnId', 'reason'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Return rejected", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/receiveReturn', methods=['POST'])
@jwt_required()
def receiveReturn():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    returnId = data.get('returnId')
    restock = data.get('restock')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(returnId, (dict, list)):
            raise ValueError('Invalid returnId')
        if isinstance(restock, (dict, list)):
            raise ValueError('Invalid restock')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().receiveReturn(userId, returnId, restock)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'receiveReturn', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'returnId', 'restock')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'receiveReturn', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'returnId', 'restock')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'receiveReturn', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'returnId', 'restock'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Return received", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
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



@app.route('/getStorePolicy', methods=['POST'])
@jwt_required()
def getStorePolicy():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    storeId = data.get('storeId')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(storeId, (dict, list)):
            raise ValueError('Invalid storeId')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().getStorePolicy(userId, storeId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'getStorePolicy', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'getStorePolicy', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'getStorePolicy', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Policy fetched", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/updateMyStorePolicy', methods=['POST'])
@jwt_required()
def updateMyStorePolicy():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    acceptsReturns = data.get('acceptsReturns')
    returnWindowDays = data.get('returnWindowDays')
    returnConditions = data.get('returnConditions')
    nonReturnableItems = data.get('nonReturnableItems')
    refundMethod = data.get('refundMethod')
    returnShippingPaidBy = data.get('returnShippingPaidBy')
    defectReportDays = data.get('defectReportDays')
    deliveryOptions = data.get('deliveryOptions')
    deliveryAreas = data.get('deliveryAreas')
    offersFreeDelivery = data.get('offersFreeDelivery')
    freeDeliveryMinimum = data.get('freeDeliveryMinimum')
    handoverDays = data.get('handoverDays')
    acceptedPaymentMethods = data.get('acceptedPaymentMethods')
    paidOrderCancellation = data.get('paidOrderCancellation')
    sendsMarketingMessages = data.get('sendsMarketingMessages')
    additionalTerms = data.get('additionalTerms')
    clear = data.get('clear')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(acceptsReturns, (dict, list)):
            raise ValueError('Invalid acceptsReturns')
        if isinstance(returnWindowDays, (dict, list)):
            raise ValueError('Invalid returnWindowDays')
        if isinstance(returnConditions, (dict, list)):
            raise ValueError('Invalid returnConditions')
        if isinstance(nonReturnableItems, (dict, list)):
            raise ValueError('Invalid nonReturnableItems')
        if isinstance(refundMethod, (dict, list)):
            raise ValueError('Invalid refundMethod')
        if isinstance(returnShippingPaidBy, (dict, list)):
            raise ValueError('Invalid returnShippingPaidBy')
        if isinstance(defectReportDays, (dict, list)):
            raise ValueError('Invalid defectReportDays')
        if isinstance(deliveryAreas, (dict, list)):
            raise ValueError('Invalid deliveryAreas')
        if isinstance(offersFreeDelivery, (dict, list)):
            raise ValueError('Invalid offersFreeDelivery')
        if isinstance(freeDeliveryMinimum, (dict, list)):
            raise ValueError('Invalid freeDeliveryMinimum')
        if isinstance(handoverDays, (dict, list)):
            raise ValueError('Invalid handoverDays')
        if isinstance(paidOrderCancellation, (dict, list)):
            raise ValueError('Invalid paidOrderCancellation')
        if isinstance(sendsMarketingMessages, (dict, list)):
            raise ValueError('Invalid sendsMarketingMessages')
        if isinstance(additionalTerms, (dict, list)):
            raise ValueError('Invalid additionalTerms')
        if deliveryOptions is not None and not isinstance(deliveryOptions, list):
            raise ValueError('Invalid deliveryOptions')
        if acceptedPaymentMethods is not None and not isinstance(acceptedPaymentMethods, list):
            raise ValueError('Invalid acceptedPaymentMethods')
        if clear is not None and not isinstance(clear, list):
            raise ValueError('Invalid clear')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().updateMyStorePolicy(userId, acceptsReturns, returnWindowDays, returnConditions, nonReturnableItems, refundMethod, returnShippingPaidBy, defectReportDays, deliveryOptions, deliveryAreas, offersFreeDelivery, freeDeliveryMinimum, handoverDays, acceptedPaymentMethods, paidOrderCancellation, sendsMarketingMessages, additionalTerms, clear)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'updateMyStorePolicy', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'acceptsReturns', 'returnWindowDays', 'returnConditions', 'nonReturnableItems', 'refundMethod', 'returnShippingPaidBy', 'defectReportDays', 'deliveryOptions', 'deliveryAreas', 'offersFreeDelivery', 'freeDeliveryMinimum', 'handoverDays', 'acceptedPaymentMethods', 'paidOrderCancellation', 'sendsMarketingMessages', 'additionalTerms', 'clear')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'updateMyStorePolicy', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'acceptsReturns', 'returnWindowDays', 'returnConditions', 'nonReturnableItems', 'refundMethod', 'returnShippingPaidBy', 'defectReportDays', 'deliveryOptions', 'deliveryAreas', 'offersFreeDelivery', 'freeDeliveryMinimum', 'handoverDays', 'acceptedPaymentMethods', 'paidOrderCancellation', 'sendsMarketingMessages', 'additionalTerms', 'clear')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'updateMyStorePolicy', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'acceptsReturns', 'returnWindowDays', 'returnConditions', 'nonReturnableItems', 'refundMethod', 'returnShippingPaidBy', 'defectReportDays', 'deliveryOptions', 'deliveryAreas', 'offersFreeDelivery', 'freeDeliveryMinimum', 'handoverDays', 'acceptedPaymentMethods', 'paidOrderCancellation', 'sendsMarketingMessages', 'additionalTerms', 'clear'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Policy saved", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
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
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(message, (dict, list)):
            raise ValueError('Invalid message')
        if isinstance(conversationId, (dict, list)):
            raise ValueError('Invalid conversationId')
        if form is not None and not isinstance(form, dict):
            raise ValueError('Invalid form')
        if attachments is not None and not isinstance(attachments, list):
            raise ValueError('Invalid attachments')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().sendAdminChatMessage(userId, message, conversationId, form, attachments)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'sendAdminChatMessage', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'message', 'conversationId', 'form', 'attachments')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'sendAdminChatMessage', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'message', 'conversationId', 'form', 'attachments')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'sendAdminChatMessage', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'message', 'conversationId', 'form', 'attachments'))})

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
        if form is not None and not isinstance(form, dict):
            raise ValueError('Invalid form')
        if attachments is not None and not isinstance(attachments, list):
            raise ValueError('Invalid attachments')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().sendStoreChatMessage(userId, storeId, message, conversationId, form, attachments)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'sendStoreChatMessage', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'message', 'conversationId', 'form', 'attachments')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'sendStoreChatMessage', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'message', 'conversationId', 'form', 'attachments')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'sendStoreChatMessage', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'message', 'conversationId', 'form', 'attachments'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Reply sent", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
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



@app.route('/sendAdminVoiceMessage', methods=['POST'])
@jwt_required()
def sendAdminVoiceMessage():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    audio = data.get('audio')
    conversationId = data.get('conversationId')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(audio, (dict, list)):
            raise ValueError('Invalid audio')
        if isinstance(conversationId, (dict, list)):
            raise ValueError('Invalid conversationId')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().sendAdminVoiceMessage(userId, audio, conversationId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'sendAdminVoiceMessage', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'audio', 'conversationId')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'sendAdminVoiceMessage', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'audio', 'conversationId')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'sendAdminVoiceMessage', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'audio', 'conversationId'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Reply sent", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/sendStoreVoiceMessage', methods=['POST'])
@jwt_required()
def sendStoreVoiceMessage():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    storeId = data.get('storeId')
    audio = data.get('audio')
    conversationId = data.get('conversationId')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(storeId, (dict, list)):
            raise ValueError('Invalid storeId')
        if isinstance(audio, (dict, list)):
            raise ValueError('Invalid audio')
        if isinstance(conversationId, (dict, list)):
            raise ValueError('Invalid conversationId')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().sendStoreVoiceMessage(userId, storeId, audio, conversationId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'sendStoreVoiceMessage', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'audio', 'conversationId')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'sendStoreVoiceMessage', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'audio', 'conversationId')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'sendStoreVoiceMessage', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'storeId', 'audio', 'conversationId'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Reply sent", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/speakChatTurn', methods=['POST'])
@jwt_required()
def speakChatTurn():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    conversationId = data.get('conversationId')
    turnSeq = data.get('turnSeq')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(conversationId, (dict, list)):
            raise ValueError('Invalid conversationId')
        if isinstance(turnSeq, (dict, list)):
            raise ValueError('Invalid turnSeq')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().speakChatTurn(userId, conversationId, turnSeq)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'speakChatTurn', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'conversationId', 'turnSeq')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'speakChatTurn', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'conversationId', 'turnSeq')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'speakChatTurn', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'conversationId', 'turnSeq'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Reply spoken", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/listVoices', methods=['POST'])
@jwt_required()
def listVoices():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    search = data.get('search')
    pageToken = data.get('pageToken')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(search, (dict, list)):
            raise ValueError('Invalid search')
        if isinstance(pageToken, (dict, list)):
            raise ValueError('Invalid pageToken')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().listVoices(userId, search, pageToken)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'listVoices', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'search', 'pageToken')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'listVoices', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'search', 'pageToken')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'listVoices', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'search', 'pageToken'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Voices fetched", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/generateProductPhotos', methods=['POST'])
@jwt_required()
def generateProductPhotos():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    imageUrls = data.get('imageUrls')
    count = data.get('count')
    notes = data.get('notes')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(count, (dict, list)):
            raise ValueError('Invalid count')
        if isinstance(notes, (dict, list)):
            raise ValueError('Invalid notes')
        if not isinstance(imageUrls, list):
            raise ValueError('Invalid imageUrls')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().generateProductPhotos(userId, imageUrls, count, notes)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'generateProductPhotos', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'imageUrls', 'count', 'notes')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'generateProductPhotos', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'imageUrls', 'count', 'notes')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'generateProductPhotos', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'imageUrls', 'count', 'notes'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Photos created", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/setProductPhoto', methods=['POST'])
@jwt_required()
def setProductPhoto():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    userId = data.get('userId')
    productId = data.get('productId')
    photoId = data.get('photoId')
    current_user = get_jwt_identity()
    try:
        if isinstance(userId, (dict, list)):
            raise ValueError('Invalid userId')
        if isinstance(productId, (dict, list)):
            raise ValueError('Invalid productId')
        if isinstance(photoId, (dict, list)):
            raise ValueError('Invalid photoId')
        if userId != current_user:
            raise ValueError("Unauthorized access")
        res = ApiRequests().setProductPhoto(userId, productId, photoId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'setProductPhoto', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'productId', 'photoId')), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'setProductPhoto', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('userId', 'productId', 'photoId')), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'setProductPhoto', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('userId', 'productId', 'photoId'))})

    jwtToken = create_access_token(identity=current_user)
    response = ApiResponse(message="Product photo set", status=200, current_user=current_user, data=res, jwtToken=jwtToken)
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
