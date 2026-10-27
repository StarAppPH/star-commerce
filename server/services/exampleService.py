"""
Example service — copy this file and rename it to create a new service integration.

Steps to create a new service:
  1. Copy this file and rename it (e.g., accountingService.py)
  2. Rename the class to match (e.g., AccountingService)
     - The filename must be the camelCase version of the class name
     - e.g., class AccountingService -> filename: accountingService.py
  3. Set projectName= to the target repo's AppConfig().projectName whenever it
     differs from the filename (e.g. file accountingService.py -> projectName='accounting').
     Mock Pub/Sub messages are addressed with this value; the filename still
     drives the env-var names.
  4. Update cloudRunServiceName to match the Cloud Run service name
  5. Update the path with prod/dev URLs
  6. Set localRepoDirName to the target repo's folder name under the shared
     GitHub parent directory (e.g. 'accountingSystem'). AppCreator.py reads
     ../<folder>/server/AppCreator.py PORT and refuses to build if it differs
     from the port in dev=; it only warns when that checkout is absent.
  7. Set the env var for prod URL (e.g., ACCOUNTING_SERVICE_PROD_URL)
  8. Set the env var for PubSub (e.g., PUBSUB_ENABLED_ACCOUNTING=true)
  9. Register the service in AllServices.py
"""

from .__abstractService import AbstractService, Path
from .__request import request
from .__getIsProductionEnv import getIsProductionEnv
import requests
import os


class ExampleService(AbstractService):

    def __init__(self, **data):
        super().__init__(**data)
        self._validateProdUrl()

    def request(self, functionName: str, method: str, data: dict, useProd: bool = False) -> dict:
        return request(self.path, functionName, method, data, useProd,
                       service=self.cloudRunServiceName)

    def ping(self) -> bool:
        base_url = self.path.prod if getIsProductionEnv() else self.path.dev
        try:
            response = requests.head(base_url, timeout=0.2)
            return True
        except (requests.ConnectionError, requests.Timeout, requests.RequestException):
            return False


exampleService = ExampleService(
    # projectName='exampleTargetProjectName',
    cloudRunServiceName="example-service",
    localRepoDirName="example-service-repo",
    path=Path(prod=os.getenv("EXAMPLE_SERVICE_PROD_URL"),
              dev="http://127.0.0.1:5001/"))
