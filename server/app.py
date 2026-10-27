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
