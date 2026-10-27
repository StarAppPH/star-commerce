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



@app.route('/handshake', methods=['GET'])

def handshake():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    try:
        res = ApiRequests().handshake()
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'handshake', 'event': 'error', 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'handshake', 'event': 'error', 'traceback': failure.traceback})
        return failure.response()



    response = ApiResponse(message="Handshake successful", status=200, data=res)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/getTodos', methods=['POST'])

def getTodos():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    try:
        res = ApiRequests().getTodos()
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'getTodos', 'event': 'error', 'requestKeys': [], 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'getTodos', 'event': 'error', 'requestKeys': [], 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'getTodos', 'event': 'success', 'requestKeys': []})

    response = ApiResponse(message="Todos fetched", status=200, data=res)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/createTodo', methods=['POST'])

def createTodo():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    title = data.get('title')
    try:
        if isinstance(title, (dict, list)):
            raise ValueError('Invalid title')
        res = ApiRequests().createTodo(title)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'createTodo', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('title',)), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'createTodo', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('title',)), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'createTodo', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('title',))})

    response = ApiResponse(message="Todo created", status=200, data=res)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/markTodoAsCompleted', methods=['POST'])

def markTodoAsCompleted():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    todoId = data.get('todoId')
    try:
        if isinstance(todoId, (dict, list)):
            raise ValueError('Invalid todoId')
        res = ApiRequests().markTodoAsCompleted(todoId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'markTodoAsCompleted', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('todoId',)), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'markTodoAsCompleted', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('todoId',)), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'markTodoAsCompleted', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('todoId',))})

    response = ApiResponse(message="Todo marked as completed", status=200, data=res)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/markTodoAsUnfinished', methods=['POST'])

def markTodoAsUnfinished():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    todoId = data.get('todoId')
    try:
        if isinstance(todoId, (dict, list)):
            raise ValueError('Invalid todoId')
        res = ApiRequests().markTodoAsUnfinished(todoId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'markTodoAsUnfinished', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('todoId',)), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'markTodoAsUnfinished', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('todoId',)), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'markTodoAsUnfinished', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('todoId',))})

    response = ApiResponse(message="Todo marked as unfinished", status=200, data=res)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



@app.route('/deleteTodo', methods=['POST'])

def deleteTodo():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        data = {}
    todoId = data.get('todoId')
    try:
        if isinstance(todoId, (dict, list)):
            raise ValueError('Invalid todoId')
        res = ApiRequests().deleteTodo(todoId)
    except PermissionError as e:
        failure = routeFailure(e, status=403)
        logging.log(failure.level, {'function': 'deleteTodo', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('todoId',)), 'traceback': failure.traceback})
        return failure.response()
    except Exception as e:
        failure = routeFailure(e, status=400)
        logging.log(failure.level, {'function': 'deleteTodo', 'event': 'error', 'requestKeys': sorted(key for key in data if key in ('todoId',)), 'traceback': failure.traceback})
        return failure.response()

    logging.info({'function': 'deleteTodo', 'event': 'success', 'requestKeys': sorted(key for key in data if key in ('todoId',))})

    response = ApiResponse(message="Todo deleted", status=200, data=res)
    return jsonify(response.model_dump(exclude_unset=True)), response.status



# Never compressed: the routes that issue an access token (BREACH; see
# AppCreator._compression_block).
NO_COMPRESSION_ENDPOINTS = frozenset(['devLogin', 'loginWithGoogle'])

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
