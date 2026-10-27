import os
import datetime
import functools

if os.getenv('ENVIRONMENT', '').startswith('local'):
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))


# Keys that must NEVER leave the service in client-facing HTTP responses or
# pubsub payloads (e.g. secret tokens that act as capabilities). Stripping is
# enforced automatically at the two egress boundaries (ApiResponse.data
# serialization in objects.py and the pubSubDecorator publish step in
# pubSub.py) — endpoints do NOT need manual pops or {'field': 0} projections.
# Add your app's secret field names here.
SECRET_RESPONSE_KEYS = frozenset()


class Project:

    def __init__(self):
        super().__init__()
        self.projectName = 'starCommerce'
        self.projectId = os.getenv('GOOGLE_CLOUD_PROJECT_ID')
        self.gcpRegion = os.getenv('GCP_REGION', 'asia-east1')


@functools.cache
def _serviceEnvVarNames() -> tuple[tuple[str, str], ...]:
    """(serviceName, PUBSUB_ENABLED_* variable) for every service object in
    services/, found once per process. The modules are imported, never run
    again: a module the app already imported is reused as is, and no service
    object is built twice (running every services/*.py on each AppConfig()
    cost ~8 ms a call, and AppConfig() runs on every publish and transaction)."""
    import importlib
    import re
    from services.__abstractService import AbstractService

    services_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'services')
    pairs = {}
    for filename in sorted(os.listdir(services_dir)):
        if not filename.endswith('.py'):
            continue
        try:
            module = importlib.import_module('services.' + filename[:-3])
        except Exception:
            continue
        for var_name, obj in vars(module).items():
            if isinstance(obj, AbstractService):
                pairs[var_name] = 'PUBSUB_ENABLED_' + re.sub(r'(?<=[a-z0-9])([A-Z])', r'_\1', var_name).upper()
    return tuple(pairs.items())


class PubSubConfig:
    def __init__(self):
        super().__init__()
        # The variables' VALUES are read on every init, so a test (or a
        # process) that changes one sees it in the next AppConfig().
        self._serviceEnabled = {}
        for service_name, env_var_name in _serviceEnvVarNames():
            value = os.getenv(env_var_name)
            if value is None:
                raise Exception(f"{env_var_name} environment variable is not set")
            value = value.lower()
            if value not in ['true', 'false']:
                raise Exception(f"Invalid {env_var_name} environment variable")
            self._serviceEnabled[service_name] = value == 'true'

    def isServiceEnabled(self, serviceName: str) -> bool:
        if serviceName not in self._serviceEnabled:
            raise ValueError(f"Unknown service: {serviceName}. Available: {list(self._serviceEnabled.keys())}")
        return self._serviceEnabled[serviceName]


class MongoDb(Project):

    def __init__(self):
        super().__init__()
        # cloudprod must name its database (a default could point a
        # production service at the wrong one); elsewhere test<projectName>.
        self.databaseName = os.getenv('databaseName') or None
        if self.databaseName is None:
            if os.getenv('ENVIRONMENT') == 'cloudprod':
                raise Exception(
                    'databaseName environment is not set for cloudprod environment')
            self.databaseName = 'test' + self.projectName

    def getDatabaseName(self) -> str:
        return self.databaseName


class Timezone:

    def __init__(self):
        super().__init__()
        self.timezone = 8

    def getTimezone(self) -> datetime.timezone:
        return datetime.timezone(datetime.timedelta(hours=self.timezone))


class Environment:

    def __init__(self):
        self.environment = os.getenv('ENVIRONMENT')
        if self.environment not in [
                'localdev', 'localprod', 'clouddev', 'cloudprod'
        ]:
            raise Exception("Invalid environment")
        # The other bases run after the check: an invalid ENVIRONMENT is the
        # error, whatever else is missing.
        super().__init__()

    def getEnvironment(self) -> str:
        return self.environment

    def getIsCloudEnvironment(self) -> bool:
        return self.getEnvironment() in ['clouddev', 'cloudprod']

    def getIsProductionEnvironment(self) -> bool:
        return self.getEnvironment() in ['cloudprod']

    def getisLocalDevEnvironment(self) -> bool:
        return self.getEnvironment() in ['localdev']

    def getisLocalEnvironment(self) -> bool:
        return self.getEnvironment() in ['localdev', 'localprod']

    def getIsDevEnvironment(self) -> bool:
        return self.getEnvironment() in ['localdev', 'clouddev']


class AppConfig(Environment, PubSubConfig, Timezone, MongoDb):

    def __init__(self):
        # Every base calls super().__init__(), so this runs each of them once,
        # in MRO order (Environment, PubSubConfig, Timezone, MongoDb, Project).
        super().__init__()


if __name__ == "__main__":
    appConfig = AppConfig()
